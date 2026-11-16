"""Core business logic for Callzo Task 1 — Usage Metering Service:
- Constant-time HMAC-SHA256 verification over raw bytes
- Idempotent webhook ingestion (duplicate event_id protection)
- Out-of-order call lifecycle reconciliation (call.ended arriving before call.started)
- Late arrival policy (~35 days old events attributed to historical billing month + audit flag)
- Monthly integer cent cap enforcement (Soft 80% warning + Hard 100% cap)
- Background retry queue with exponential backoff + raw-event ledger reconciliation
"""
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from .database import DbCursorWrapper, db_adapter
from .schemas import WebhookCallEvent

logger = logging.getLogger("callzo.metering")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
DEFAULT_MONTHLY_CAP_CENTS = int(os.getenv("DEFAULT_MONTHLY_CAP_CENTS", "2500"))
LATE_EVENT_THRESHOLD_DAYS = int(os.getenv("LATE_EVENT_THRESHOLD_DAYS", "30"))
HARD_CAP_HTTP_429_ON_START = os.getenv("HARD_CAP_HTTP_429_ON_START", "false").lower() == "true"

METRICS: Dict[str, int] = {
    "webhooks_received_total": 0,
    "webhooks_accepted_total": 0,
    "webhooks_duplicate_ignored_total": 0,
    "webhooks_rejected_signature_total": 0,
    "webhooks_rejected_malformed_total": 0,
    "events_out_of_order_resolved_total": 0,
    "events_late_flagged_total": 0,
    "cap_soft_warnings_total": 0,
    "cap_hard_breaches_total": 0,
    "cap_blocked_calls_total": 0,
    "retry_jobs_enqueued_total": 0,
    "retry_jobs_completed_total": 0,
    "reconciliation_runs_total": 0,
    "reconciliation_drifts_healed_total": 0,
}


def compute_hmac_sha256(raw_body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, secret: str = WEBHOOK_SECRET) -> bool:
    if not secret or not signature_header or not isinstance(signature_header, str):
        return False
    cleaned_sig = signature_header.strip()
    if cleaned_sig.lower().startswith("sha256="):
        cleaned_sig = cleaned_sig[7:].strip()
    expected = compute_hmac_sha256(raw_body, secret=secret)
    return hmac.compare_digest(cleaned_sig.lower(), expected.lower())


def _ensure_account(db: DbCursorWrapper, account_id: str, now_iso: str) -> Dict[str, Any]:
    lock_clause = " FOR UPDATE" if db._is_pg else ""
    row = db.execute("SELECT * FROM accounts WHERE account_id = ?" + lock_clause, (account_id,)).fetchone()
    if row is None:
        db.execute(
            """
            INSERT INTO accounts (
                account_id, monthly_cap_cents, soft_cap_warning_pct,
                is_capped, cap_state, capped_at, created_at, updated_at
            ) VALUES (?, ?, 80, 0, 'OK', NULL, ?, ?)
            ON CONFLICT(account_id) DO NOTHING
            """,
            (account_id, DEFAULT_MONTHLY_CAP_CENTS, now_iso, now_iso),
        )
        row = db.execute("SELECT * FROM accounts WHERE account_id = ?" + lock_clause, (account_id,)).fetchone()
    return row


def _ensure_monthly_usage(db: DbCursorWrapper, account_id: str, billing_month: str, now_iso: str) -> Dict[str, Any]:
    row = db.execute(
        "SELECT * FROM account_monthly_usage WHERE account_id = ? AND billing_month = ?",
        (account_id, billing_month),
    ).fetchone()
    if row is None:
        db.execute(
            """
            INSERT INTO account_monthly_usage (
                account_id, billing_month, total_calls_started, total_calls_ended,
                total_calls_failed, total_duration_seconds, total_cost_cents,
                overage_cost_cents, late_events_count, late_cost_cents,
                blocked_calls_count, last_reconciled_at, updated_at
            ) VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, NULL, ?)
            """,
            (account_id, billing_month, now_iso),
        )
        row = db.execute(
            "SELECT * FROM account_monthly_usage WHERE account_id = ? AND billing_month = ?",
            (account_id, billing_month),
        ).fetchone()
    return row


def _enqueue_retry_job(
    db: DbCursorWrapper,
    reference_id: str,
    job_type: str,
    account_id: str,
    payload: Dict[str, Any],
    now_iso: str,
) -> None:
    existing = db.execute(
        "SELECT reference_id FROM retry_jobs WHERE reference_id = ?", (reference_id,)
    ).fetchone()
    if existing is None:
        db.execute(
            """
            INSERT INTO retry_jobs (
                reference_id, job_type, account_id, payload_json,
                status, attempts, max_attempts, next_run_at, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 0, 5, ?, NULL, ?, ?)
            """,
            (reference_id, job_type, account_id, json.dumps(payload), now_iso, now_iso, now_iso),
        )
        METRICS["retry_jobs_enqueued_total"] += 1


def ingest_webhook_event(
    event: WebhookCallEvent,
    raw_body_str: str,
    signature_hex: str,
    now_override: datetime | None = None,
) -> Tuple[int, Dict[str, Any]]:
    now_utc = now_override or datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    with db_adapter.transaction() as db:
        # 1. Idempotency check by event_id
        dup = db.execute(
            "SELECT event_id FROM raw_webhook_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if dup is not None:
            METRICS["webhooks_duplicate_ignored_total"] += 1
            logger.info(
                json.dumps({
                    "event": "webhook.duplicate_ignored",
                    "event_id": event.event_id,
                    "call_id": event.call_id,
                    "account_id": event.account_id,
                })
            )
            return 200, {
                "status": "duplicate_ignored",
                "idempotent": True,
                "event_id": event.event_id,
                "call_id": event.call_id,
                "account_id": event.account_id,
            }

        # 2. Classify late arrival (~35 days in the past)
        age_delta = now_utc - event.timestamp
        late_days = max(0, int(age_delta.total_seconds() // 86400))
        event_billing_month = event.timestamp.strftime("%Y-%m")
        current_billing_month = now_utc.strftime("%Y-%m")
        is_late = (late_days >= LATE_EVENT_THRESHOLD_DAYS) or (event_billing_month < current_billing_month)

        account = _ensure_account(db, event.account_id, now_iso)
        usage = _ensure_monthly_usage(db, event.account_id, event_billing_month, now_iso)

        # Reserve the unique event ID before applying any side effects. RETURNING
        # keeps concurrent duplicate deliveries idempotent on both supported DBs.
        inserted = db.execute(
            """
            INSERT INTO raw_webhook_events (
                event_id, event_type, account_id, call_id, event_timestamp,
                received_at, billing_month, duration_seconds, cost_cents,
                destination_country, is_late, late_days, raw_payload,
                signature_sha256, processing_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processed')
            ON CONFLICT(event_id) DO NOTHING
            RETURNING event_id
            """,
            (
                event.event_id,
                event.event_type,
                event.account_id,
                event.call_id,
                event.timestamp.isoformat(),
                now_iso,
                event_billing_month,
                event.duration_seconds,
                event.cost_cents,
                event.destination_country,
                1 if is_late else 0,
                late_days,
                raw_body_str,
                signature_hex,
            ),
        ).fetchone()
        if inserted is None:
            METRICS["webhooks_duplicate_ignored_total"] += 1
            return 200, {
                "status": "duplicate_ignored", "idempotent": True,
                "event_id": event.event_id, "call_id": event.call_id,
                "account_id": event.account_id,
            }

        # 4. Out-of-order resilient CallSession state machine
        call_session = db.execute(
            "SELECT * FROM call_sessions WHERE account_id = ? AND call_id = ?", (event.account_id, event.call_id)
        ).fetchone()
        out_of_order_detected = False

        if call_session is None:
            if event.event_type == "call.ended":
                out_of_order_detected = True
                METRICS["events_out_of_order_resolved_total"] += 1
            initial_status = (
                "started"
                if event.event_type == "call.started"
                else ("ended" if event.event_type == "call.ended" else "failed")
            )
            db.execute(
                """
                INSERT INTO call_sessions (
                    call_id, account_id, billing_month, status,
                    started_event_id, ended_event_id, started_at, ended_at,
                    duration_seconds, cost_cents, destination_country,
                    arrived_out_of_order, is_late, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.call_id,
                    event.account_id,
                    event_billing_month,
                    initial_status,
                    event.event_id if event.event_type == "call.started" else None,
                    event.event_id if event.event_type in ("call.ended", "call.failed") else None,
                    event.timestamp.isoformat() if event.event_type == "call.started" else None,
                    event.timestamp.isoformat() if event.event_type in ("call.ended", "call.failed") else None,
                    event.duration_seconds if event.event_type == "call.ended" else 0,
                    event.cost_cents if event.event_type == "call.ended" else 0,
                    event.destination_country,
                    1 if out_of_order_detected else 0,
                    1 if is_late else 0,
                    now_iso,
                ),
            )
        else:
            if event.event_type == "call.started":
                # If call.ended already arrived first, keep terminal status & cost intact!
                already_terminal = call_session["status"] in ("ended", "failed")
                if already_terminal:
                    out_of_order_detected = True
                    METRICS["events_out_of_order_resolved_total"] += 1
                db.execute(
                    """
                    UPDATE call_sessions
                    SET started_event_id = ?,
                        started_at = ?,
                        arrived_out_of_order = ?,
                        updated_at = ?
                    WHERE account_id = ? AND call_id = ?
                    """,
                    (
                        event.event_id,
                        event.timestamp.isoformat(),
                        1 if already_terminal or call_session["arrived_out_of_order"] else 0,
                        now_iso,
                        event.account_id,
                        event.call_id,
                    ),
                )
            elif event.event_type == "call.ended":
                db.execute(
                    """
                    UPDATE call_sessions
                    SET status = 'ended',
                        ended_event_id = ?,
                        ended_at = ?,
                        duration_seconds = ?,
                        cost_cents = ?,
                        destination_country = ?,
                        updated_at = ?
                        WHERE account_id = ? AND call_id = ?
                    """,
                    (
                        event.event_id,
                        event.timestamp.isoformat(),
                        event.duration_seconds,
                        event.cost_cents,
                        event.destination_country,
                        now_iso,
                        event.account_id,
                        event.call_id,
                    ),
                )
            elif event.event_type == "call.failed":
                new_status = "ended" if call_session["status"] == "ended" else "failed"
                db.execute(
                    """
                    UPDATE call_sessions
                    SET status = ?,
                        ended_event_id = ?,
                        ended_at = ?,
                        updated_at = ?
                        WHERE account_id = ? AND call_id = ?
                    """,
                    (new_status, event.event_id, event.timestamp.isoformat(), now_iso, event.account_id, event.call_id),
                )

        # 5. Update integer usage ledger & evaluate monthly cap
        cap_admission = "allowed"
        total_started = usage["total_calls_started"]
        total_ended = usage["total_calls_ended"]
        total_failed = usage["total_calls_failed"]
        total_dur = usage["total_duration_seconds"]
        total_cost = usage["total_cost_cents"]
        late_cnt = usage["late_events_count"]
        late_cost = usage["late_cost_cents"]
        blocked_cnt = usage["blocked_calls_count"]

        if event.event_type == "call.started":
            total_started += 1
            active_usage = _ensure_monthly_usage(db, event.account_id, current_billing_month, now_iso)
            if active_usage["total_cost_cents"] >= account["monthly_cap_cents"] and not is_late and event_billing_month == current_billing_month:
                blocked_cnt += 1
                METRICS["cap_blocked_calls_total"] += 1
                cap_admission = "blocked_by_hard_cap"
                db.execute(
                    "UPDATE call_sessions SET status = 'blocked_by_cap' WHERE account_id = ? AND call_id = ? AND status = 'started'",
                    (event.account_id, event.call_id),
                )
        elif event.event_type == "call.failed":
            total_failed += 1
        elif event.event_type == "call.ended":
            total_ended += 1
            total_dur += event.duration_seconds
            total_cost += event.cost_cents
            if is_late:
                late_cnt += 1
                late_cost += event.cost_cents
                METRICS["events_late_flagged_total"] += 1
                _enqueue_retry_job(
                    db,
                    reference_id=f"late_{event.event_id}",
                    job_type="late_event_audit",
                    account_id=event.account_id,
                    payload={
                        "event_id": event.event_id,
                        "account_id": event.account_id,
                        "billing_month": event_billing_month,
                        "late_days": late_days,
                        "cost_cents": event.cost_cents,
                    },
                    now_iso=now_iso,
                )

        monthly_cap = account["monthly_cap_cents"]
        soft_threshold = (monthly_cap * account["soft_cap_warning_pct"]) // 100
        overage_cost = max(0, total_cost - monthly_cap)

        db.execute(
            """
            UPDATE account_monthly_usage
            SET total_calls_started = ?,
                total_calls_ended = ?,
                total_calls_failed = ?,
                total_duration_seconds = ?,
                total_cost_cents = ?,
                overage_cost_cents = ?,
                late_events_count = ?,
                late_cost_cents = ?,
                blocked_calls_count = ?,
                updated_at = ?
            WHERE account_id = ? AND billing_month = ?
            """,
            (
                total_started,
                total_ended,
                total_failed,
                total_dur,
                total_cost,
                overage_cost,
                late_cnt,
                late_cost,
                blocked_cnt,
                now_iso,
                event.account_id,
                event_billing_month,
            ),
        )

        is_capped = bool(account["is_capped"])
        cap_state = account["cap_state"]
        capped_at = account["capped_at"]

        if event_billing_month == current_billing_month:
            previous_is_capped = is_capped
            is_capped = total_cost >= monthly_cap
            if is_capped:
                cap_state = "HARD_CAP_EXCEEDED"
                capped_at = capped_at or now_iso
            elif total_cost >= soft_threshold:
                cap_state = "SOFT_CAP_WARNING"
                capped_at = None
            else:
                cap_state = "OK"
                capped_at = None
            if is_capped and not previous_is_capped:
                METRICS["cap_hard_breaches_total"] += 1
                _enqueue_retry_job(
                    db,
                    reference_id=f"cap_breach_{event.account_id}_{event_billing_month}",
                    job_type="cap_breach_alert",
                    account_id=event.account_id,
                    payload={
                        "account_id": event.account_id,
                        "billing_month": event_billing_month,
                        "total_cost_cents": total_cost,
                        "monthly_cap_cents": monthly_cap,
                        "overage_cost_cents": overage_cost,
                        "trigger_event_id": event.event_id,
                    },
                    now_iso=now_iso,
                )
            if not is_capped and total_cost >= soft_threshold and account["cap_state"] == "OK":
                cap_state = "SOFT_CAP_WARNING"
                METRICS["cap_soft_warnings_total"] += 1

        if event_billing_month == current_billing_month:
            db.execute(
                """
                UPDATE accounts
                SET is_capped = ?, cap_state = ?, capped_at = ?, updated_at = ?
                WHERE account_id = ?
                """,
                (1 if is_capped else 0, cap_state, capped_at, now_iso, event.account_id),
            )

    METRICS["webhooks_accepted_total"] += 1
    response_payload = {
        "status": "processed",
        "idempotent": False,
        "event_id": event.event_id,
        "call_id": event.call_id,
        "account_id": event.account_id,
        "billing_month": event_billing_month,
        "is_late": is_late,
        "late_days": late_days,
        "out_of_order_resolved": out_of_order_detected,
        "cap_state": cap_state,
        "is_capped": is_capped,
        "cap_admission": cap_admission,
        "monthly_cost_cents": total_cost,
        "monthly_cap_cents": monthly_cap,
        "overage_cost_cents": overage_cost,
    }
    logger.info(json.dumps({"event": "webhook.processed", **response_payload}))

    if cap_admission == "blocked_by_hard_cap" and HARD_CAP_HTTP_429_ON_START:
        return 429, response_payload
    return 200, response_payload


def process_pending_retry_jobs(simulate_failure_once: bool = False) -> Dict[str, int]:
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    processed = 0
    completed = 0
    requeued = 0
    dead_lettered = 0

    with db_adapter.transaction() as db:
        jobs = db.execute(
            "SELECT * FROM retry_jobs WHERE status = 'pending' AND next_run_at <= ?",
            (now_iso,),
        ).fetchall()

        for job in jobs:
            processed += 1
            attempts = job["attempts"] + 1
            if simulate_failure_once and attempts == 1:
                backoff_sec = min(60, 2 ** attempts)
                next_run = (now_utc + timedelta(seconds=backoff_sec)).isoformat()
                db.execute(
                    """
                    UPDATE retry_jobs
                    SET attempts = ?, last_error = ?, next_run_at = ?, updated_at = ?
                    WHERE reference_id = ?
                    """,
                    (
                        attempts,
                        "Simulated transient downstream timeout on attempt 1",
                        next_run,
                        now_iso,
                        job["reference_id"],
                    ),
                )
                requeued += 1
                continue

            if attempts > job["max_attempts"]:
                db.execute(
                    "UPDATE retry_jobs SET attempts = ?, status = 'dead_letter', updated_at = ? WHERE reference_id = ?",
                    (attempts, now_iso, job["reference_id"]),
                )
                dead_lettered += 1
            else:
                db.execute(
                    "UPDATE retry_jobs SET attempts = ?, status = 'completed', last_error = NULL, updated_at = ? WHERE reference_id = ?",
                    (attempts, now_iso, job["reference_id"]),
                )
                completed += 1
                METRICS["retry_jobs_completed_total"] += 1

    return {
        "processed": processed,
        "completed": completed,
        "requeued": requeued,
        "dead_lettered": dead_lettered,
    }


def reconcile_usage_from_raw_events() -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    with db_adapter.transaction() as db:
        raw_events = db.execute(
            "SELECT * FROM raw_webhook_events WHERE processing_status = 'processed'"
        ).fetchall()

        expected_buckets: Dict[Tuple[str, str], Dict[str, int]] = {}
        for ev in raw_events:
            key = (ev["account_id"], ev["billing_month"])
            if key not in expected_buckets:
                expected_buckets[key] = {
                    "total_calls_started": 0,
                    "total_calls_ended": 0,
                    "total_calls_failed": 0,
                    "total_duration_seconds": 0,
                    "total_cost_cents": 0,
                    "late_events_count": 0,
                    "late_cost_cents": 0,
                }
            b = expected_buckets[key]
            if ev["event_type"] == "call.started":
                b["total_calls_started"] += 1
            elif ev["event_type"] == "call.failed":
                b["total_calls_failed"] += 1
            elif ev["event_type"] == "call.ended":
                b["total_calls_ended"] += 1
                b["total_duration_seconds"] += int(ev["duration_seconds"])
                b["total_cost_cents"] += int(ev["cost_cents"])
                if bool(ev["is_late"]):
                    b["late_events_count"] += 1
                    b["late_cost_cents"] += int(ev["cost_cents"])

        # Existing buckets with no surviving raw events must also be cleared.
        existing_buckets = db.execute(
            "SELECT account_id, billing_month FROM account_monthly_usage"
        ).fetchall()
        for bucket in existing_buckets:
            expected_buckets.setdefault(
                (bucket["account_id"], bucket["billing_month"]),
                {
                    "total_calls_started": 0, "total_calls_ended": 0,
                    "total_calls_failed": 0, "total_duration_seconds": 0,
                    "total_cost_cents": 0, "late_events_count": 0,
                    "late_cost_cents": 0,
                },
            )

        drifts_detected = 0
        drifts_healed = 0
        drift_details: List[Dict[str, Any]] = []

        for (account_id, billing_month), exp in expected_buckets.items():
            account = _ensure_account(db, account_id, now_iso)
            usage = _ensure_monthly_usage(db, account_id, billing_month, now_iso)

            cost_diff = exp["total_cost_cents"] - usage["total_cost_cents"]
            dur_diff = exp["total_duration_seconds"] - usage["total_duration_seconds"]
            ended_diff = exp["total_calls_ended"] - usage["total_calls_ended"]
            started_diff = exp["total_calls_started"] - usage["total_calls_started"]
            failed_diff = exp["total_calls_failed"] - usage["total_calls_failed"]
            late_count_diff = exp["late_events_count"] - usage["late_events_count"]
            late_cost_diff = exp["late_cost_cents"] - usage["late_cost_cents"]

            overage = max(0, exp["total_cost_cents"] - account["monthly_cap_cents"])

            if any((cost_diff, dur_diff, ended_diff, started_diff, failed_diff, late_count_diff, late_cost_diff)):
                drifts_detected += 1
                drift_details.append({
                    "account_id": account_id,
                    "billing_month": billing_month,
                    "previous_cost_cents": usage["total_cost_cents"],
                    "recomputed_cost_cents": exp["total_cost_cents"],
                    "cost_drift_cents": cost_diff,
                    "previous_duration_seconds": usage["total_duration_seconds"],
                    "recomputed_duration_seconds": exp["total_duration_seconds"],
                })
                drifts_healed += 1
                METRICS["reconciliation_drifts_healed_total"] += 1

            db.execute(
                """
                UPDATE account_monthly_usage
                SET total_calls_started = ?,
                    total_calls_ended = ?,
                    total_calls_failed = ?,
                    total_duration_seconds = ?,
                    total_cost_cents = ?,
                    overage_cost_cents = ?,
                    late_events_count = ?,
                    late_cost_cents = ?,
                    last_reconciled_at = ?,
                    updated_at = ?
                WHERE account_id = ? AND billing_month = ?
                """,
                (
                    exp["total_calls_started"],
                    exp["total_calls_ended"],
                    exp["total_calls_failed"],
                    exp["total_duration_seconds"],
                    exp["total_cost_cents"],
                    overage,
                    exp["late_events_count"],
                    exp["late_cost_cents"],
                    now_iso,
                    now_iso,
                    account_id,
                    billing_month,
                ),
            )

            if billing_month == now_utc.strftime("%Y-%m"):
                soft_threshold = (account["monthly_cap_cents"] * account["soft_cap_warning_pct"]) // 100
                if exp["total_cost_cents"] >= account["monthly_cap_cents"]:
                    is_capped, cap_state = 1, "HARD_CAP_EXCEEDED"
                    capped_at = account["capped_at"] or now_iso
                elif exp["total_cost_cents"] >= soft_threshold:
                    is_capped, cap_state, capped_at = 0, "SOFT_CAP_WARNING", None
                else:
                    is_capped, cap_state, capped_at = 0, "OK", None
                db.execute(
                    "UPDATE accounts SET is_capped = ?, cap_state = ?, capped_at = ?, updated_at = ? WHERE account_id = ?",
                    (is_capped, cap_state, capped_at, now_iso, account_id),
                )

        # Accounts with no usage in the current period must not carry last
        # month's hard cap forward into the new month.
        current_month = now_utc.strftime("%Y-%m")
        accounts = db.execute("SELECT * FROM accounts").fetchall()
        for account in accounts:
            current = expected_buckets.get((account["account_id"], current_month))
            if current is None:
                db.execute(
                    "UPDATE accounts SET is_capped = 0, cap_state = 'OK', capped_at = NULL, updated_at = ? WHERE account_id = ?",
                    (now_iso, account["account_id"]),
                )

        db.execute(
            """
            INSERT INTO reconciliation_runs (
                ran_at, buckets_checked, drifts_detected, drifts_healed, details_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (now_iso, len(expected_buckets), drifts_detected, drifts_healed, json.dumps(drift_details)),
        )

    METRICS["reconciliation_runs_total"] += 1
    return {
        "ran_at": now_iso,
        "buckets_checked": len(expected_buckets),
        "drifts_detected": drifts_detected,
        "drifts_healed": drifts_healed,
        "details": drift_details,
    }

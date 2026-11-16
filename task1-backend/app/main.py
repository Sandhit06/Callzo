"""FastAPI application entrypoint for Callzo Task 1 — Usage Metering Service."""
import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

from .database import db_adapter
from .schemas import CapConfigUpdate, SchemaValidationError, WebhookCallEvent
from .services import (
    METRICS,
    ingest_webhook_event,
    process_pending_retry_jobs,
    reconcile_usage_from_raw_events,
    verify_webhook_signature,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
)
logger = logging.getLogger("callzo.metering")

ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")


async def _background_worker_loop(stop_event: asyncio.Event):
    tick = 0
    while not stop_event.is_set():
        try:
            await asyncio.sleep(5.0)
            tick += 1
            process_pending_retry_jobs()
            if tick % 12 == 0:
                reconcile_usage_from_raw_events()
        except asyncio.CancelledError:
            break
        except Exception as exc:  # pragma: no cover
            logger.error(json.dumps({"event": "background_worker.error", "error": str(exc)}))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_adapter.init_schema()
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(_background_worker_loop(stop_event))
    yield
    stop_event.set()
    worker_task.cancel()


app = FastAPI(
    title="Callzo Usage Metering Service",
    version="1.0.0",
    description="Task 1 Backend: HMAC-verified telephony webhook ingestion, out-of-order call reconciliation, and monthly cap enforcement.",
    lifespan=lifespan,
)

def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> str:
    token = x_api_key
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token or not ADMIN_API_KEY or not hmac.compare_digest(token, ADMIN_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Provide X-API-Key header or Bearer token.",
        )
    return token


@app.get("/health")
def health_check():
    with db_adapter.transaction() as db:
        ev_cnt = db.execute("SELECT COUNT(*) AS cnt FROM raw_webhook_events").fetchone()["cnt"]
        ac_cnt = db.execute("SELECT COUNT(*) AS cnt FROM accounts").fetchone()["cnt"]
    return {
        "status": "ok",
        "service": "callzo-usage-metering-api",
        "raw_events_ingested": ev_cnt,
        "accounts_tracked": ac_cnt,
    }


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    lines = [
        f"# HELP callzo_{k} Operational counter for {k}\n# TYPE callzo_{k} counter\ncallzo_{k} {v}"
        for k, v in METRICS.items()
    ]
    return "\n".join(lines) + "\n"


@app.post("/webhooks/calls")
async def receive_call_webhook(request: Request):
    METRICS["webhooks_received_total"] += 1
    raw_body = await request.body()
    signature_header = request.headers.get("X-Signature")

    # 1. Verify HMAC-SHA256 over raw bytes BEFORE touching JSON parser
    if not verify_webhook_signature(raw_body, signature_header):
        METRICS["webhooks_rejected_signature_total"] += 1
        logger.warning(
            json.dumps({
                "event": "webhook.rejected_invalid_signature",
                "has_signature_header": bool(signature_header),
                "body_bytes": len(raw_body),
            })
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_signature", "detail": "Missing or invalid X-Signature HMAC-SHA256 header."},
        )

    # 2. Reject empty or malformed JSON with 400
    if not raw_body or not raw_body.strip():
        METRICS["webhooks_rejected_malformed_total"] += 1
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "empty_body", "detail": "Request body cannot be empty."},
        )

    try:
        raw_text = raw_body.decode("utf-8")
        parsed_json = json.loads(raw_text)
    except Exception as exc:
        METRICS["webhooks_rejected_malformed_total"] += 1
        logger.warning(json.dumps({"event": "webhook.malformed_json", "error": str(exc)}))
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "malformed_json", "detail": "Invalid or truncated JSON payload."},
        )

    if not isinstance(parsed_json, dict):
        METRICS["webhooks_rejected_malformed_total"] += 1
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_payload_type", "detail": "Webhook payload must be a JSON object."},
        )

    # 3. Validate strict schema (unknown event_type, float cost_cents, missing fields -> 422)
    try:
        validated_event = WebhookCallEvent.validate_strict_dict(parsed_json)
    except SchemaValidationError as exc:
        METRICS["webhooks_rejected_malformed_total"] += 1
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "schema_validation_failed",
                "detail": exc.errors,
            },
        )

    # 4. Ingest atomically
    status_code, payload = ingest_webhook_event(
        event=validated_event,
        raw_body_str=raw_text,
        signature_hex=signature_header.strip(),
    )
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/api/v1/accounts", dependencies=[Depends(require_api_key)])
def list_accounts_usage():
    with db_adapter.transaction() as db:
        accounts = db.execute("SELECT * FROM accounts ORDER BY account_id").fetchall()
        usages = db.execute(
            "SELECT * FROM account_monthly_usage ORDER BY account_id, billing_month DESC"
        ).fetchall()

    usage_by_acct = {}
    for u in usages:
        usage_by_acct.setdefault(u["account_id"], []).append(u)

    return {
        "accounts": [
            {
                "account_id": a["account_id"],
                "monthly_cap_cents": a["monthly_cap_cents"],
                "is_capped": bool(a["is_capped"]),
                "cap_state": a["cap_state"],
                "capped_at": a["capped_at"],
                "monthly_usage": usage_by_acct.get(a["account_id"], []),
            }
            for a in accounts
        ],
        "metrics": METRICS,
    }


@app.get("/api/v1/accounts/{account_id}/usage", dependencies=[Depends(require_api_key)])
def get_account_usage(account_id: str):
    with db_adapter.transaction() as db:
        acct = db.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        if acct is None:
            raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found")
        usages = db.execute(
            "SELECT * FROM account_monthly_usage WHERE account_id = ? ORDER BY billing_month DESC",
            (account_id,),
        ).fetchall()
        calls = db.execute(
            "SELECT * FROM call_sessions WHERE account_id = ? ORDER BY updated_at DESC LIMIT 100",
            (account_id,),
        ).fetchall()

    return {
        "account_id": acct["account_id"],
        "monthly_cap_cents": acct["monthly_cap_cents"],
        "is_capped": bool(acct["is_capped"]),
        "cap_state": acct["cap_state"],
        "capped_at": acct["capped_at"],
        "monthly_buckets": usages,
        "recent_calls": [
            {
                **c,
                "arrived_out_of_order": bool(c["arrived_out_of_order"]),
                "is_late": bool(c["is_late"]),
            }
            for c in calls
        ],
    }


@app.put("/api/v1/accounts/{account_id}/cap", dependencies=[Depends(require_api_key)])
def update_account_cap(account_id: str, payload: CapConfigUpdate):
    now_iso = datetime.now(timezone.utc).isoformat()
    with db_adapter.transaction() as db:
        acct = db.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        if acct is None:
            db.execute(
                """
                INSERT INTO accounts (
                    account_id, monthly_cap_cents, soft_cap_warning_pct,
                    is_capped, cap_state, capped_at, created_at, updated_at
                ) VALUES (?, ?, 80, 0, 'OK', NULL, ?, ?)
                """,
                (account_id, payload.monthly_cap_cents, now_iso, now_iso),
            )
        else:
            db.execute(
                "UPDATE accounts SET monthly_cap_cents = ?, updated_at = ? WHERE account_id = ?",
                (payload.monthly_cap_cents, now_iso, account_id),
            )
    reconcile_usage_from_raw_events()
    with db_adapter.transaction() as db:
        updated = db.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
    return {
        "account_id": updated["account_id"],
        "monthly_cap_cents": updated["monthly_cap_cents"],
        "is_capped": bool(updated["is_capped"]),
        "cap_state": updated["cap_state"],
    }


@app.post("/api/v1/reconcile", dependencies=[Depends(require_api_key)])
def trigger_reconciliation():
    return reconcile_usage_from_raw_events()


@app.post("/api/v1/retries/run", dependencies=[Depends(require_api_key)])
def trigger_retry_worker():
    return process_pending_retry_jobs()

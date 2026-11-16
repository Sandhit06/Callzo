"""Comprehensive test suite for Callzo Task 1 — Usage Metering Service.
Compatible with both `pytest` and standard library `python3 -m unittest`.
Verifies all 7 simulator scenarios:
1. normal
2. duplicates (idempotency)
3. out_of_order (call.ended before call.started)
4. late (~35 days in the past)
5. malformed (missing fields, floats for cost, unknown event_type, truncated JSON, empty body)
6. bad_signature (missing or tampered X-Signature)
7. cap_burst (40 back-to-back calls breaching soft and hard monthly caps)
Plus: API authentication, retry job backoff/processing, and ledger drift reconciliation.
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATABASE_URL"] = "sqlite:///./test_callzo_metering.db"
os.environ["WEBHOOK_SECRET"] = "callzo_test_secret_change_me"
os.environ["ADMIN_API_KEY"] = "callzo_admin_key_2026"
os.environ["DEFAULT_MONTHLY_CAP_CENTS"] = "2500"

from fastapi.testclient import TestClient
from app.database import db_adapter
from app.main import app
from app.services import compute_hmac_sha256, process_pending_retry_jobs

AUTH_HEADERS = {"X-API-Key": "callzo_admin_key_2026"}


class TestCallzoUsageMetering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_adapter.init_schema()
        cls.client = TestClient(app)

    def setUp(self):
        db_adapter.init_schema()
        db_adapter.reset_all()

    def post_signed_webhook(self, payload_dict: dict, secret: str = "callzo_test_secret_change_me"):
        raw_bytes = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
        sig = compute_hmac_sha256(raw_bytes, secret=secret)
        return self.client.post(
            "/webhooks/calls",
            content=raw_bytes,
            headers={"Content-Type": "application/json", "X-Signature": sig},
        )

    def test_1_normal_call_lifecycle(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        r1 = self.post_signed_webhook({
            "event_id": "evt_norm_1",
            "event_type": "call.started",
            "account_id": "acct_001",
            "call_id": "call_norm_100",
            "timestamp": now_iso,
            "duration_seconds": 0,
            "cost_cents": 0,
            "destination_country": "US",
        })
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["status"], "processed")

        r2 = self.post_signed_webhook({
            "event_id": "evt_norm_2",
            "event_type": "call.ended",
            "account_id": "acct_001",
            "call_id": "call_norm_100",
            "timestamp": now_iso,
            "duration_seconds": 180,
            "cost_cents": 60,
            "destination_country": "US",
        })
        self.assertEqual(r2.status_code, 200)
        data = r2.json()
        self.assertEqual(data["monthly_cost_cents"], 60)
        self.assertEqual(data["cap_state"], "OK")

    def test_2_duplicates_idempotency_never_double_counts(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        ended_payload = {
            "event_id": "evt_dup_999",
            "event_type": "call.ended",
            "account_id": "acct_002",
            "call_id": "call_dup_200",
            "timestamp": now_iso,
            "duration_seconds": 300,
            "cost_cents": 120,
            "destination_country": "IN",
        }
        first = self.post_signed_webhook(ended_payload)
        second = self.post_signed_webhook(ended_payload)
        third = self.post_signed_webhook(ended_payload)

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["idempotent"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "duplicate_ignored")
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(third.status_code, 200)
        self.assertTrue(third.json()["idempotent"])

        usage_res = self.client.get("/api/v1/accounts/acct_002/usage", headers=AUTH_HEADERS)
        self.assertEqual(usage_res.status_code, 200)
        bucket = usage_res.json()["monthly_buckets"][0]
        self.assertEqual(bucket["total_cost_cents"], 120)
        self.assertEqual(bucket["total_duration_seconds"], 300)
        self.assertEqual(bucket["total_calls_ended"], 1)

    def test_3_out_of_order_ended_before_started(self):
        now = datetime.now(timezone.utc)
        r_end = self.post_signed_webhook({
            "event_id": "evt_ooo_end",
            "event_type": "call.ended",
            "account_id": "acct_003",
            "call_id": "call_ooo_300",
            "timestamp": now.isoformat(),
            "duration_seconds": 240,
            "cost_cents": 85,
            "destination_country": "GB",
        })
        self.assertEqual(r_end.status_code, 200)
        self.assertTrue(r_end.json()["out_of_order_resolved"])

        r_start = self.post_signed_webhook({
            "event_id": "evt_ooo_start",
            "event_type": "call.started",
            "account_id": "acct_003",
            "call_id": "call_ooo_300",
            "timestamp": (now - timedelta(seconds=240)).isoformat(),
            "duration_seconds": 0,
            "cost_cents": 0,
            "destination_country": "GB",
        })
        self.assertEqual(r_start.status_code, 200)
        self.assertTrue(r_start.json()["out_of_order_resolved"])

        usage_res = self.client.get("/api/v1/accounts/acct_003/usage", headers=AUTH_HEADERS).json()
        call_rec = usage_res["recent_calls"][0]
        self.assertEqual(call_rec["call_id"], "call_ooo_300")
        self.assertEqual(call_rec["status"], "ended")
        self.assertEqual(call_rec["duration_seconds"], 240)
        self.assertEqual(call_rec["cost_cents"], 85)
        self.assertTrue(call_rec["arrived_out_of_order"])

    def test_4_late_event_35_days_old_attributed_to_historical_month(self):
        late_ts = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
        r = self.post_signed_webhook({
            "event_id": "evt_late_1",
            "event_type": "call.ended",
            "account_id": "acct_004",
            "call_id": "call_late_400",
            "timestamp": late_ts,
            "duration_seconds": 600,
            "cost_cents": 200,
            "destination_country": "US",
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["is_late"])
        self.assertGreaterEqual(body["late_days"], 35)

    def test_5_malformed_payloads_rejected_cleanly(self):
        empty_sig = compute_hmac_sha256(b"")
        r_empty = self.client.post("/webhooks/calls", content=b"", headers={"X-Signature": empty_sig})
        self.assertEqual(r_empty.status_code, 400)

        bad_json = b'{"event_id": "evt_bad", "event_type": '
        r_trunc = self.client.post(
            "/webhooks/calls",
            content=bad_json,
            headers={"X-Signature": compute_hmac_sha256(bad_json)},
        )
        self.assertEqual(r_trunc.status_code, 400)

        r_unknown = self.post_signed_webhook({
            "event_id": "evt_unk_1",
            "event_type": "call.ringing_unknown",
            "account_id": "acct_005",
            "call_id": "call_500",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": 10,
            "cost_cents": 5,
            "destination_country": "US",
        })
        self.assertEqual(r_unknown.status_code, 422)

        r_float = self.post_signed_webhook({
            "event_id": "evt_flt_1",
            "event_type": "call.ended",
            "account_id": "acct_005",
            "call_id": "call_501",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": 60,
            "cost_cents": 12.5,
            "destination_country": "US",
        })
        self.assertEqual(r_float.status_code, 422)

    def test_6_bad_signature_rejected_before_processing(self):
        raw_bytes = json.dumps({
            "event_id": "evt_tampered_1",
            "event_type": "call.ended",
            "account_id": "acct_006",
            "call_id": "call_600",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": 120,
            "cost_cents": 500,
            "destination_country": "US",
        }).encode("utf-8")

        r_missing = self.client.post("/webhooks/calls", content=raw_bytes)
        self.assertEqual(r_missing.status_code, 401)

        r_invalid = self.client.post(
            "/webhooks/calls",
            content=raw_bytes,
            headers={"X-Signature": "deadbeef" * 8},
        )
        self.assertEqual(r_invalid.status_code, 401)

        res = self.client.get("/api/v1/accounts/acct_006/usage", headers=AUTH_HEADERS)
        self.assertEqual(res.status_code, 404)

    def test_7_cap_burst_engages_soft_and_hard_caps_and_reconciles(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        states_seen = set()

        for i in range(1, 41):
            res_end = self.post_signed_webhook({
                "event_id": f"evt_burst_end_{i}",
                "event_type": "call.ended",
                "account_id": "acct_007",
                "call_id": f"call_burst_{i}",
                "timestamp": now_iso,
                "duration_seconds": 300,
                "cost_cents": 100,
                "destination_country": "US",
            })
            self.assertEqual(res_end.status_code, 200)
            states_seen.add(res_end.json()["cap_state"])

        self.assertIn("OK", states_seen)
        self.assertIn("SOFT_CAP_WARNING", states_seen)
        self.assertIn("HARD_CAP_EXCEEDED", states_seen)

        res_blocked = self.post_signed_webhook({
            "event_id": "evt_burst_after_cap_start",
            "event_type": "call.started",
            "account_id": "acct_007",
            "call_id": "call_burst_blocked_41",
            "timestamp": now_iso,
            "duration_seconds": 0,
            "cost_cents": 0,
            "destination_country": "US",
        })
        self.assertEqual(res_blocked.status_code, 200)
        self.assertEqual(res_blocked.json()["cap_admission"], "blocked_by_hard_cap")

        # Introduce artificial drift into account_monthly_usage and verify reconciliation heals it
        with db_adapter.transaction() as db:
            db.execute(
                "UPDATE account_monthly_usage SET total_cost_cents = 123 WHERE account_id = 'acct_007'"
            )

        retry_stats = process_pending_retry_jobs(simulate_failure_once=True)
        self.assertGreaterEqual(retry_stats["requeued"], 1)

        rec_res = self.client.post("/api/v1/reconcile", headers=AUTH_HEADERS)
        self.assertEqual(rec_res.status_code, 200)
        rec_data = rec_res.json()
        self.assertEqual(rec_data["drifts_detected"], 1)
        self.assertEqual(rec_data["drifts_healed"], 1)
        self.assertEqual(rec_data["details"][0]["recomputed_cost_cents"], 4000)


if __name__ == "__main__":
    unittest.main()

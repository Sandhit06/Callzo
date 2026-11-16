# Task 1 — Callzo Usage Metering Service (Backend)

A telephony usage metering service built with **Python 3.11, FastAPI, PostgreSQL 16, and explicit parameterized SQL**. It verifies signatures over raw request bytes, reconciles out-of-order and duplicate delivery, enforces the current billing month's integer-cent cap, and heals ledger drift through periodic reconciliation.

---

## 1. Quick Start (`docker compose up`)

From inside the `task1-backend/` directory, run:

```bash
docker compose up --build
```

The supplied webhook simulator is published for `linux/arm64`, so Compose requests that platform explicitly. Docker Desktop on x64 Windows uses ARM emulation to run it.

What happens automatically on startup:
1. **`postgres` (`postgres:16-alpine`)** boots and passes its `pg_isready` health check.
2. **`api` (`FastAPI` on port `8000`)** initializes the schema (`accounts`, `raw_webhook_events`, `call_sessions`, `account_monthly_usage`, `retry_jobs`, `reconciliation_runs`), starts the async background retry & reconciliation worker, and passes its `/health` check.
3. **`simulator` (`meetrj/callzo-webhook-simulator`)** fires `--scenario all` (`normal`, `duplicates`, `out_of_order`, `late`, `malformed`, `bad_signature`, `cap_burst`) against `http://api:8000/webhooks/calls`.

To run a single simulator scenario while inspecting logs:

```yaml
# In docker-compose.yml under the simulator service:
command: ["--url", "http://api:8000/webhooks/calls", "--scenario", "cap_burst"]
```

### Querying Authenticated API Endpoints

All administrative endpoints require `X-API-Key: callzo_admin_key_2026` (or `Authorization: Bearer callzo_admin_key_2026`), configured via `ADMIN_API_KEY`:

```bash
# 1. Health check (public)
curl -s http://localhost:8000/health | jq

# 2. Prometheus metrics (public)
curl -s http://localhost:8000/metrics

# 3. List all accounts, cap states, and monthly integer usage buckets
curl -s -H "X-API-Key: callzo_admin_key_2026" http://localhost:8000/api/v1/accounts | jq

# 4. Inspect a single account's usage buckets and recent call sessions
curl -s -H "X-API-Key: callzo_admin_key_2026" http://localhost:8000/api/v1/accounts/acct_001/usage | jq

# 5. Trigger an immediate raw-events-to-ledger reconciliation pass
curl -s -X POST -H "X-API-Key: callzo_admin_key_2026" http://localhost:8000/api/v1/reconcile | jq
```

---

## 2. Architecture & Database Schema Justification

All monetary values (`cost_cents`, `monthly_cap_cents`, `overage_cost_cents`, `late_cost_cents`) and call durations (`duration_seconds`) are stored and computed strictly as **64-bit/32-bit integers** (`INTEGER`). Floating-point types are forbidden at both the Pydantic validation layer and the SQL schema layer to prevent IEEE-754 rounding drift.

| Table | Primary / Unique Key | Purpose & Justification |
| :--- | :--- | :--- |
| `raw_webhook_events` | `UNIQUE (event_id)` | Immutable append-only audit log of every valid webhook payload received. Enforces idempotency at the database level so duplicate deliveries (`duplicates` scenario) can never double-count usage even under concurrent requests. |
| `call_sessions` | `PRIMARY KEY (call_id)` | State-machine table tracking each call (`started` → `ended` / `failed` / `blocked_by_cap`). Resolves `out_of_order` deliveries where `call.ended` arrives before `call.started`. |
| `account_monthly_usage` | `UNIQUE (account_id, billing_month)` | Denormalized integer ledger storing `total_cost_cents`, `total_duration_seconds`, `overage_cost_cents`, `late_cost_cents`, and call counts per `YYYY-MM` bucket for $O(1)$ cap checks and dashboard reads. |
| `accounts` | `PRIMARY KEY (account_id)` | Stores per-account `monthly_cap_cents` (default `2500` cents = `$25.00`), `cap_state` (`OK`, `SOFT_CAP_WARNING`, `HARD_CAP_EXCEEDED`), and `capped_at`. |
| `retry_jobs` | `UNIQUE (reference_id)` | Persistent background queue with exponential backoff (`2^attempts` seconds, up to `max_attempts = 5` before `dead_letter`) for cap-breach alerts and late-event audit hooks. |
| `reconciliation_runs` | `PRIMARY KEY (id)` | Audit history of periodic reconciliation jobs comparing `SUM(raw_webhook_events)` against `account_monthly_usage` and healing any detected drift. |

---

## 3. How Each Simulator Scenario Is Handled

1. **`bad_signature` (HMAC-SHA256 Verification Over Raw Bytes)**:
   - Before parsing JSON or touching the database, `POST /webhooks/calls` reads `raw_body = await request.body()` and computes `HMAC-SHA256(WEBHOOK_SECRET, raw_body)`.
   - Compares against `X-Signature` using constant-time `hmac.compare_digest`. Missing or tampered signatures return `401 Unauthorized` immediately and increment `callzo_webhooks_rejected_signature_total`.
2. **`malformed` (Strict Input Validation)**:
   - Empty bodies and truncated/invalid JSON return `400 Bad Request`.
   - Unknown `event_type` values (anything outside `call.started`, `call.ended`, `call.failed`), missing required fields, negative numbers, or float values for `cost_cents`/`duration_seconds` are rejected by Pydantic v2 strict validators with `422 Unprocessable Entity`.
3. **`duplicates` (Idempotent Ingestion)**:
   - The simulator resends identical `event_id` payloads 2–3 times.
   - Inside an atomic database transaction, our service checks `raw_webhook_events.event_id` (backed by a `UNIQUE` index). Duplicate deliveries return `200 OK` with `{"status": "duplicate_ignored", "idempotent": true}` and never increment `account_monthly_usage`.
4. **`out_of_order` (`call.ended` Arriving Before `call.started`)**:
   - Both events share `call_id`. When `call.ended` arrives first, `call_sessions` creates the record in terminal `status = "ended"` with its `duration_seconds` and `cost_cents`, and marks `arrived_out_of_order = True`.
   - When the matching `call.started` arrives later (carrying `duration_seconds = 0, cost_cents = 0`), the state machine populates `started_at` and `started_event_id` **without** regressing `status` back to `started` and **without** overwriting `duration_seconds` or `cost_cents`.
5. **`late` (Events Timestamped ~35 Days in the Past)**:
   - **Stated Policy**: Events arriving more than `LATE_EVENT_THRESHOLD_DAYS` (30 days) after their `timestamp`—or belonging to a prior `YYYY-MM` billing month—are ingested into `raw_webhook_events` with `is_late = True`, attributed to their **original occurrence `billing_month` (`YYYY-MM`)**, tracked separately in `late_events_count` / `late_cost_cents`, and enqueued as a `late_event_audit` job in `retry_jobs`.
   - **Why**: Attributing a 35-day-old replay to the *current* month would unexpectedly trip a customer's active monthly cap for calls that happened last billing cycle. Attributing to the event's `YYYY-MM` bucket while flagging `late_cost_cents` preserves exact financial auditability for prior-month adjustment invoices without disrupting current-month live traffic.
6. **`cap_burst` (~40 Back-to-Back Long Calls & Monthly Cap Boundary Behavior)**:
   - **Two-Tier Cap Policy (Soft Warning at 80% + Hard Enforcement at 100%)**:
     - **Soft Cap (`>= 80%` of `monthly_cap_cents`)**: `cap_state` transitions from `OK` to `SOFT_CAP_WARNING`. Calls continue normally.
     - **Hard Cap Boundary (`>= 100%` of `monthly_cap_cents`)**: `cap_state` transitions to `HARD_CAP_EXCEEDED` and `is_capped = True`.
     - **In-flight `call.ended` events at/after the boundary**: Always recorded in `raw_webhook_events` and `account_monthly_usage` (with `overage_cost_cents = total_cost_cents - monthly_cap_cents`) because a `call.ended` webhook means the telephony carrier already completed the call and incurred cost. An idempotent `cap_breach_alert` job is enqueued in `retry_jobs`.
     - **Subsequent `call.started` events after Hard Cap is reached**: Marked with `cap_admission = "blocked_by_hard_cap"` (`call_sessions.status = "blocked_by_cap"`) and counted in `blocked_calls_count` (or optionally returns HTTP `429` if `HARD_CAP_HTTP_429_ON_START=true`).

---

## 4. Trade-offs & What We Would Build Next

- **Synchronous Transactional Ingestion vs. Kafka/Redis Stream Buffering**: We chose synchronous ACID transactions in Postgres because a single Postgres instance easily handles thousands of webhooks/sec while giving immediate read-after-write consistency for cap enforcement. At 50,000+ webhooks/sec, we would front the endpoint with Redis Streams / Kafka and batch-commit to Postgres.
- **Next Steps with More Time**:
  1. Add Alembic database migrations and row-level locking (`SELECT ... FOR UPDATE`) stress benchmarks under 100-worker concurrency.
  2. Add outbound webhook delivery signing for customer `cap_breach_alert` notifications using `httpx.AsyncClient`.
  3. Add multi-currency rate tables by `destination_country`.

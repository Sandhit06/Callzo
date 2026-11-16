"""Strict validation schemas for Callzo Usage Metering Service.
Compatible with both Pydantic v1 and Pydantic v2.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Literal
from pydantic import BaseModel, Field

VALID_EVENT_TYPES = {"call.started", "call.ended", "call.failed"}


class SchemaValidationError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("Webhook payload failed schema validation")
        self.errors = errors


class WebhookCallEvent(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["call.started", "call.ended", "call.failed"]
    account_id: str = Field(..., min_length=1, max_length=64)
    call_id: str = Field(..., min_length=1, max_length=128)
    timestamp: datetime
    duration_seconds: int = Field(..., ge=0)
    cost_cents: int = Field(..., ge=0)
    destination_country: str = Field(..., min_length=2, max_length=8)

    @classmethod
    def validate_strict_dict(cls, payload: Dict[str, Any]) -> "WebhookCallEvent":
        errors: list[dict[str, Any]] = []
        required_fields = [
            "event_id",
            "event_type",
            "account_id",
            "call_id",
            "timestamp",
            "duration_seconds",
            "cost_cents",
            "destination_country",
        ]
        for req in required_fields:
            if req not in payload or payload[req] is None:
                errors.append({"field": req, "msg": f"Missing required field '{req}'"})

        extra_keys = set(payload.keys()) - set(required_fields)
        if extra_keys:
            errors.append({"field": sorted(extra_keys)[0], "msg": "Unexpected extra field(s) not allowed"})

        if errors:
            raise SchemaValidationError(errors)

        # Enforce string types
        for str_field in ("event_id", "event_type", "account_id", "call_id", "destination_country"):
            val = payload.get(str_field)
            if not isinstance(val, str) or not val.strip():
                errors.append({"field": str_field, "msg": f"Field '{str_field}' must be a non-empty string"})

        if payload.get("event_type") not in VALID_EVENT_TYPES:
            errors.append({
                "field": "event_type",
                "msg": f"Unknown event_type '{payload.get('event_type')}'. Must be one of {sorted(VALID_EVENT_TYPES)}",
            })

        # Strictly integer duration_seconds and cost_cents (reject floats, bools, and strings!)
        for int_field in ("duration_seconds", "cost_cents"):
            val = payload.get(int_field)
            if isinstance(val, bool) or not isinstance(val, int):
                errors.append({
                    "field": int_field,
                    "msg": f"Field '{int_field}' must be a strict integer (floats and strings are forbidden)",
                })
            elif val < 0:
                errors.append({
                    "field": int_field,
                    "msg": f"Field '{int_field}' must be >= 0",
                })

        # Parse ISO-8601 timestamp
        raw_ts = payload.get("timestamp")
        parsed_dt = None
        if not isinstance(raw_ts, str) or not raw_ts.strip():
            errors.append({"field": "timestamp", "msg": "Field 'timestamp' must be an ISO-8601 string"})
        else:
            try:
                norm_ts = raw_ts.strip().replace("Z", "+00:00")
                parsed_dt = datetime.fromisoformat(norm_ts)
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                else:
                    parsed_dt = parsed_dt.astimezone(timezone.utc)
            except Exception:
                errors.append({"field": "timestamp", "msg": f"Invalid ISO-8601 timestamp: '{raw_ts}'"})

        if errors:
            raise SchemaValidationError(errors)

        return cls(
            event_id=payload["event_id"].strip(),
            event_type=payload["event_type"],
            account_id=payload["account_id"].strip(),
            call_id=payload["call_id"].strip(),
            timestamp=parsed_dt,
            duration_seconds=payload["duration_seconds"],
            cost_cents=payload["cost_cents"],
            destination_country=payload["destination_country"].strip(),
        )


class CapConfigUpdate(BaseModel):
    monthly_cap_cents: int = Field(..., ge=100, le=100_000_000)

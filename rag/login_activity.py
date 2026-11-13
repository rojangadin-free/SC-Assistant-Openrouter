"""Bounded successful sign-ins for the admin feed, not a permanent audit log."""

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from rag.store import JsonBlobStore

RETENTION_DAYS = 30
MAX_EVENTS = 1000
MAX_PAYLOAD_BYTES = 380 * 1024


def _store():
    return JsonBlobStore("login_activity", os.getenv("LOGIN_ACTIVITY_FILE", "login_activity.json"),
                         lambda: {"events": {}})


def _load() -> dict:
    return _store().load()


def record_login(*, uid: str, display_name: str, email: str) -> dict:
    """Record one verified sign-in; callers must isolate persistence failures."""
    if not all(isinstance(value, str) and value.strip() for value in (uid, display_name, email)):
        raise ValueError("Login activity requires a complete account identity.")

    # A conditional-write retry is still the same sign-in, not a new event.
    now = datetime.now(timezone.utc)
    event = {"id": uuid4().hex, "at": now.isoformat(timespec="microseconds"),
             "uid": uid, "display_name": display_name, "email": email}
    cutoff = now - timedelta(days=RETENTION_DAYS)

    def change(data):
        existing = data.get("events", {})
        if not isinstance(existing, dict):
            raise TypeError("Invalid login activity store.")
        recent = [(now, event)]
        for row in existing.values():
            if not isinstance(row, dict) or row.get("id") == event["id"]:
                continue
            if not all(isinstance(row.get(key), str) and row[key].strip()
                       for key in event):
                continue
            try:
                at = datetime.fromisoformat(row["at"])
            except ValueError:
                continue
            if at.tzinfo is not None and at >= cutoff:
                recent.append((at, {key: row[key] for key in event}))
        recent.sort(key=lambda entry: entry[0], reverse=True)

        kept = {}
        # Match JsonBlobStore's UTF-8 JSON encoding, reserving its timestamp too.
        size = len(json.dumps({"events": {}, "updated_at": event["at"]}, ensure_ascii=False).encode("utf-8"))
        for _, row in recent[:MAX_EVENTS]:
            pair_size = len(json.dumps({row["id"]: row}, ensure_ascii=False).encode("utf-8")) - 2
            pair_size += 2 if kept else 0
            if size + pair_size > MAX_PAYLOAD_BYTES:
                break
            kept[row["id"]] = row
            size += pair_size
        if event["id"] not in kept:
            raise ValueError("Login activity exceeds the recent-history budget.")
        data.clear()
        data.update(events=kept, updated_at=event["at"])

    _store().mutate(change)
    return event

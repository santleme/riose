"""Auditable animal event chain and future adapter interfaces."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
import math
from typing import Any, Protocol


EVENT_TYPES = frozenset({
    "ANIMAL_CREATED", "OWNER_CHANGED", "WEIGHT_RECORDED", "VACCINATION",
    "HEALTH_EVENT", "TRANSFER", "SLAUGHTER", "VIRTUAL_FENCE_SIMULATED",
})
GENESIS_HASH = "0" * 64


class BlockchainAdapter(Protocol):
    """Future optional adapter; local operation never depends on a chain."""

    def publish(self, event_hash: str, payload: dict[str, Any]) -> str: ...


class EventSigner(Protocol):
    def sign(self, digest: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class AnimalEvent:
    event_id: int
    animal_id: str
    event_type: str
    timestamp: float
    payload: dict[str, Any]
    previous_hash: str
    hash: str
    signature: str | None = None


def canonical_event(animal_id: str, event_type: str, timestamp: float,
                    payload: dict[str, Any], previous_hash: str) -> bytes:
    document = {
        "animal_id": animal_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "payload": payload,
        "previous_hash": previous_hash,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def event_digest(animal_id: str, event_type: str, timestamp: float,
                 payload: dict[str, Any], previous_hash: str) -> str:
    return hashlib.sha256(canonical_event(animal_id, event_type, timestamp,
                                          payload, previous_hash)).hexdigest()


def make_cryptographic_id() -> str:
    """Opaque random identity; signing/key custody is a future interface."""
    return secrets.token_hex(32)


def append_event(connection: sqlite3.Connection, animal_id: str,
                 event_type: str, payload: dict[str, Any],
                 timestamp: float | None = None, *, commit: bool = True) -> AnimalEvent:
    if not isinstance(animal_id, str) or not animal_id.strip():
        raise ValueError("animal_id must be a non-empty string")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported event_type: {event_type}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    timestamp = time.time() if timestamp is None else timestamp
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError("timestamp must be a finite number")
    timestamp = float(timestamp)
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain finite JSON-compatible values") from exc
    row = connection.execute(
        "SELECT hash FROM animal_events WHERE animal_id=? ORDER BY event_id DESC LIMIT 1",
        (animal_id,),
    ).fetchone()
    previous_hash = row[0] if row else GENESIS_HASH
    digest = event_digest(animal_id, event_type, timestamp, payload, previous_hash)
    cursor = connection.execute(
        "INSERT INTO animal_events(animal_id,event_type,timestamp,payload,previous_hash,hash,signature) VALUES(?,?,?,?,?,?,NULL)",
        (animal_id, event_type, timestamp, encoded, previous_hash, digest),
    )
    if commit:
        connection.commit()
    return AnimalEvent(cursor.lastrowid, animal_id, event_type, timestamp,
                       payload, previous_hash, digest)


def verify_event_chain(connection: sqlite3.Connection, animal_id: str) -> bool:
    rows = connection.execute(
        "SELECT animal_id,event_type,timestamp,payload,previous_hash,hash FROM animal_events WHERE animal_id=? ORDER BY event_id",
        (animal_id,),
    )
    previous_hash = GENESIS_HASH
    for event_animal, event_type, timestamp, payload_json, stored_previous, stored_hash in rows:
        try:
            payload = json.loads(payload_json, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(payload, dict) or not math.isfinite(timestamp):
                return False
            if stored_previous != previous_hash or event_type not in EVENT_TYPES:
                return False
            calculated = event_digest(event_animal, event_type, timestamp, payload, previous_hash)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if calculated != stored_hash:
            return False
        previous_hash = stored_hash
    return True

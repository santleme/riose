"""SQLite persistence for a local property deployment."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from ...domain.identity import append_event


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS animals (
  animal_id TEXT PRIMARY KEY, hardware_id TEXT NOT NULL UNIQUE,
  cryptographic_id TEXT NOT NULL UNIQUE, name TEXT, sex TEXT, breed TEXT,
  birth_date TEXT, weight_kg REAL, property_name TEXT, lot TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS anchors (
  anchor_id TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL,
  height_m REAL NOT NULL, kind TEXT NOT NULL, enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS animal_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, animal_id TEXT NOT NULL,
  event_type TEXT NOT NULL, timestamp REAL NOT NULL, payload TEXT NOT NULL,
  previous_hash TEXT NOT NULL, hash TEXT NOT NULL, signature TEXT,
  FOREIGN KEY(animal_id) REFERENCES animals(animal_id)
);
CREATE TABLE IF NOT EXISTS telemetry (
  id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
  tag_id TEXT NOT NULL, anchor_id TEXT NOT NULL, rssi_dbm REAL, snr_db REAL,
  packet_received INTEGER NOT NULL, imu_accel_norm_g REAL,
  behavior_state TEXT, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
  tag_id TEXT NOT NULL, x REAL, y REAL, method TEXT NOT NULL,
  quality REAL, status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_sample_reruns
  ON telemetry(tag_id,anchor_id,timestamp,id);
CREATE INDEX IF NOT EXISTS idx_position_sample_reruns
  ON positions(tag_id,timestamp,id);
CREATE TABLE IF NOT EXISTS debug_truth (
  timestamp REAL NOT NULL, tag_id TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
  PRIMARY KEY(timestamp, tag_id)
);
CREATE TABLE IF NOT EXISTS run_metrics (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = "data/cattle_rf.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def save_anchors(self, anchors: Iterable[Any]) -> None:
        with self._lock:
            self.connection.executemany(
                "INSERT INTO anchors(anchor_id,x,y,height_m,kind,enabled) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(anchor_id) DO UPDATE SET x=excluded.x,y=excluded.y,height_m=excluded.height_m,kind=excluded.kind,enabled=excluded.enabled",
                [(a.anchor_id, a.x, a.y, a.height_m, a.kind, int(a.enabled)) for a in anchors],
            )
            self.connection.commit()

    def create_animal(self, animal_id: str, hardware_id: str, cryptographic_id: str,
                      **profile: Any) -> dict[str, Any]:
        import time
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    "INSERT INTO animals(animal_id,hardware_id,cryptographic_id,name,sex,breed,birth_date,weight_kg,property_name,lot,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (animal_id, hardware_id, cryptographic_id, profile.get("name"), profile.get("sex"),
                     profile.get("breed"), profile.get("birth_date"), profile.get("weight_kg"),
                     profile.get("property_name"), profile.get("lot"), time.time()),
                )
                append_event(self.connection, animal_id, "ANIMAL_CREATED", {"hardware_id": hardware_id}, commit=False)
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            return self.get_animal(animal_id) or {}

    def append_animal_event(self, animal_id: str, event_type: str,
                            payload: dict[str, Any], timestamp: float | None = None):
        with self._lock:
            return append_event(self.connection, animal_id, event_type, payload, timestamp)

    def verify_animal_chain(self, animal_id: str) -> bool:
        from ...domain.identity import verify_event_chain
        with self._lock:
            return verify_event_chain(self.connection, animal_id)

    def get_animal(self, animal_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute("SELECT * FROM animals WHERE animal_id=?", (animal_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["events"] = [dict(r) for r in self.connection.execute(
                "SELECT event_id,event_type,timestamp,payload,previous_hash,hash,signature FROM animal_events WHERE animal_id=? ORDER BY event_id DESC LIMIT 100",
                (animal_id,),
            )]
            for event in result["events"]:
                try:
                    event["payload"] = json.loads(event["payload"])
                except (TypeError, json.JSONDecodeError):
                    event["payload"] = None
            return result

    def animal_id_for_hardware_id(self, hardware_id: str) -> str | None:
        """Find the registered animal assigned to a telemetry tag identifier."""
        with self._lock:
            row = self.connection.execute(
                "SELECT animal_id FROM animals WHERE hardware_id=?", (hardware_id,)
            ).fetchone()
            return str(row["animal_id"]) if row is not None else None

    def list_animals(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute("SELECT * FROM animals ORDER BY animal_id")]

    def animal_trajectory(self, animal_id: str, limit: int = 1000) -> list[dict[str, Any]] | None:
        """Return receiver-derived positions for the tag assigned to an animal."""
        with self._lock:
            animal = self.connection.execute(
                "SELECT hardware_id FROM animals WHERE animal_id=?", (animal_id,)
            ).fetchone()
            if animal is None:
                return None
            rows = self.connection.execute(
                "SELECT timestamp,tag_id,x,y,method,quality,status FROM ("
                "SELECT timestamp,tag_id,x,y,method,quality,status,id,"
                "ROW_NUMBER() OVER(PARTITION BY tag_id,timestamp ORDER BY id DESC) AS sample_rank "
                "FROM positions WHERE tag_id=?"
                ") WHERE sample_rank=1 ORDER BY timestamp DESC,id DESC LIMIT ?",
                (animal["hardware_id"], limit),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def save_episode(self, observations: Iterable[Any], estimates: Iterable[Any],
                     truth: Iterable[Any], persist_truth: bool = True) -> None:
        with self._lock:
            self.connection.executemany(
                "INSERT INTO telemetry(timestamp,tag_id,anchor_id,rssi_dbm,snr_db,packet_received,imu_accel_norm_g,behavior_state,status) VALUES(?,?,?,?,?,?,?,?,?)",
                ((o.timestamp_s, o.tag_id, o.anchor_id, o.rssi_dbm, o.snr_db,
                  int(o.packet_received), o.imu_accel_norm_g, o.behavior_state, o.status.value)
                 for o in observations),
            )
            self.connection.executemany(
                "INSERT INTO positions(timestamp,tag_id,x,y,method,quality,status) VALUES(?,?,?,?,?,?,?)",
                ((e.timestamp_s, e.tag_id, e.x, e.y, e.method, e.quality, e.status.value)
                 for e in estimates),
            )
            if persist_truth:
                self.connection.executemany(
                    "INSERT OR REPLACE INTO debug_truth(timestamp,tag_id,x,y) VALUES(?,?,?,?)",
                    ((t.timestamp_s, t.tag_id, t.x, t.y) for t in truth),
                )
            self.connection.commit()

    def telemetry(self, limit: int = 1000, tag_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if tag_id is not None:
                rows = self.connection.execute(
                    "SELECT id,timestamp,tag_id,anchor_id,rssi_dbm,snr_db,packet_received,imu_accel_norm_g,behavior_state,status FROM (SELECT *,ROW_NUMBER() OVER("
                    "PARTITION BY tag_id,anchor_id,timestamp ORDER BY id DESC) AS sample_rank "
                    "FROM telemetry WHERE tag_id=?) WHERE sample_rank=1 ORDER BY id DESC LIMIT ?",
                    (tag_id, limit))
            else:
                rows = self.connection.execute(
                    "SELECT id,timestamp,tag_id,anchor_id,rssi_dbm,snr_db,packet_received,imu_accel_norm_g,behavior_state,status FROM (SELECT *,ROW_NUMBER() OVER("
                    "PARTITION BY tag_id,anchor_id,timestamp ORDER BY id DESC) AS sample_rank "
                    "FROM telemetry) WHERE sample_rank=1 ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def positions(self, limit: int = 1000, debug: bool = False,
                  at_s: float | None = None) -> list[dict[str, Any]]:
        time_filter = "WHERE timestamp <= ?" if at_s is not None else ""
        args: tuple[Any, ...] = (at_s,) if at_s is not None else ()
        projection = "p.timestamp,p.tag_id,p.x,p.y,p.method,p.quality,p.status"
        join = ""
        truth_cte = ""
        if debug:
            projection += ",truth.x AS ground_truth_x,truth.y AS ground_truth_y"
            truth_cte = """, truth_ranked AS (
                SELECT p.id AS position_id, d.x, d.y,
                       ROW_NUMBER() OVER(PARTITION BY p.id
                         ORDER BY ABS(d.timestamp-p.timestamp), d.timestamp) AS truth_rank
                FROM ranked p JOIN debug_truth d
                  ON d.tag_id=p.tag_id AND ABS(d.timestamp-p.timestamp)<=1.0
            )"""
            join = "LEFT JOIN truth_ranked truth ON truth.position_id=p.id AND truth.truth_rank=1"
        query = f"""WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER(PARTITION BY tag_id ORDER BY timestamp DESC,id DESC) AS rn
            FROM positions {time_filter}
          ){truth_cte} SELECT {projection} FROM ranked p {join}
          WHERE p.rn=1 ORDER BY p.tag_id LIMIT ?"""
        with self._lock:
            return [dict(r) for r in self.connection.execute(query, (*args, limit))]

    def positions_history(self, tag_id: str, limit: int = 1000,
                          debug: bool = False) -> list[dict[str, Any]]:
        """Return one estimate per tag/timestep, preferring the latest rerun."""
        ranked = """WITH ranked AS (
            SELECT p.id,p.timestamp,p.tag_id,p.x,p.y,p.method,p.quality,p.status,
                   ROW_NUMBER() OVER(PARTITION BY p.tag_id,p.timestamp ORDER BY p.id DESC) AS sample_rank
            FROM positions p WHERE p.tag_id=?
          ), latest AS (
            SELECT id,timestamp,tag_id,x,y,method,quality,status FROM ranked
            WHERE sample_rank=1 ORDER BY timestamp DESC,id DESC LIMIT ?
          )"""
        query = ranked + " SELECT timestamp,tag_id,x,y,method,quality,status FROM latest ORDER BY timestamp,id"
        args: tuple[Any, ...] = (tag_id, limit)
        if debug:
            query = ranked + """, candidates AS (
                SELECT p.id,p.timestamp,p.tag_id,p.x,p.y,p.method,p.quality,p.status,
                       d.x AS ground_truth_x,d.y AS ground_truth_y,
                       ROW_NUMBER() OVER(PARTITION BY p.id ORDER BY ABS(d.timestamp-p.timestamp),d.timestamp) AS truth_rank
                FROM latest p LEFT JOIN debug_truth d
                  ON d.tag_id=p.tag_id AND ABS(d.timestamp-p.timestamp)<=1.0
              ) SELECT timestamp,tag_id,x,y,method,quality,status,ground_truth_x,ground_truth_y
                FROM candidates WHERE truth_rank=1 ORDER BY timestamp,id"""
        with self._lock:
            return [dict(r) for r in self.connection.execute(query, args)]

    def events(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            events = [dict(r) for r in self.connection.execute(
                "SELECT * FROM animal_events ORDER BY event_id DESC LIMIT ?", (limit,))]
            for event in events:
                try:
                    event["payload"] = json.loads(event["payload"])
                except (TypeError, json.JSONDecodeError):
                    event["payload"] = None
            return events

    def set_metrics(self, metrics: dict[str, Any]) -> None:
        with self._lock:
            self.connection.executemany(
                "INSERT OR REPLACE INTO run_metrics(key,value) VALUES(?,?)",
                [(key, json.dumps(value, default=str)) for key, value in metrics.items()],
            )
            self.connection.commit()

    def get_metrics(self) -> dict[str, Any]:
        with self._lock:
            return {row["key"]: json.loads(row["value"]) for row in self.connection.execute("SELECT * FROM run_metrics")}

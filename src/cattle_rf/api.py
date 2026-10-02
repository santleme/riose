"""Local FastAPI interface and dashboard."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import json
import math
from pathlib import Path
from typing import Any, Literal
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import Anchor, FarmConfig
from .db import Store
from .identity import make_cryptographic_id


class AnimalCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    animal_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    hardware_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    name: str | None = Field(default=None, max_length=120)
    sex: Literal["female", "male", "unknown"] | None = None
    breed: str | None = Field(default=None, max_length=120)
    birth_date: str | None = Field(default=None, max_length=10)
    weight_kg: float | None = Field(default=None, gt=0, le=3000, allow_inf_nan=False)
    property_name: str | None = Field(default=None, max_length=160)
    lot: str | None = Field(default=None, max_length=120)

    @field_validator("birth_date")
    @classmethod
    def validate_birth_date(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("birth_date must use YYYY-MM-DD") from exc
        return value


class EventCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    animal_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    event_type: Literal["OWNER_CHANGED", "WEIGHT_RECORDED", "VACCINATION", "HEALTH_EVENT", "TRANSFER", "SLAUGHTER"]
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float | None = Field(default=None, allow_inf_nan=False)

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must contain finite JSON-compatible values") from exc
        if len(encoded.encode("utf-8")) > 65536:
            raise ValueError("payload must be at most 64 KiB")
        return value


class AnchorInput(BaseModel):
    anchor_id: str = Field(min_length=1, max_length=64)
    x: float = Field(allow_inf_nan=False)
    y: float = Field(allow_inf_nan=False)
    height_m: float = Field(default=3.0, gt=0, allow_inf_nan=False)
    kind: str = "esp32-c6-subghz"
    enabled: bool = True


class SimulationRequest(BaseModel):
    width_m: float = Field(default=1000.0, gt=0, le=100_000, allow_inf_nan=False)
    height_m: float = Field(default=1000.0, gt=0, le=100_000, allow_inf_nan=False)
    animal_count: int = Field(default=1, ge=1, le=1000)
    anchor_count: int = Field(default=4, ge=1, le=40)
    duration_s: float = Field(default=600, gt=0, le=86_400, allow_inf_nan=False)
    sample_period_s: float = Field(default=30, ge=0.1, allow_inf_nan=False)
    seed: int = 7
    packet_loss_probability: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    method: str = "weighted_centroid"
    anchors: list[AnchorInput] | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_observation_budget(self) -> "SimulationRequest":
        # The simulator stores observations and truth in memory before writing
        # them to SQLite. Bound the work accepted by this local API, including
        # the three synthetic training episodes used by tree-based estimators.
        max_records = 250_000
        steps = max(1, math.ceil(self.duration_s / self.sample_period_s))
        records = steps * self.animal_count * self.anchor_count
        if self.method in {"extra_trees", "gradient_boosting"}:
            training_steps = max(1, math.ceil(
                max(600.0, self.duration_s) / min(30.0, self.sample_period_s)
            ))
            training_animals = max(300, min(self.animal_count, 1000))
            records += 3 * training_steps * training_animals * self.anchor_count
        if records > max_records:
            raise ValueError(
                f"simulation exceeds the local API budget of {max_records} generated observations"
            )
        return self


class CSIRequest(BaseModel):
    timestamp_s: float = 0.0
    tag_id: str = "tag-0001"
    anchor_id: str = "anchor-01"
    movement_intensity: float = Field(default=0.2, ge=0)
    seed: int = 7


class FenceZoneInput(BaseModel):
    zone_id: str
    kind: str
    polygon: list[tuple[float, float]] = Field(min_length=3)


class FenceRequest(BaseModel):
    zones: list[FenceZoneInput] = Field(min_length=1)
    warning_distance_m: float = Field(default=20.0, ge=0)


def create_app(db_path: str | Path = "data/cattle_rf.sqlite3") -> FastAPI:
    app = FastAPI(title="Cattle RF Local MVP", version="0.1.0")
    app.state.store = Store(db_path)
    app.state.anchors = []
    app.state.last_config = None
    app.state.last_episode = None
    app.state.last_estimates = []

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "evidence": "SIMULATED"}

    @app.get("/api/animals")
    def animals() -> list[dict[str, Any]]:
        return app.state.store.list_animals()

    @app.post("/api/animals", status_code=201)
    def create_animal(body: AnimalCreate) -> dict[str, Any]:
        try:
            return app.state.store.create_animal(
                body.animal_id, body.hardware_id, make_cryptographic_id(),
                **body.model_dump(exclude={"animal_id", "hardware_id"}),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="animal_id or hardware_id already exists") from exc

    @app.get("/api/animals/{animal_id}")
    def animal(animal_id: str) -> dict[str, Any]:
        result = app.state.store.get_animal(animal_id)
        if result is None:
            raise HTTPException(status_code=404, detail="animal not found")
        result["trajectory"] = app.state.store.animal_trajectory(animal_id, limit=100)
        return result

    @app.get("/api/animals/{animal_id}/trajectory")
    def animal_trajectory(animal_id: str, limit: int = Query(1000, ge=1, le=10000)) -> list[dict[str, Any]]:
        result = app.state.store.animal_trajectory(animal_id, limit)
        if result is None:
            raise HTTPException(status_code=404, detail="animal not found")
        return result

    @app.get("/api/anchors")
    def anchors() -> list[dict[str, Any]]:
        return [asdict(a) for a in app.state.anchors]

    @app.get("/api/positions")
    def positions(limit: int = Query(1000, ge=1, le=10000), debug: bool = False,
                  at_s: float | None = Query(None, ge=0)) -> list[dict[str, Any]]:
        # Ground truth is joined only after an explicit debug request.
        return app.state.store.positions(limit, debug=debug, at_s=at_s)

    @app.get("/api/positions/history")
    def position_history(tag_id: str = Query(min_length=1, max_length=64),
                         limit: int = Query(1000, ge=1, le=10000),
                         debug: bool = False) -> list[dict[str, Any]]:
        # Historical ground truth remains isolated behind the explicit debug flag.
        return app.state.store.positions_history(tag_id, limit, debug=debug)

    @app.get("/api/telemetry")
    def telemetry(limit: int = Query(1000, ge=1, le=10000),
                  tag_id: str | None = Query(None, min_length=1, max_length=64)) -> list[dict[str, Any]]:
        return app.state.store.telemetry(limit, tag_id)

    @app.get("/api/events")
    def events(limit: int = Query(1000, ge=1, le=10000)) -> list[dict[str, Any]]:
        return app.state.store.events(limit)

    @app.post("/api/events", status_code=201)
    def append_animal_event(body: EventCreate) -> dict[str, Any]:
        if app.state.store.get_animal(body.animal_id) is None:
            raise HTTPException(status_code=404, detail="animal not found")
        try:
            event = app.state.store.append_animal_event(
                body.animal_id, body.event_type, body.payload, body.timestamp)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**asdict(event), "chain_valid": app.state.store.verify_animal_chain(body.animal_id)}

    @app.get("/api/animals/{animal_id}/events/verify")
    def verify_animal_events(animal_id: str) -> dict[str, Any]:
        if app.state.store.get_animal(animal_id) is None:
            raise HTTPException(status_code=404, detail="animal not found")
        return {"animal_id": animal_id,
                "valid": app.state.store.verify_animal_chain(animal_id),
                "evidence": "LOCAL_HASH_CHAIN"}

    @app.post("/api/simulation/run")
    def run_simulation(body: SimulationRequest) -> dict[str, Any]:
        from .pipeline import run_episode
        config = FarmConfig(
            width_m=body.width_m, height_m=body.height_m,
            animal_count=body.animal_count, anchor_count=body.anchor_count,
            duration_s=body.duration_s, sample_period_s=body.sample_period_s,
            seed=body.seed, packet_loss_probability=body.packet_loss_probability,
        )
        anchor_set = None
        if body.anchors is not None:
            ids = [anchor.anchor_id for anchor in body.anchors]
            if len(body.anchors) != body.anchor_count:
                raise HTTPException(status_code=422, detail="anchor_count must match the supplied anchors")
            if len(ids) != len(set(ids)):
                raise HTTPException(status_code=422, detail="anchor_id values must be unique")
            if any(anchor.x < 0 or anchor.x > body.width_m or anchor.y < 0 or anchor.y > body.height_m
                   for anchor in body.anchors):
                raise HTTPException(status_code=422, detail="anchor coordinates must be inside the farm")
            anchor_set = tuple(Anchor(**anchor.model_dump()) for anchor in body.anchors)
        try:
            episode, estimates, metrics = run_episode(config, body.method, anchors=anchor_set)
        except (ImportError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        app.state.store.save_anchors(episode.anchors)
        app.state.store.save_episode(episode.observations, estimates, episode.ground_truth)
        app.state.anchors = list(episode.anchors)
        app.state.last_config = config
        app.state.last_episode = episode
        app.state.last_estimates = list(estimates)
        app.state.store.set_metrics(metrics)
        ensure_animals(app.state.store, body.animal_count)
        return {"status": "completed", "evidence": "SIMULATED", "observations": len(episode.observations),
                "estimates": len(estimates), "metrics": metrics}

    @app.get("/api/experiments")
    def experiments() -> dict[str, Any]:
        return {"available_methods": ["strongest_anchor", "weighted_centroid", "path_loss", "extra_trees", "gradient_boosting", "temporal_fusion"],
                "advanced_rf": capability_status(), "evidence": "SIMULATED"}

    @app.post("/api/experiments/csi")
    def simulated_csi(body: CSIRequest) -> dict[str, Any]:
        from dataclasses import asdict
        from .experimental import simulate_wifi_csi
        return asdict(simulate_wifi_csi(body.timestamp_s, body.tag_id, body.anchor_id,
                                        body.movement_intensity, body.seed))

    @app.post("/api/experiments/virtual-fence")
    def run_virtual_fence(body: FenceRequest) -> dict[str, Any]:
        from dataclasses import asdict
        from .virtual_fence import FenceZone, simulate_fence
        episode = app.state.last_episode
        if episode is None:
            raise HTTPException(status_code=409, detail="run a farm simulation first")
        zones = [FenceZone(zone.zone_id, zone.kind, tuple(zone.polygon)) for zone in body.zones]
        events = simulate_fence(episode.ground_truth, zones, body.warning_distance_m)
        records = []
        for event in events:
            animal = app.state.store.get_animal_by_hardware_id(event.tag_id)
            if animal is None:
                continue
            stored = app.state.store.append_animal_event(
                animal["animal_id"], "VIRTUAL_FENCE_SIMULATED",
                {"zone_id": event.zone_id, "state": event.state.value,
                 "response": event.simulated_response, "evidence": "SIMULATED"},
                event.timestamp_s,
            )
            records.append(asdict(event) | {"event_hash": stored.hash})
        return {"status": "SIMULATED", "electric_stimulus": False, "events": records}

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        from .sim.advanced import advanced_capabilities
        return {"advanced_rf": advanced_capabilities(), "hardware": capability_status(),
                "cellular": "FUTURE", "blockchain": "FUTURE"}

    @app.get("/api/metrics")
    def metrics() -> dict[str, Any]:
        return app.state.store.get_metrics()

    return app


def ensure_animals(store: Store, count: int) -> None:
    import sqlite3
    import uuid
    for index in range(count):
        animal_id = f"cow-{index + 1:04d}"
        if store.get_animal(animal_id) is None:
            try:
                store.create_animal(animal_id, f"tag-{index + 1:04d}", make_cryptographic_id(),
                                    name=f"Animal {index + 1}", property_name="Demo Farm")
            except sqlite3.IntegrityError:
                continue


def capability_status() -> dict[str, bool]:
    import importlib.util
    import shutil
    try:
        ns3_python = importlib.util.find_spec("ns.core") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        ns3_python = False
    return {
        "sionna": importlib.util.find_spec("sionna") is not None,
        "ns3": shutil.which("ns3") is not None or ns3_python,
        "wokwi_cli": shutil.which("wokwi-cli") is not None,
        "zephyr_west": shutil.which("west") is not None,
        "ngspice": shutil.which("ngspice") is not None,
        "kicad_cli": shutil.which("kicad-cli") is not None,
    }


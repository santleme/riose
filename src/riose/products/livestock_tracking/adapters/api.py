"""Local FastAPI interface and dashboard."""

from __future__ import annotations

from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import date
import json
import math
from pathlib import Path
from typing import Any, Literal
import sqlite3

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.contracts import Anchor, FarmConfig
from .persistence import Store
from ..domain.identity import make_cryptographic_id


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
    duration_s: float = Field(default=600, gt=0, le=604_800, allow_inf_nan=False)
    sample_period_s: float = Field(default=30, ge=0.001, le=604_800, allow_inf_nan=False)
    seed: int = 7
    packet_loss_probability: float = Field(default=0.05, ge=0, le=1, allow_inf_nan=False)
    method: str = "weighted_centroid"
    anchors: list[AnchorInput] | None = Field(default=None, min_length=1, max_length=40)


class CSIRequest(BaseModel):
    timestamp_s: float = Field(default=0.0, allow_inf_nan=False)
    tag_id: str = "tag-0001"
    anchor_id: str = "anchor-01"
    movement_intensity: float = Field(default=0.2, ge=0, allow_inf_nan=False)
    seed: int = 7


class FenceZoneInput(BaseModel):
    zone_id: str
    kind: str
    polygon: list[tuple[float, float]] = Field(min_length=3)

    @field_validator("polygon")
    @classmethod
    def validate_polygon_coordinates(cls, value: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if any(not math.isfinite(coordinate) for point in value for coordinate in point):
            raise ValueError("polygon coordinates must be finite")
        return value


class FenceRequest(BaseModel):
    zones: list[FenceZoneInput] = Field(min_length=1)
    warning_distance_m: float = Field(default=20.0, ge=0, allow_inf_nan=False)


# Bound the in-memory API pipeline before constructing episode/training tuples.
# The budget accounts for episodes, grouped estimator input, truth, and results;
# supervised methods include three additional training episodes.
MAX_SIMULATION_OBSERVATIONS = 500_000
MAX_SIMULATION_MEMORY_UNITS = 1_000_000


def estimate_simulation_observations(body: SimulationRequest, enabled_anchors: int) -> int:
    episode_steps = max(1, math.ceil(body.duration_s / body.sample_period_s))
    count = body.animal_count * episode_steps * enabled_anchors
    if body.method in {"extra_trees", "gradient_boosting"}:
        training_animals = max(300, min(body.animal_count, 1000))
        training_steps = max(1, math.ceil(max(600.0, body.duration_s) /
                                          min(30.0, body.sample_period_s)))
        count += 3 * training_animals * training_steps * enabled_anchors
    return count


def estimate_simulation_memory_units(body: SimulationRequest, enabled_anchors: int) -> int:
    """Estimate allocated per-sample records, including truth and motion.

    RF observation counts alone are not a safe admission bound: movement and
    ground-truth records are generated even when every receiver is disabled.
    One inference sample accounts for truth, motion, observations, and its
    estimate. A supervised training sample accounts for truth, motion,
    observations, and the matching feature/target rows used during fitting.
    """
    episode_steps = max(1, math.ceil(body.duration_s / body.sample_period_s))
    episode_samples = body.animal_count * episode_steps
    units = episode_samples * (enabled_anchors + 3)
    if body.method in {"extra_trees", "gradient_boosting"}:
        training_animals = max(300, min(body.animal_count, 1000))
        training_steps = max(1, math.ceil(max(600.0, body.duration_s) /
                                          min(30.0, body.sample_period_s)))
        units += 3 * training_animals * training_steps * (enabled_anchors + 3)
    return units


def create_app(db_path: str | Path = "data/cattle_rf.sqlite3") -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            app.state.store.close()

    app = FastAPI(title="Cattle RF Local MVP", version="0.1.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.exception_handler(RequestValidationError)
    async def validation_error_response(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Python's JSON parser accepts NaN/Infinity extensions. Pydantic correctly
        # rejects them, but its default error payload echoes the raw non-finite
        # input and Starlette cannot serialize that payload as JSON.
        def json_safe(value: Any) -> Any:
            if isinstance(value, float) and not math.isfinite(value):
                return repr(value)
            if isinstance(value, BaseException):
                return str(value)
            if isinstance(value, dict):
                return {key: json_safe(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_safe(item) for item in value]
            return value

        return JSONResponse(status_code=422, content={"detail": json_safe(exc.errors())})

    app.state.store = Store(db_path)
    app.state.anchors = []
    app.state.last_config = None
    app.state.last_ground_truth = None

    static_dir = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="site-assets")

    @app.get("/", response_class=HTMLResponse)
    def landing_page() -> str:
        return (static_dir / "landing.html").read_text(encoding="utf-8")

    @app.get("/manifesto", response_class=HTMLResponse)
    def manifesto_page() -> str:
        return (static_dir / "manifesto.html").read_text(encoding="utf-8")

    @app.get("/demo", response_class=HTMLResponse)
    def dashboard() -> str:
        return (static_dir / "index.html").read_text(encoding="utf-8")

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
        from ..application.pipeline import run_episode
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
        enabled_anchors = (sum(anchor.enabled for anchor in anchor_set)
                           if anchor_set is not None else body.anchor_count)
        if enabled_anchors < 1:
            raise HTTPException(status_code=422, detail="at least one anchor must be enabled")
        estimated_observations = estimate_simulation_observations(body, enabled_anchors)
        if estimated_observations > MAX_SIMULATION_OBSERVATIONS:
            raise HTTPException(
                status_code=422,
                detail=(f"requested simulation exceeds the in-memory observation budget "
                        f"({estimated_observations:,} > {MAX_SIMULATION_OBSERVATIONS:,}); "
                        "increase sample_period_s or reduce duration_s, animal_count, anchors, "
                        "or use a non-supervised method"),
            )
        estimated_memory_units = estimate_simulation_memory_units(body, enabled_anchors)
        if estimated_memory_units > MAX_SIMULATION_MEMORY_UNITS:
            raise HTTPException(
                status_code=422,
                detail=(f"requested simulation exceeds the in-memory sample budget "
                        f"({estimated_memory_units:,} > {MAX_SIMULATION_MEMORY_UNITS:,}); "
                        "increase sample_period_s or reduce duration_s or animal_count"),
            )
        try:
            episode, estimates, metrics = run_episode(config, body.method, anchors=anchor_set)
        except (ImportError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        app.state.store.save_anchors(episode.anchors)
        app.state.store.save_episode(episode.observations, estimates, episode.ground_truth)
        app.state.anchors = list(episode.anchors)
        app.state.last_config = config
        # The virtual fence only needs truth. Do not retain all observations and
        # estimates for the lifetime of the dashboard process.
        app.state.last_ground_truth = episode.ground_truth
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
        from ..simulation.experimental import simulate_wifi_csi
        return asdict(simulate_wifi_csi(body.timestamp_s, body.tag_id, body.anchor_id,
                                        body.movement_intensity, body.seed))

    @app.post("/api/experiments/virtual-fence")
    def run_virtual_fence(body: FenceRequest) -> dict[str, Any]:
        from dataclasses import asdict
        from ..domain.virtual_fence import FenceZone, simulate_fence
        ground_truth = app.state.last_ground_truth
        if ground_truth is None:
            raise HTTPException(status_code=409, detail="run a farm simulation first")
        zones = [FenceZone(zone.zone_id, zone.kind, tuple(zone.polygon)) for zone in body.zones]
        events = simulate_fence(ground_truth, zones, body.warning_distance_m)
        records = []
        for event in events:
            animal_id = app.state.store.animal_id_for_hardware_id(event.tag_id)
            if animal_id is None:
                continue
            stored = app.state.store.append_animal_event(
                animal_id, "VIRTUAL_FENCE_SIMULATED",
                {"zone_id": event.zone_id, "state": event.state.value,
                 "response": event.simulated_response, "evidence": "SIMULATED"},
                event.timestamp_s,
            )
            records.append(asdict(event) | {"event_hash": stored.hash})
        return {"status": "SIMULATED", "electric_stimulus": False, "events": records}

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        from ..simulation.advanced import advanced_capabilities
        return {"advanced_rf": advanced_capabilities(), "hardware": capability_status(),
                "cellular": "FUTURE"}

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

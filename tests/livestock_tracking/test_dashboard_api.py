from fastapi.testclient import TestClient
import sqlite3

import pytest

from cattle_rf.api import (
    MAX_SIMULATION_MEMORY_UNITS,
    MAX_SIMULATION_OBSERVATIONS,
    SimulationRequest,
    create_app,
    estimate_simulation_memory_units,
    estimate_simulation_observations,
)


def test_dashboard_explicit_anchors_and_debug_only_truth(tmp_path):
    app = create_app(tmp_path / "dashboard.sqlite3")
    client = TestClient(app)
    page = client.get("/demo")
    assert page.status_code == 200
    assert "Cattle RF" in page.text
    anchors = [
        {"anchor_id": "north-west", "x": 0, "y": 0},
        {"anchor_id": "north-east", "x": 1000, "y": 0},
        {"anchor_id": "south-east", "x": 1000, "y": 1000},
        {"anchor_id": "south-west", "x": 0, "y": 1000},
    ]
    response = client.post("/api/simulation/run", json={
        "animal_count": 1,
        "anchor_count": 4,
        "anchors": anchors,
        "duration_s": 60,
        "sample_period_s": 30,
        "seed": 19,
    })
    assert response.status_code == 200, response.text
    assert response.json()["evidence"] == "SIMULATED"
    assert [a["anchor_id"] for a in client.get("/api/anchors").json()] == [
        "north-west", "north-east", "south-east", "south-west"
    ]

    tag_id = "tag-0001"
    regular = client.get("/api/positions", params={"limit": 100, "at_s": 60}).json()
    assert regular and "ground_truth_x" not in regular[0]
    history = client.get("/api/positions/history", params={"tag_id": tag_id, "limit": 100}).json()
    assert history and "ground_truth_x" not in history[0]
    debug = client.get("/api/positions/history", params={
        "tag_id": tag_id, "limit": 100, "debug": "true"
    }).json()
    assert debug and "ground_truth_x" in debug[0]
    assert debug == sorted(debug, key=lambda row: row["timestamp"])


def test_dashboard_rerun_replaces_duplicate_visible_samples_and_keeps_latest_page(tmp_path):
    client = TestClient(create_app(tmp_path / "rerun-history.sqlite3"))
    common = {"animal_count": 1, "anchor_count": 4, "duration_s": 90,
              "sample_period_s": 30, "method": "strongest_anchor"}
    for seed in (11, 29):
        response = client.post("/api/simulation/run", json={**common, "seed": seed})
        assert response.status_code == 200, response.text

    history = client.get("/api/positions/history", params={
        "tag_id": "tag-0001", "limit": 100,
    }).json()
    assert [row["timestamp"] for row in history] == [0.0, 30.0, 60.0]
    assert [row["timestamp"] for row in client.get(
        "/api/positions/history", params={"tag_id": "tag-0001", "limit": 2}
    ).json()] == [30.0, 60.0]
    animal = client.get("/api/animals/cow-0001").json()
    assert [row["timestamp"] for row in animal["trajectory"]] == [0.0, 30.0, 60.0]

    telemetry = client.get("/api/telemetry", params={"tag_id": "tag-0001", "limit": 100}).json()
    assert len(telemetry) == 12
    assert len({(row["timestamp"], row["anchor_id"]) for row in telemetry}) == 12
    assert "sample_rank" not in telemetry[0]
    latest_telemetry = client.get("/api/telemetry", params={
        "tag_id": "tag-0001", "limit": 4,
    }).json()
    assert len(latest_telemetry) == 4
    assert {row["timestamp"] for row in latest_telemetry} == {60.0}


def test_dashboard_exposes_simulation_only_virtual_fence_action(tmp_path):
    client = TestClient(create_app(tmp_path / "virtual-fence-ui.sqlite3"))
    page = client.get("/demo")

    assert page.status_code == 200
    assert 'id="runFence"' in page.text
    assert 'Simular cerca virtual' in page.text
    assert "api/experiments/virtual-fence" in page.text
    assert "zona-central-demo" in page.text
    assert "Nenhum estímulo físico ou elétrico é produzido" in page.text


def test_landing_page_and_static_product_assets(tmp_path):
    client = TestClient(create_app(tmp_path / "landing.sqlite3"))

    page = client.get("/")
    assert page.status_code == 200
    assert "RIOSE — Livestock technology" in page.text
    assert "Machine learning-assisted self-powered ear tag for animal welfare" in page.text
    assert 'id="inspection-toggle"' not in page.text
    assert "turn toward the rear" in page.text
    assert "wire routing is illustrative" in page.text
    assert "Xiaoyu Su, Peidi Fan, Ying Liu, Jianfeng Ping, Xunjia Li and Yuxiang Pan" in page.text
    assert "Nature Communications · 2026" in page.text
    assert "Independent study" not in page.text
    assert "5,399 sampling windows from three animals" in page.text
    assert 'id="product-canvas"' in page.text
    assert "scene-fallback" not in page.text
    assert "/assets/product-scene.js" in page.text
    assert "/assets/landing.css" in page.text
    assert "/assets/landing.js" in page.text
    assert "<style>" not in page.text
    assert "<script>" not in page.text

    manifesto = client.get("/manifesto")
    assert manifesto.status_code == 200
    assert "Manifesto — RIOSE" in manifesto.text
    assert "The physical world takes no shortcuts." in manifesto.text
    assert "Named after Bel Riose" in manifesto.text
    assert "proper noun · origin" in manifesto.text
    assert "/assets/manifesto.css" in manifesto.text
    assert "/assets/manifesto.js" in manifesto.text
    assert "<style>" not in manifesto.text
    assert "<script>" not in manifesto.text

    landing_css = client.get("/assets/landing.css")
    assert landing_css.status_code == 200
    assert ".scene-wrap" in landing_css.text
    landing_js = client.get("/assets/landing.js")
    assert landing_js.status_code == 200
    assert "sceneObserver" in landing_js.text

    manifesto_css = client.get("/assets/manifesto.css")
    assert manifesto_css.status_code == 200
    assert ".manifesto-copy" in manifesto_css.text
    manifesto_js = client.get("/assets/manifesto.js")
    assert manifesto_js.status_code == 200
    assert "ascii-structure" in manifesto_js.text

    scene = client.get("/assets/product-scene.js")
    assert scene.status_code == 200
    assert "WebGLRenderer" in scene.text
    assert "smoothstep(faceAlignment" in scene.text
    assert "createTechnicalAnnotations" in scene.text
    assert "wireRoutes" in scene.text
    assert "Circuit board" in scene.text
    assert "PCB · concept" not in scene.text
    assert "CONCEPT STUDY" not in scene.text

    three = client.get("/assets/vendor/three.module.js")
    assert three.status_code == 200
    assert "Three.js Authors" in three.text
    assert three.headers.get("content-encoding") == "gzip"

    fallback = client.get("/assets/riose-ear-tag-fallback.webp")
    assert fallback.status_code == 404


def test_dashboard_closes_sqlite_store_on_lifespan_shutdown(tmp_path):
    app = create_app(tmp_path / "lifecycle.sqlite3")
    store = app.state.store

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    assert store.connection is not None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.connection.execute("SELECT 1")


def test_simulation_rejects_duplicate_or_outside_farm_anchors(tmp_path):
    client = TestClient(create_app(tmp_path / "invalid.sqlite3"))
    common = {"animal_count": 1, "duration_s": 30, "sample_period_s": 30}
    duplicate = [{"anchor_id": "a", "x": 0, "y": 0}] * 2
    response = client.post("/api/simulation/run", json={**common, "anchors": duplicate})
    assert response.status_code == 422
    outside = [{"anchor_id": "a", "x": 1001, "y": 0}]
    response = client.post("/api/simulation/run", json={**common, "anchors": outside})
    assert response.status_code == 422


def test_simulation_rejects_nonfinite_and_extreme_numeric_inputs(tmp_path):
    client = TestClient(create_app(tmp_path / "numeric-input.sqlite3"))
    response = client.post("/api/simulation/run", json={
        "animal_count": 1,
        "anchor_count": 4,
        "duration_s": 1e308,
        "sample_period_s": 5e-324,
    })
    assert response.status_code == 422

    response = client.post("/api/simulation/run", content='{"width_m":null}',
                           headers={"content-type": "application/json"})
    assert response.status_code == 422
    with pytest.raises(ValueError):
        SimulationRequest(width_m=float("nan"))


@pytest.mark.parametrize("nonfinite", ["NaN", "Infinity", "-Infinity"])
def test_simulation_rejects_nonfinite_packet_loss_before_persisting(tmp_path, nonfinite):
    app = create_app(tmp_path / f"nonfinite-{nonfinite}.sqlite3")
    with TestClient(app) as client:
        response = client.post(
            "/api/simulation/run",
            content=(
                '{"animal_count":1,"anchor_count":4,"duration_s":30,'
                f'"sample_period_s":30,"packet_loss_probability":{nonfinite}'
                "}"
            ),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 422
        assert client.get("/api/anchors").json() == []
        assert client.get("/api/telemetry").json() == []
        assert client.get("/api/positions").json() == []
        assert client.get("/api/metrics").json() == {}
        assert client.get("/api/animals").json() == []


def test_simulation_rejects_requests_over_memory_budget_before_running(tmp_path, monkeypatch):
    from riose.products.livestock_tracking.application import pipeline

    def should_not_start(*args, **kwargs):
        raise AssertionError("oversized request reached the simulation pipeline")

    monkeypatch.setattr(pipeline, "run_episode", should_not_start)
    client = TestClient(create_app(tmp_path / "oversized.sqlite3"))
    response = client.post("/api/simulation/run", json={
        "animal_count": 1000,
        "anchor_count": 40,
        "duration_s": 86400,
        "sample_period_s": 1,
    })
    assert response.status_code == 422
    assert "observation budget" in response.json()["detail"]


def test_memory_budget_keeps_default_supervised_dashboard_scenario_available():
    request = SimulationRequest(
        animal_count=100,
        anchor_count=8,
        duration_s=1800,
        sample_period_s=30,
        method="extra_trees",
    )

    estimate = estimate_simulation_observations(request, enabled_anchors=8)
    assert estimate == 480_000
    assert estimate <= MAX_SIMULATION_OBSERVATIONS
    assert estimate_simulation_memory_units(request, enabled_anchors=8) <= MAX_SIMULATION_MEMORY_UNITS


def test_memory_budget_counts_truth_and_motion_when_anchors_are_disabled(tmp_path, monkeypatch):
    from riose.products.livestock_tracking.application import pipeline

    def should_not_start(*args, **kwargs):
        raise AssertionError("oversized disabled-anchor request reached the simulation pipeline")

    monkeypatch.setattr(pipeline, "run_episode", should_not_start)
    client = TestClient(create_app(tmp_path / "disabled-anchors.sqlite3"))
    anchors = [
        {"anchor_id": f"a{i}", "x": (i % 2) * 1000, "y": (i // 2) * 1000,
         "enabled": False}
        for i in range(4)
    ]
    response = client.post("/api/simulation/run", json={
        "animal_count": 1000,
        "anchor_count": 4,
        "anchors": anchors,
        "duration_s": 604800,
        "sample_period_s": 1,
    })
    assert response.status_code == 422
    assert "at least one anchor must be enabled" in response.json()["detail"]


def test_memory_budget_rejects_large_ground_truth_even_with_one_enabled_anchor(tmp_path, monkeypatch):
    from riose.products.livestock_tracking.application import pipeline

    def should_not_start(*args, **kwargs):
        raise AssertionError("oversized request reached the simulation pipeline")

    monkeypatch.setattr(pipeline, "run_episode", should_not_start)
    client = TestClient(create_app(tmp_path / "truth-memory.sqlite3"))
    anchors = [
        {"anchor_id": f"a{i}", "x": (i % 2) * 1000, "y": (i // 2) * 1000,
         "enabled": i == 0}
        for i in range(4)
    ]
    response = client.post("/api/simulation/run", json={
        "animal_count": 1000,
        "anchor_count": 4,
        "anchors": anchors,
        "duration_s": 1000,
        "sample_period_s": 2,
    })
    assert response.status_code == 422
    assert "in-memory observation budget" not in response.json()["detail"]
    assert "in-memory sample budget" in response.json()["detail"]

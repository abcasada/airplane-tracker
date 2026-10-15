"""Replay harness, metrics and backtest on the synthetic fixture recording."""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.predict.arrival import runways_for
from app.replay.__main__ import main as cli_main
from app.replay.metrics import calibration
from app.replay.runner import evaluate_recordings
from tests.conftest import Harness
from tests.scenarios.flight import T0
from tests.scenarios.replay_fixture import DURATION_S, FIXTURES, NAME

RECORDING = FIXTURES / NAME
EXPECTED = FIXTURES / f"{NAME}.expected.json"

HOME = {"name": "Home", "lat": 38.84, "lon": -97.61, "elevation_ft": 1220, "radius_nm": 10,
        "is_home": True}  # fmt: skip
RANGE = {
    "name": "Range", "role": "area", "lat": 38.67, "lon": -97.85, "radius_nm": 10,
    "alt_ceiling_ft": 23000,
    "polygon": [[38.7556, -97.7669], [38.6625, -97.7669], [38.6389, -97.7919],
                [38.6389, -97.8336], [38.5833, -97.8336], [38.5833, -97.9336],
                [38.7556, -97.9336]],
}  # fmt: skip
KSLN = {"name": "KSLN", "role": "airport", "airport_icao": "KSLN", "lat": 38.791, "lon": -97.652,
        "elevation_ft": 1288, "radius_nm": 15}  # fmt: skip
RUNWAYS: list[dict[str, float | str | None]] = [
    {"le_ident": "17", "le_lat": 38.8075, "le_lon": -97.6522, "le_heading": 180.0,
     "he_ident": "35", "he_lat": 38.7738, "he_lon": -97.6522, "he_heading": 360.0},
]  # fmt: skip


def cond(field: str, op: str, place: int | None = None) -> dict[str, Any]:
    return {"type": "cond", "field": field, "op": op, "place": place}


async def setup(h: Harness) -> dict[str, int]:
    c = h.client
    ids = {}
    for key, body in (("home", HOME), ("range", RANGE), ("ksln", KSLN)):
        r = await c.post("/api/places", json=body)
        assert r.status_code == 201, r.text
        ids[key] = r.json()["id"]
    filters = {
        "Military visible": [
            cond("military", "is_true"), cond("visible_now", "is_true", ids["home"]),
        ],
        "Predicted visible": [cond("predicted_visible", "is_true", ids["home"])],
        "Inside range": [cond("inside_area", "is_true", ids["range"])],
        "Arriving KSLN": [cond("predicted_arrival", "is_true", ids["ksln"])],
    }  # fmt: skip
    for name, children in filters.items():
        body = {"name": name, "definition": {"root": {"mode": "all", "children": children}}}
        assert (await c.post("/api/filters", json=body)).status_code == 201
    h.ctx.rt().runways[ids["ksln"]] = runways_for(RUNWAYS)
    return ids


def _exchange(got: dict[str, Any]) -> Any:
    """Loads the expectations; REPLAY_DUMP=<file> also saves the report, to compare metrics
    before and after a prediction change."""
    if dump := os.environ.get("REPLAY_DUMP"):
        Path(dump).write_text(json.dumps(got, indent=1))
    return json.loads(EXPECTED.read_text())


async def test_replay_fixture_matches_expected_alerts(authed: Harness) -> None:
    await setup(authed)
    report = await evaluate_recordings(authed.ctx.rt(), RECORDING, T0, T0 + DURATION_S)
    got = {
        "batches": report["batches"],
        "alerts": {
            f["name"]: sorted(a["hex"] for a in f["aircraft_list"]) for f in report["filters"]
        },
        "prediction": report["prediction"]["kinds"],
    }
    expected = _exchange(got)
    assert got["batches"] == expected["batches"]
    assert got["alerts"] == expected["alerts"]
    for kind, want in expected["predictor"].items():
        pred = got["prediction"][kind]["predictor"]
        assert {k: pred[k] for k in want} == want, kind
    # The baseline (adsb-alarm's heuristic) finds the same events but with far more noise.
    for kind in expected["predictor"]:
        kinds = got["prediction"][kind]
        assert kinds["baseline"]["false_alarms"] > kinds["predictor"]["false_alarms"]
        assert kinds["calibration"]["ece"] < 0.15


def _copy_recording(dest: Path) -> None:
    shutil.copytree(RECORDING, dest, dirs_exist_ok=True)


async def _wait(h: Harness, job_id: str) -> dict[str, Any]:
    for _ in range(500):
        r = await h.client.get(f"/api/filters/backtest/{job_id}")
        assert r.status_code == 200
        job: dict[str, Any] = r.json()
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("backtest did not finish")


async def test_backtest_compares_draft_with_saved(authed: Harness) -> None:
    ids = await setup(authed)
    rt = authed.ctx.rt()
    _copy_recording(rt.recorder.root)
    authed.clock.advance(DURATION_S + 60)
    saved = next(f for f in rt.filters if f.name == "Predicted visible")
    draft = {"root": {"mode": "all", "children": [
        cond("military", "is_true"), cond("predicted_visible", "is_true", ids["home"]),
    ]}}  # fmt: skip
    r = await authed.client.post(
        "/api/filters/backtest", json={"definition": draft, "filter_id": saved.id, "days": 1}
    )
    assert r.status_code == 202, r.text
    job = await _wait(authed, r.json()["id"])
    assert job["status"] == "done", job
    res = job["result"]
    assert res["predicted"] is True
    assert res["totals"]["draft"] == {"episodes": 1, "notified": 1, "aircraft": 1}
    assert res["totals"]["saved"]["aircraft"] == 3
    assert [a["hex"] for a in res["only_saved"]] == ["a4004", "a7007"]
    assert res["only_draft"] == [] and res["both"] == 1
    assert [d["date"] for d in res["days"]] == ["2027-01-15"]
    assert res["days"][0]["draft"]["episodes"] == 1


async def test_backtest_applies_opt_in_suppression_and_shadow_mode(authed: Harness) -> None:
    ids = await setup(authed)
    _copy_recording(authed.ctx.rt().recorder.root)
    authed.clock.advance(DURATION_S + 60)
    near = cond("distance_nm", "lt", ids["home"]) | {"value": 30}
    draft = {"root": {"mode": "all", "children": [near]}, "notify": {"max_per_day": 2}}
    r = await authed.client.post("/api/filters/backtest", json={"definition": draft})
    res = (await _wait(authed, r.json()["id"]))["result"]
    assert res["predicted"] is False  # no prediction fields: the predictor isn't run
    assert res["totals"]["draft"]["episodes"] == 7
    assert res["totals"]["draft"]["notified"] == 2
    r = await authed.client.post(
        "/api/filters/backtest", json={"definition": draft, "mode": "shadow"}
    )
    res = (await _wait(authed, r.json()["id"]))["result"]
    assert res["totals"]["draft"]["notified"] == 0


async def test_backtest_errors_and_cancel(authed: Harness) -> None:
    bad = {"root": {"mode": "all", "children": [cond("no_such_field", "is_true")]}}
    r = await authed.client.post("/api/filters/backtest", json={"definition": bad})
    job = await _wait(authed, r.json()["id"])
    assert job["status"] == "error" and "no_such_field" in job["error"]
    assert (await authed.client.get("/api/filters/backtest/nope")).status_code == 404
    r = await authed.client.post(
        "/api/filters/backtest", json={"definition": {"root": {}}, "filter_id": 999}
    )
    assert r.status_code == 404
    r = await authed.client.post("/api/filters/backtest", json={"definition": {}, "days": 30})
    assert r.status_code == 422


async def test_cli_prints_metrics(
    authed: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await setup(authed)
    monkeypatch.setenv("DATA_DIR", str(authed.ctx.settings.data_dir))
    monkeypatch.setenv("ENV", "development")
    code = await cli_main(
        ["--from", "2027-01-15T08:00", "--days", "0.05", "--recordings", str(RECORDING)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "151 batches" in out
    assert "visible" in out and "baseline" in out and "Military visible" in out


def test_calibration_math() -> None:
    perfect = calibration([(0.9, True)] * 9 + [(0.9, False)] + [(0.1, False)] * 9 + [(0.1, True)])
    assert perfect["ece"] == pytest.approx(0.0)
    assert perfect["brier"] == pytest.approx(0.09)
    over = calibration([(0.9, False)] * 10)
    assert over["ece"] == pytest.approx(0.9) and over["brier"] == pytest.approx(0.81)
    assert calibration([])["samples"] == 0

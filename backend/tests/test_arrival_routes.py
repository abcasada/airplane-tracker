import httpx
import pytest
import respx

from app import geo
from app.clock import FakeClock
from app.db import Database
from app.enrich import datasets as ds
from app.enrich.routes import ROUTESET_URL, Route, RouteLeg, RouteService, fit_route
from app.predict.arrival import ArrivalInputs, arrival_score, runways_for

# KSLN from OurAirports: field 38.791 N 97.652 W, 1,288 ft; runway 17/35 thresholds.
FIELD = (38.791, -97.652)
RWY_ROWS: list[dict[str, float | str | None]] = [
    {"le_ident": "17", "le_lat": 38.8075, "le_lon": -97.6522, "le_heading": 180.0,
     "he_ident": "35", "he_lat": 38.7738, "he_lon": -97.6522, "he_heading": 360.0},
]  # fmt: skip
RUNWAYS = runways_for(RWY_ROWS)


def score_at(
    bearing_from_field: float, dist_nm: float, **kw: float | bool | None
) -> tuple[float, str | None]:
    lat, lon = geo.destination(*FIELD, bearing_from_field, dist_nm)
    inputs = ArrivalInputs(lat=lat, lon=lon, **kw)  # type: ignore[arg-type]
    return arrival_score(inputs, *FIELD, RUNWAYS)


def test_straight_in_final_scores_high_and_names_runway() -> None:
    # 6 nm south of the field, heading north, descending on a 3° path: final for runway 35.
    score, rwy = score_at(180, 6, height_ft=1900, gs_kt=150, track_deg=0, vrate_fpm=-700,
                          on_ground=False)  # fmt: skip
    assert score > 0.8
    assert rwy == "35"


def test_overflight_in_cruise_scores_zero() -> None:
    score, _ = score_at(180, 6, height_ft=34000, gs_kt=450, track_deg=0, vrate_fpm=0,
                        on_ground=False)  # fmt: skip
    assert score == 0


def test_departure_climbing_away_scores_low() -> None:
    score, _ = score_at(0, 5, height_ft=3000, gs_kt=200, track_deg=0, vrate_fpm=2500,
                        on_ground=False)  # fmt: skip
    assert score < 0.1


def test_descending_toward_from_distance_is_moderate() -> None:
    score, rwy = score_at(45, 30, height_ft=9000, gs_kt=260, track_deg=225, vrate_fpm=-1200,
                          on_ground=False)  # fmt: skip
    assert 0.3 < score < 0.8
    assert rwy is None


def test_passing_by_at_low_altitude_is_lower_than_heading_in() -> None:
    by, _ = score_at(90, 8, height_ft=2500, gs_kt=140, track_deg=0, vrate_fpm=0, on_ground=False)
    toward, _ = score_at(90, 8, height_ft=2500, gs_kt=140, track_deg=270, vrate_fpm=0,
                         on_ground=False)  # fmt: skip
    assert by < 0.3 < toward


def _route(*legs: tuple[str, float, float], plausible: bool | None = True) -> Route:
    return Route("X1", tuple(RouteLeg(i, None, None, la, lo) for i, la, lo in legs), plausible,
                 "adsb.im", 0)  # fmt: skip


def test_route_fit_picks_current_leg() -> None:
    r = _route(("KPIT", 40.49, -80.23), ("KDEN", 39.86, -104.67), ("CYEG", 53.31, -113.58))
    fit = fit_route(r, 40.2, -95.0)  # over Kansas, on the PIT–DEN leg
    assert fit is not None
    assert (fit.origin, fit.destination, fit.plausible) == ("KPIT", "KDEN", True)


def test_stale_route_is_implausible() -> None:
    # The adsbdb example: "BOS→SFO" for an aircraft sitting at JFK.
    r = _route(("KBOS", 42.36, -71.01), ("KSFO", 37.62, -122.38))
    fit = fit_route(r, 40.64, -73.78)
    assert fit is not None
    assert fit.plausible is False
    assert fit.off_track_nm > 40


def test_cross_track_geometry() -> None:
    xt, at = geo.cross_and_along_track_nm(0.5, 5.0, 0.0, 0.0, 0.0, 10.0)
    assert xt == pytest.approx(-30.0, abs=0.5)
    assert at == pytest.approx(300.0, abs=1.0)


@respx.mock
async def test_route_service_batches_and_caches(tmp_path: object) -> None:
    from pathlib import Path

    db = Database(Path(str(tmp_path)) / "db.sqlite3")
    await db.connect()
    clock = FakeClock()
    route = respx.post(ROUTESET_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"callsign": "UAL1816", "plausible": True, "_airports": [
                    {"icao": "KPIT", "iata": "PIT", "name": "PIT", "lat": 40.49, "lon": -80.23},
                    {"icao": "KDEN", "iata": "DEN", "name": "DEN", "lat": 39.86, "lon": -104.67}]},
                {"callsign": "SAM491", "airport_codes": "unknown"},
            ],
        )
    )  # fmt: skip
    async with httpx.AsyncClient() as http:
        svc = RouteService(db, http, clock)
        svc.want("UAL1816", 38.8, -97.6)
        svc.want("SAM491", 38.8, -97.6)
        assert await svc.flush() == 2
        assert route.call_count == 1
        assert svc.get("UAL1816") is not None and svc.get("UAL1816").known  # type: ignore[union-attr]
        assert not svc.get("SAM491").known  # type: ignore[union-attr]
        svc.want("UAL1816", 38.8, -97.6)  # fresh: not re-requested
        assert svc.pending == {}
        clock.advance(7 * 3600)
        svc.want("SAM491", 38.8, -97.6)  # unknown entries expire sooner
        assert "SAM491" in svc.pending
        assert await svc.flush() == 1
        # Reloaded from the database by a new service instance.
        svc2 = RouteService(db, http, clock)
        await svc2.load()
        assert svc2.get("UAL1816") is not None
    await db.close()


AIRPORTS_CSV = (
    '"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft","continent",'
    '"iso_country","iso_region","municipality","scheduled_service","icao_code","iata_code",'
    '"gps_code","local_code","home_link","wikipedia_link","keywords"\n'
    '3891,"KSLN","medium_airport","Salina Regional",38.791,-97.652,1288,"NA","US","US-KS",'
    '"Salina","yes","KSLN","SLN","KSLN","SLN",,,\n'
    '1,"XX1","seaplane_base","Lake",38.0,-97.0,1000,"NA","US","US-KS","X","no",,,,,,,\n'
)
RUNWAYS_CSV = (
    '"id","airport_ref","airport_ident","length_ft","width_ft","surface","lighted","closed",'
    '"le_ident","le_latitude_deg","le_longitude_deg","le_elevation_ft","le_heading_degT",'
    '"le_displaced_threshold_ft","he_ident","he_latitude_deg","he_longitude_deg",'
    '"he_elevation_ft","he_heading_degT","he_displaced_threshold_ft"\n'
    '1,3891,"KSLN",12301,150,"ASP",1,0,"17",38.8075,-97.6522,1288,180,,"35",38.7738,-97.6522,'
    "1270,360,\n"
)


@respx.mock
async def test_airport_import_and_api(authed: object) -> None:
    from tests.conftest import Harness

    h = authed
    assert isinstance(h, Harness)
    respx.get(ds.OURAIRPORTS_AIRPORTS).mock(return_value=httpx.Response(200, text=AIRPORTS_CSV))
    respx.get(ds.OURAIRPORTS_RUNWAYS).mock(return_value=httpx.Response(200, text=RUNWAYS_CSV))
    rt = h.ctx.rt()
    specs = [s for s in rt.datasets.specs if s.name.startswith("ourairports")]
    for spec in specs:
        assert await rt.datasets.refresh(spec)
    r = (await h.client.get("/api/airports/search?q=salina")).json()
    assert [a["ident"] for a in r] == ["KSLN"]
    detail = (await h.client.get("/api/airports/sln")).json()
    assert detail["elevation_ft"] == 1288
    assert detail["runways"][0]["le_ident"] == "17"
    assert (await h.client.get("/api/airports/XX1")).status_code == 404  # seaplane base skipped
    place = {"name": "KSLN", "role": "airport", "airport_icao": "KSLN", "lat": 38.791,
             "lon": -97.652, "elevation_ft": 1288, "radius_nm": 15}  # fmt: skip
    pid = (await h.client.post("/api/places", json=place)).json()["id"]
    assert [rw.ident for rw in rt.runways[pid]] == ["17", "35"]

import pytest

from app import geo

# R-3601A vertices from 80 FR 60528 (Smoky Hill range), used as a realistic test polygon.
RANGE = [
    (38.7556, -97.7669), (38.6625, -97.7669), (38.6389, -97.7919), (38.6389, -97.8336),
    (38.5833, -97.8336), (38.5833, -97.9336), (38.7556, -97.9336),
]  # fmt: skip


def test_haversine_known_distance() -> None:
    # KSLN to KICT is about 69 nm.
    d = geo.haversine_nm(38.791, -97.652, 37.650, -97.433)
    assert d == pytest.approx(69.3, abs=0.5)


def test_bearing_cardinal() -> None:
    assert geo.bearing_deg(38.0, -97.0, 39.0, -97.0) == pytest.approx(0.0, abs=0.01)
    assert geo.bearing_deg(38.0, -97.0, 38.0, -96.0) == pytest.approx(90.0, abs=0.5)


def test_destination_roundtrip() -> None:
    lat, lon = geo.destination(38.79, -97.65, 225.0, 12.0)
    assert geo.haversine_nm(38.79, -97.65, lat, lon) == pytest.approx(12.0, abs=0.01)
    assert geo.bearing_deg(38.79, -97.65, lat, lon) == pytest.approx(225.0, abs=0.1)


def test_local_projection_roundtrip() -> None:
    x, y = geo.to_local(38.9, -97.4, 38.79, -97.65)
    lat, lon = geo.from_local(x, y, 38.79, -97.65)
    assert (lat, lon) == pytest.approx((38.9, -97.4), abs=1e-6)
    assert (x**2 + y**2) ** 0.5 == pytest.approx(
        geo.haversine_nm(38.9, -97.4, 38.79, -97.65), rel=0.005
    )


def test_point_in_polygon() -> None:
    assert geo.point_in_polygon(38.70, -97.85, RANGE)
    assert not geo.point_in_polygon(38.70, -97.70, RANGE)  # east of the range
    assert not geo.point_in_polygon(38.62, -97.80, RANGE)  # in the notch


def test_elevation_angle() -> None:
    # 1 nm away and 6076 ft up is 45 degrees.
    assert geo.elevation_angle_deg(1.0, geo.FT_PER_NM) == pytest.approx(45.0)
    assert geo.slant_range_nm(3.0, 4 * geo.FT_PER_NM) == pytest.approx(5.0)


def test_angle_diff() -> None:
    assert geo.angle_diff(10, 350) == pytest.approx(20)
    assert geo.angle_diff(350, 10) == pytest.approx(-20)

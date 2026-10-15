"""Sun elevation from the NOAA solar calculator equations (accurate to ~0.1° for 1800–2100).

Pure function of time and position, so replay and tests are deterministic.
"""

import math

# Geometric elevations: sunrise/sunset (allows for refraction and the sun's radius) and the end
# of civil twilight.
SUNRISE_DEG = -0.833
CIVIL_DARK_DEG = -6.0


def sun_elevation_deg(t: float, lat: float, lon: float) -> float:
    """Geometric elevation of the sun's centre above the horizon at epoch time `t`."""
    jd = t / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * jc) + math.sin(3 * mr) * 0.000289)  # fmt: skip
    true_long = l0 + c
    omega = 125.04 - 1934.136 * jc
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    eps = math.radians(eps0 + 0.00256 * math.cos(math.radians(omega)))
    decl = math.asin(math.sin(eps) * math.sin(math.radians(app_long)))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr)
    )  # fmt: skip
    minutes_utc = (t % 86400.0) / 60.0
    true_solar = (minutes_utc + eq_time + 4 * lon) % 1440
    hour_angle = true_solar / 4 - 180 if true_solar >= 0 else true_solar / 4 + 180
    latr = math.radians(lat)
    cos_zenith = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(
        math.radians(hour_angle)
    )
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zenith))))
    return 90.0 - zenith

"""How low the data sources see aircraft near home: lowest height seen per distance band, by day.

Tells the user early whether low-altitude traffic near home is covered by the public feeds.
"""

from typing import Any

BANDS_NM = [(0, 5), (5, 10), (10, 20), (20, 40), (40, 80), (80, 250)]
KEEP_DAYS = 14


class Coverage:
    def __init__(self) -> None:
        # epoch day (UTC) → band index → [min_height_ft or None, count, ground_count]
        self.days: dict[int, dict[int, list[Any]]] = {}

    def load(self, data: dict[str, Any]) -> None:
        self.days = {
            int(day): {int(b): list(v) for b, v in bands.items()} for day, bands in data.items()
        }

    def to_json(self) -> dict[str, Any]:
        return {str(d): {str(b): v for b, v in bands.items()} for d, bands in self.days.items()}

    def add(
        self, now: float, dist_nm: float, alt_ft: float | None, elev_ft: float, on_ground: bool
    ) -> None:
        band = next((i for i, (lo, hi) in enumerate(BANDS_NM) if lo <= dist_nm < hi), None)
        if band is None:
            return
        day = int(now // 86400)
        bands = self.days.setdefault(day, {})
        if len(self.days) > KEEP_DAYS:
            del self.days[min(self.days)]
        stat = bands.setdefault(band, [None, 0, 0])
        stat[1] += 1
        height = 0.0 if on_ground else (alt_ft - elev_ft if alt_ft is not None else None)
        if on_ground:
            stat[2] += 1
        if height is not None and (stat[0] is None or height < stat[0]):
            stat[0] = height

    def report(self) -> list[dict[str, Any]]:
        out = []
        for i, (lo, hi) in enumerate(BANDS_NM):
            lows = [b[i][0] for b in self.days.values() if i in b and b[i][0] is not None]
            count = sum(b[i][1] for b in self.days.values() if i in b)
            ground = sum(b[i][2] for b in self.days.values() if i in b)
            out.append(
                {
                    "band": f"{lo}–{hi} nm",
                    "lowest_height_ft": round(min(lows)) if lows else None,
                    "reports": int(count),
                    "ground_reports": int(ground),
                }
            )
        return out

"""Aircraft photos for the web UI.

plane-alert-db links come first. Otherwise planespotters.net, whose API terms require the
photographer credit and a plain link to the photo page next to the image, so its photos are
shown only in the web UI (never in phone notifications, which can't carry the link).
"""

import json
import re
from typing import Any

import httpx

from app.clock import Clock
from app.db import Database

PLANESPOTTERS = "https://api.planespotters.net/pub/photos/hex/{hex}"
FOUND_TTL_S = 7 * 86400.0
MISSING_TTL_S = 86400.0
_HEX = re.compile(r"^[0-9a-f]{6}$")


async def planespotters_photo(
    db: Database, http: httpx.AsyncClient, clock: Clock, hex_: str, user_agent: str
) -> dict[str, Any] | None:
    if not _HEX.match(hex_):
        return None
    now = clock.now()
    row = await db.fetchone(
        "SELECT value, fetched_at FROM cache WHERE namespace = 'planespotters' AND key = ?",
        (hex_,),
    )
    if row is not None:
        value = json.loads(row["value"]) if row["value"] else None
        ttl = FOUND_TTL_S if value else MISSING_TTL_S
        if now - row["fetched_at"] < ttl:
            return value
    try:
        resp = await http.get(
            PLANESPOTTERS.format(hex=hex_), headers={"User-Agent": user_agent}, timeout=10
        )
        photos = resp.json().get("photos", []) if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        photos = None
    if photos is None:
        return None  # transient failure: don't cache
    value = None
    if photos:
        p = photos[0]
        thumb = p.get("thumbnail_large") or p.get("thumbnail") or {}
        if thumb.get("src") and p.get("link"):
            value = {
                "source": "planespotters",
                "src": thumb["src"],
                "link": p["link"],
                "credit": p.get("photographer") or "unknown photographer",
            }
    await db.execute(
        "INSERT OR REPLACE INTO cache(namespace, key, value, fetched_at) VALUES (?, ?, ?, ?)",
        ("planespotters", hex_, json.dumps(value) if value else None, now),
    )
    return value

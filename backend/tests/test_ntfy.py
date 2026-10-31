"""ntfy payloads, signed notification buttons and the action endpoint."""

from zoneinfo import ZoneInfo

from app.notify import actions
from app.notify.base import OutgoingNotification
from app.notify.ntfy import NtfyClient, NtfyConfig, NtfyNotifier, ntfy_payload
from tests.conftest import Harness

SECRET = "s3cret"
NOW = 1_800_000_000.0


def note(**kw: object) -> OutgoingNotification:
    base: dict[str, object] = dict(
        hex="ae1234", title="✈ C-17 · in ~4 min", message="RCH452 · USAF", channel="Aircraft",
        priority="high", image="https://example.org/c17.jpg",
        click_url="https://adsb.example.com/#/aircraft/ae1234", military=True, alert_ids=(7, 8),
    )  # fmt: skip
    base.update(kw)
    return OutgoingNotification(**base)  # type: ignore[arg-type]


def notifier(public_url: str | None = "https://adsb.example.com") -> NtfyNotifier:
    client = NtfyClient(None, NtfyConfig("http://ntfy", "planes"))  # type: ignore[arg-type]
    return NtfyNotifier(client, ZoneInfo("America/Chicago"), SECRET, public_url, lambda: NOW)


def test_payload_maps_everything_and_signs_buttons() -> None:
    n = notifier()
    p = ntfy_payload(note(eta_at=NOW + 240), n.tz, n.action_url)
    assert p["priority"] == 4 and p["tags"] == ["airplane", "military_helmet"]
    assert p["sequence_id"] == "aircraft-ae1234"
    assert p["click"].endswith("/#/aircraft/ae1234") and p["attach"].endswith("c17.jpg")
    # 1_800_000_240 = 2027-01-15 08:04 UTC = 02:04 in Chicago.
    assert p["message"].startswith("ETA 02:04\n")
    labels = [a["label"] for a in p["actions"]]
    assert labels == ["Open", "Mute 1 day", "Not useful"]
    mute = p["actions"][1]
    assert mute["action"] == "http" and mute["clear"] is True
    token = mute["url"].rsplit("/", 1)[1]
    assert actions.verify(SECRET, token, NOW) == ("mute", "ae1234")
    down = p["actions"][2]["url"].rsplit("/", 1)[1]
    assert actions.verify(SECRET, down, NOW) == ("down", "7,8")


def test_payload_without_public_url_has_no_buttons_and_urgent_maps_to_max() -> None:
    n = notifier(None)
    p = ntfy_payload(note(click_url=None, alarm_stream=True, image=None), n.tz, n.action_url)
    assert "actions" not in p and "click" not in p and "attach" not in p
    assert p["priority"] == 5


def test_tokens_reject_tampering_expiry_and_bad_values() -> None:
    token = actions.sign(SECRET, "mute", "ae1234", NOW)
    assert actions.verify("other-secret", token, NOW) is None
    assert actions.verify(SECRET, token, NOW + actions.TOKEN_TTL_S + 1) is None
    _body, sig = token.split(".")
    forged = actions._b64(b"mute:ae9999:9999999999") + "." + sig
    assert actions.verify(SECRET, forged, NOW) is None
    assert actions.verify(SECRET, "garbage", NOW) is None
    for bad in (("mute", "not-a-hex"), ("delete", "x"), ("down", "1;DROP")):
        try:
            actions.sign(SECRET, *bad, NOW)
        except ValueError:
            continue
        raise AssertionError(bad)


async def test_action_endpoint_mutes_and_rates_without_a_session(harness: Harness) -> None:
    # No account, no session, no CSRF token: the signed token is the authorization.
    h = harness
    now = h.clock.now()
    mute = actions.sign(h.ctx.secret_key, "mute", "ae1234", now)
    r = await h.client.post(f"/api/notify/action/{mute}")
    assert r.status_code == 200 and r.json() == {"ok": True, "action": "mute"}
    rt = h.ctx.rt()
    assert any(e.kind == "hex" and e.value == "ae1234" for e in rt.exclusions)
    alert_id = await h.ctx.db.insert(
        "INSERT INTO alerts(filter_name, mode, hex, state, started_at, last_matched_at) "
        "VALUES ('f', 'live', 'ae1234', 'ended', ?, ?)",
        (now, now),
    )
    down = actions.sign(h.ctx.secret_key, "down", str(alert_id), now)
    assert (await h.client.post(f"/api/notify/action/{down}")).status_code == 200
    row = await h.ctx.db.fetchone("SELECT feedback FROM alerts WHERE id = ?", (alert_id,))
    assert row is not None and row["feedback"] == "down"
    assert (await h.client.post("/api/notify/action/forged.token")).status_code == 404
    h.clock.advance(actions.TOKEN_TTL_S + 1)
    assert (await h.client.post(f"/api/notify/action/{mute}")).status_code == 404

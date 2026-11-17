"""FAA SWIM Cloud Distribution Service (SCDS): TFMS flight data ("TFMData", `tfmDataService`).

A schedule source: filed flight plans and flight times for US IFR flights, often hours before
departure. It has no positions we use (ADS-B has better ones). This module only connects,
receives and normalizes. Filtering by your places, the bounded queue between the Solace thread
and asyncio, reconnects and storage live in `app.ingest.swim`.

Message shape (TFMS R13/R14, matched by local name because namespace prefixes vary):
`tfmDataService > fltdOutput > fltdMessage` with the flight's identity as attributes (`acid`,
`depArpt`, `arrArpt`, `msgType`, `flightRef`, `sourceTimeStamp`) and one body element
(`flightPlanInformation`, `ncsmFlightModify`, `ncsmFlightTimes`, `trackInformation`,
`departureInformation`, `arrivalInformation`, `flightPlanCancellation`, ...). Bodies carry
`qualifiedAircraftId` (`aircraftId`, `departurePoint/airport`, `arrivalPoint/airport`, `gufi`,
`igtd`), `etd`/`eta` with a `timeValue` and a type, the aircraft type (`aircraftModel` or
`flightAircraftSpecs`) and `flightStatus`. Airports come as ICAO (KDEN) or FAA (DEN) codes.
`fiOutput` (flow information) and status messages are ignored.
"""

from __future__ import annotations

import importlib
import logging
import re
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import SourceError

log = logging.getLogger(__name__)

NAME = "faa-swim"
# Larger documents are dropped: TFMS documents are 1–210 kB; a runaway one must not stall us.
MAX_PAYLOAD_BYTES = 2_000_000
MAX_FIELD_LEN = 16

# Accepts (dep, arr, acid) from a message header; False skips the message body.
Wanted = Callable[[str | None, str | None, str], bool]


@dataclass(frozen=True, slots=True)
class FlightUpdate:
    """What one `fltdMessage` says about a flight. None means "this message doesn't say"."""

    key: str  # TFMS flight reference, else gufi, else acid/dep/arr
    acid: str  # aircraft identification as filed: callsign, or the registration for GA
    msg_type: str
    sent_at: float  # sourceTimeStamp, UTC epoch s
    origin: str | None = None  # as published: ICAO (KDEN) or FAA (DEN)
    destination: str | None = None
    gufi: str | None = None
    aircraft_type: str | None = None
    status: str | None = None  # TFMS flight status, e.g. SCHEDULED, ACTIVE, COMPLETED
    scheduled_departure: float | None = None  # igtd: initial gate time of departure
    etd: float | None = None
    etd_type: str | None = None  # e.g. SCHEDULED, ESTIMATED, ACTUAL
    eta: float | None = None
    eta_type: str | None = None
    departed_at: float | None = None  # actual (not estimated) departure report
    arrived_at: float | None = None  # actual arrival report
    cancelled: bool = False


@dataclass(slots=True)
class ParseResult:
    updates: list[FlightUpdate]
    messages: int = 0  # fltdMessage elements seen
    skipped: int = 0  # not wanted (not one of your airports or aircraft)
    errors: int = 0


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _code(v: str | None) -> str | None:
    v = (v or "").strip().upper()
    return v[:MAX_FIELD_LEN] if v and v.isalnum() else None


def parse_time(v: str | None) -> float | None:
    """TFMS times are `YYYY-MM-DDTHH:MM:SS[.fff]Z`."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if dt.tzinfo is None else dt.timestamp()


def airport_aliases(code: str) -> set[str]:
    """TFMS writes US airports as ICAO (KDEN, PANC) or FAA (DEN, ANC) codes."""
    c = code.strip().upper()
    out = {c} if c else set()
    if len(c) == 4 and c[0] in "KP":
        out.add(c[1:])
    return out


def normalize_acid(v: str | None) -> str | None:
    """Callsigns and registrations compare without spaces or dashes (N-numbers file as N123AB)."""
    if not v:
        return None
    s = re.sub(r"[\s-]", "", v).upper()
    return s[:MAX_FIELD_LEN] if s and s.isalnum() else None


def decode_payload(payload: bytes | str | None) -> bytes | None:
    """Bytes of an XML document, inflated if gzip/zlib-compressed; None if empty or too big."""
    if payload is None:
        return None
    data = payload.encode() if isinstance(payload, str) else bytes(payload)
    if data[:2] == b"\x1f\x8b" or data[:1] == b"\x78":
        d = zlib.decompressobj(zlib.MAX_WBITS | 32)  # gzip or zlib header
        try:
            data = d.decompress(data, MAX_PAYLOAD_BYTES + 1)
        except zlib.error:
            return None
    if not data or len(data) > MAX_PAYLOAD_BYTES:
        return None
    return data


def _find(el: ET.Element, name: str) -> ET.Element | None:
    for e in el.iter():
        if _local(e.tag) == name:
            return e
    return None


def _text(el: ET.Element, name: str) -> str | None:
    e = _find(el, name)
    if e is None or e.text is None:
        return None
    return e.text.strip() or None


def _time_el(body: ET.Element, name: str) -> tuple[float | None, str | None]:
    e = _find(body, name)
    if e is None:
        return None, None
    t = parse_time(e.get("timeValue") or e.text)
    kind = e.get(f"{name}Type")
    return t, kind.strip().upper()[:MAX_FIELD_LEN] if kind else None


def _actual(body: ET.Element, name: str) -> float | None:
    e = _find(body, name)
    if e is None or (e.get("estimated") or "").lower() == "true":
        return None
    return parse_time(e.text)


def _aircraft_type(body: ET.Element) -> str | None:
    for name in ("aircraftModel", "flightAircraftSpecs", "newFlightAircraftSpecs"):
        t = _text(body, name)
        # Specs may carry a weight/equipment prefix and suffix, e.g. "H/B744/L".
        if t:
            parts = [p for p in t.split("/") if p]
            core = max(parts, key=len) if parts else t
            return _code(core)
    return None


def _point(body: ET.Element, name: str) -> str | None:
    e = _find(body, name)
    return _code(_text(e, "airport")) if e is not None else None


class _UnwantedError(Exception):
    """The message is about an airport and aircraft you don't follow."""


def parse_flight_message(m: ET.Element, wanted: Wanted | None = None) -> FlightUpdate | None:
    acid = normalize_acid(m.get("acid"))
    dep, arr = _code(m.get("depArpt")), _code(m.get("arrArpt"))
    msg_type = (m.get("msgType") or "").strip()[:40]
    sent_at = parse_time(m.get("sourceTimeStamp"))
    body = next(iter(m), None)
    if body is None or sent_at is None:
        return None
    qid = _find(body, "qualifiedAircraftId")
    if qid is not None:
        acid = acid or normalize_acid(_text(qid, "aircraftId"))
        dep = dep or _point(qid, "departurePoint")
        arr = arr or _point(qid, "arrivalPoint")
    if not acid:
        return None
    if wanted is not None and not wanted(dep, arr, acid):
        raise _UnwantedError
    gufi = _text(qid, "gufi") if qid is not None else None
    igtd = parse_time(_text(qid, "igtd")) if qid is not None else None
    ref = (m.get("flightRef") or "").strip()
    key = f"ref:{ref[:20]}" if ref.isdigit() else f"gufi:{gufi[:40]}" if gufi else (
        f"id:{acid}/{dep or ''}/{arr or ''}"
    )  # fmt: skip
    etd, etd_type = _time_el(body, "etd")
    eta, eta_type = _time_el(body, "eta")
    status = _text(body, "flightStatus")
    return FlightUpdate(
        key=key, acid=acid, msg_type=msg_type or _local(body.tag), sent_at=sent_at,
        origin=dep, destination=arr, gufi=gufi[:40] if gufi else None,
        aircraft_type=_aircraft_type(body),
        status=status.upper()[:MAX_FIELD_LEN] if status else None,
        scheduled_departure=igtd, etd=etd, etd_type=etd_type, eta=eta, eta_type=eta_type,
        departed_at=_actual(body, "timeOfDeparture"),
        arrived_at=_actual(body, "timeOfArrival"),
        cancelled=_local(body.tag) == "flightPlanCancellation",
    )  # fmt: skip


def _flight_messages(root: ET.Element) -> Iterator[ET.Element]:
    for out in root:
        if _local(out.tag) == "fltdOutput":
            for m in out:
                if _local(m.tag) == "fltdMessage":
                    yield m
    if _local(root.tag) == "fltdMessage":
        yield root


def parse_payload(payload: bytes | str | None, wanted: Wanted | None = None) -> ParseResult:
    """One SCDS message (one XML document, often several flight messages). Pure."""
    data = decode_payload(payload)
    if data is None:
        return ParseResult([], errors=1)
    try:
        # Documents come from the FAA's authenticated broker; expat (≥2.4) caps entity
        # expansion and ElementTree never fetches external entities.
        root = ET.fromstring(data)  # noqa: S314
    except ET.ParseError:
        return ParseResult([], errors=1)
    result = ParseResult([])
    for m in _flight_messages(root):
        result.messages += 1
        try:
            u = parse_flight_message(m, wanted)
        except _UnwantedError:
            result.skipped += 1
            continue
        except (ValueError, OverflowError):
            u = None
        if u is None:
            result.errors += 1
        else:
            result.updates.append(u)
    return result


def interest_pattern(airports: set[str], acids: set[str]) -> re.Pattern[bytes] | None:
    """A byte regex that finds documents mentioning one of your airports or aircraft in a
    message header, so the national stream is filtered before any XML parsing."""
    alts: list[bytes] = []
    codes = sorted({a for c in airports for a in airport_aliases(c) if a.isalnum()})
    if codes:
        alts.append(rb"(?:depArpt|arrArpt)=[\"'](?:" + b"|".join(c.encode() for c in codes)
                    + rb")[\"']")  # fmt: skip
    ids = sorted(a for a in acids if a.isalnum())
    if ids:
        alts.append(rb"acid=[\"'](?:" + b"|".join(a.encode() for a in ids) + rb")[\"']")
    return re.compile(b"|".join(alts)) if alts else None


# ---- connection (Solace PubSub+, blocking: called from worker threads) ---------------------


@dataclass(frozen=True, slots=True)
class SwimConfig:
    host: str  # e.g. tcps://<scds-host>:55443
    vpn: str
    username: str
    password: str
    queue: str
    trust_store_dir: str = "/etc/ssl/certs"


class SolaceConnector:
    """Receives from one SCDS queue. `connect`, `is_connected` and `close` block; the ingest
    layer calls them from worker threads. Each message's payload goes to `on_payload` on
    Solace's own dispatch thread, which must return quickly (it's acknowledged afterwards)."""

    def __init__(self, cfg: SwimConfig, on_payload: Callable[[bytes], None]) -> None:
        self.cfg = cfg
        self.on_payload = on_payload
        self._service: Any = None
        self._receiver: Any = None

    @staticmethod
    def available() -> bool:
        try:
            importlib.import_module("solace.messaging.messaging_service")
        except ImportError:
            return False
        return True

    def connect(self) -> None:
        try:
            ms = importlib.import_module("solace.messaging.messaging_service")
            retry = importlib.import_module("solace.messaging.config.retry_strategy")
            tls = importlib.import_module("solace.messaging.config.transport_security_strategy")
            queue = importlib.import_module("solace.messaging.resources.queue")
            receiver_mod = importlib.import_module("solace.messaging.receiver.message_receiver")
        except ImportError as e:
            raise SourceError(
                "the Solace client library is not installed (install the 'swim' extra)"
            ) from e
        props = {
            "solace.messaging.transport.host": self.cfg.host,
            "solace.messaging.service.vpn-name": self.cfg.vpn,
            "solace.messaging.authentication.basic.username": self.cfg.username,
            "solace.messaging.authentication.basic.password": self.cfg.password,
            # SCDS expects compressed transport.
            "solace.messaging.transport.compression-level": 1,
        }
        security = tls.TLS.create().with_certificate_validation(
            False, validate_server_name=True, trust_store_file_path=self.cfg.trust_store_dir
        )
        on_payload = self.on_payload

        def on_message(_handler: Any, message: Any) -> None:
            try:
                payload = message.get_payload_as_bytes()
                if payload is None:
                    text = message.get_payload_as_string()
                    payload = text.encode() if text else b""
                on_payload(bytes(payload))
            except Exception:  # Solace swallows callback errors silently
                log.exception("SWIM message handler failed")

        handler_cls: Any = type("SwimHandler", (receiver_mod.MessageHandler,), {
            "on_message": on_message,
        })  # fmt: skip

        # A few quick retries inside the library; longer outages back off in app.ingest.swim.
        service = (
            ms.MessagingService.builder()
            .from_properties(props)
            .with_reconnection_retry_strategy(retry.RetryStrategy.parametrized_retry(3, 5000))
            .with_transport_security_strategy(security)
            .build()
        )
        self._service = service
        service.connect()
        receiver = (
            service.create_persistent_message_receiver_builder()
            .with_message_auto_acknowledgement()
            .build(queue.Queue.durable_non_exclusive_queue(self.cfg.queue))
        )
        receiver.start()
        receiver.receive_async(handler_cls())
        self._receiver = receiver

    def is_connected(self) -> bool:
        svc = self._service
        return bool(svc is not None and svc.is_connected)

    def close(self) -> None:
        receiver, service = self._receiver, self._service
        self._receiver = self._service = None
        try:
            if receiver is not None:
                receiver.terminate(5000)
        except Exception as e:  # already gone
            log.debug("SWIM receiver close: %s", e)
        try:
            if service is not None:
                service.disconnect()
        except Exception as e:
            log.debug("SWIM disconnect: %s", e)

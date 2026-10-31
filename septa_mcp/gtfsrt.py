"""Reads SEPTA's GTFS-realtime service alerts (protocol buffers) without a protobuf library.

Only the fields alerts use are read: FeedMessage.entity -> FeedEntity.alert -> Alert's
active periods, informed entities, cause, effect, severity, url and texts. See
https://gtfs.org/documentation/realtime/proto/ for the field numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CAUSES = {
    1: "unknown", 2: "other", 3: "technical problem", 4: "strike", 5: "demonstration",
    6: "accident", 7: "holiday", 8: "weather", 9: "maintenance", 10: "construction",
    11: "police activity", 12: "medical emergency",
}
EFFECTS = {
    1: "no service", 2: "reduced service", 3: "significant delays", 4: "detour",
    5: "additional service", 6: "modified service", 7: "other", 8: "unknown",
    9: "stop moved", 10: "no effect", 11: "accessibility issue",
}
SEVERITIES = {1: "unknown", 2: "info", 3: "warning", 4: "severe"}


@dataclass
class Alert:
    id: str = ""
    periods: list[tuple[int | None, int | None]] = field(default_factory=list)  # POSIX seconds
    route_ids: list[str] = field(default_factory=list)
    stop_ids: list[str] = field(default_factory=list)
    trip_ids: list[str] = field(default_factory=list)
    cause: str | None = None
    effect: str | None = None
    severity: str | None = None
    url: str | None = None
    header: str | None = None
    description: str | None = None


def _varint(b: bytes, i: int) -> tuple[int, int]:
    v = shift = 0
    while True:
        x = b[i]
        i += 1
        v |= (x & 0x7F) << shift
        shift += 7
        if x < 0x80:
            return v, i


def _fields(b: bytes):
    """(field number, value) for each field of a message: ints for varints, bytes for
    length-delimited fields. Fixed-width fields are skipped."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
            yield num, v
        elif wire == 2:
            n, i = _varint(b, i)
            yield num, b[i:i + n]
            i += n
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _text(b: bytes) -> str | None:
    """A TranslatedString: the English translation, or the first one."""
    found = []
    for num, tr in _fields(b):
        if num == 1:
            parts = dict(_fields(tr))
            text = parts.get(1, b"").decode("utf-8", "replace").strip()
            lang = parts.get(2, b"").decode("utf-8", "replace").lower()
            if text:
                found.append((lang not in ("", "en", "en-us"), text))
    return min(found)[1] if found else None


def _alert(entity_id: str, b: bytes) -> Alert:
    a = Alert(id=entity_id)
    for num, v in _fields(b):
        if num == 1:  # TimeRange
            r = dict(_fields(v))
            a.periods.append((r.get(1), r.get(2)))
        elif num == 5:  # EntitySelector
            for n, x in _fields(v):
                if n == 2:
                    a.route_ids.append(x.decode())
                elif n == 5:
                    a.stop_ids.append(x.decode())
                elif n == 4:  # TripDescriptor
                    trip = dict(_fields(x)).get(1)
                    if trip:
                        a.trip_ids.append(trip.decode())
        elif num == 6:
            a.cause = CAUSES.get(v)
        elif num == 7:
            a.effect = EFFECTS.get(v)
        elif num == 14:
            a.severity = SEVERITIES.get(v)
        elif num == 8:
            a.url = _text(v)
        elif num == 10:
            a.header = _text(v)
        elif num == 11:
            a.description = _text(v)
    return a


def parse_alerts(data: bytes) -> list[Alert]:
    out = []
    for num, entity in _fields(data):
        if num != 2:
            continue
        parts = list(_fields(entity))
        entity_id = next((v.decode() for n, v in parts if n == 1), "")
        if any(n == 2 and v for n, v in parts):  # is_deleted
            continue
        out.extend(_alert(entity_id, v) for n, v in parts if n == 5)
    return out


# -- encoding, for tests and the mock API ---------------------------------------


def _enc_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        x = v & 0x7F
        v >>= 7
        out.append(x | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _enc(num: int, v: int | bytes | str) -> bytes:
    if isinstance(v, int):
        return _enc_varint(num << 3) + _enc_varint(v)
    if isinstance(v, str):
        v = v.encode()
    return _enc_varint(num << 3 | 2) + _enc_varint(len(v)) + v


def encode_alerts(alerts: list[Alert]) -> bytes:
    """A FeedMessage holding these alerts (the inverse of parse_alerts)."""
    rev = {name: {v: k for k, v in table.items()} for name, table in
           (("cause", CAUSES), ("effect", EFFECTS), ("severity", SEVERITIES))}

    def tstr(s: str) -> bytes:
        return _enc(1, _enc(1, s) + _enc(2, "en"))

    header = _enc(1, "2.0") + _enc(3, 0)
    body = _enc(1, header)
    for a in alerts:
        m = b""
        for start, end in a.periods:
            m += _enc(1, (_enc(1, start) if start else b"") + (_enc(2, end) if end else b""))
        for r in a.route_ids:
            m += _enc(5, _enc(2, r))
        for s in a.stop_ids:
            m += _enc(5, _enc(5, s))
        for t in a.trip_ids:
            m += _enc(5, _enc(4, _enc(1, t)))
        for num, name in ((6, "cause"), (7, "effect"), (14, "severity")):
            if getattr(a, name):
                m += _enc(num, rev[name][getattr(a, name)])
        for num, name in ((8, "url"), (10, "header"), (11, "description")):
            if getattr(a, name):
                m += _enc(num, tstr(getattr(a, name)))
        body += _enc(2, _enc(1, a.id) + _enc(5, m))
    return body

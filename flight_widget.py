"""Small offline Telegram message widget; no secrets or extra data-provider requests."""

import json
import zlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def widget_html():
    source = (Path(__file__).parent / "widgets" / "flight_board.html").read_text()
    encoded = source.encode()
    if len(encoded) > 32768 or len(zlib.compress(encoded, 9)) > 4096 or "\x00" in source:
        raise ValueError("Flight widget exceeds Telegram's HTML limits")
    return source


def short_text(value, budget):
    text = str(value)
    if len(text.encode()) <= budget:
        return text
    return text.encode()[:budget - 3].decode(errors="ignore") + "…"


def widget_data(plane, timestamp):
    data = dict(c=short_text(plane.callsign, 48), r=short_text(plane.route, 360),
                a=short_text(plane.aircraft_type, 128),
                h=f"{plane.altitude:,.0f}" if plane.altitude is not None else "—",
                v=f"{plane.speed * 3.6:,.0f}" if plane.speed is not None else "—",
                d=f"{plane.distance:.1f}", t=datetime.fromtimestamp(timestamp, timezone.utc).strftime("%H:%M UTC"))
    state = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(state.encode()) > 1024:
        raise ValueError("Flight widget state exceeds Telegram's limit")
    return state


def widget_caption(plane, timestamp=None):
    altitude = f"{plane.altitude:,.0f} m" if plane.altitude is not None else "unknown altitude"
    speed = f"{plane.speed * 3.6:,.0f} km/h" if plane.speed is not None else "unknown speed"
    stamp = ("\nSnapshot " + datetime.fromtimestamp(timestamp, timezone.utc).strftime("%H:%M UTC")) if timestamp is not None else ""
    return short_text(f"✈ {plane.callsign} · {plane.aircraft_type}\n{plane.route}\n"
                      f"{altitude} · {speed} · {plane.distance:.1f} km away{stamp}", 900)

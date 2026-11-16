"""Airways: location-to-aircraft Telegram bot, using only the Python standard library."""

import html
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG = logging.getLogger("airways")
EARTH_KM = 6371.0088
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
KEYBOARD = {"keyboard": [[{"text": "📍 Share my location", "request_location": True}]],
            "resize_keyboard": True}


class APIError(Exception):
    def __init__(self, status=0, retry_after=5):
        super().__init__(f"API request failed (status {status})")
        self.status = status
        self.retry_after = retry_after


def request_json(url, *, payload=None, form=None, headers=None, timeout=20):
    """Never include request URLs, credentials, or response bodies in errors."""
    headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        delay = error.headers.get("X-Rate-Limit-Retry-After-Seconds", error.headers.get("Retry-After", "5"))
        try:
            body = json.loads(error.read())
            delay = body.get("parameters", {}).get("retry_after", delay)
        except (ValueError, AttributeError):
            pass
        try:
            delay = max(1, float(delay))
        except (TypeError, ValueError):
            delay = 5
        raise APIError(error.code, delay) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise APIError() from None


def valid_location(lat, lon):
    return (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            and math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180)


def distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(min(1, max(0, h))))


def bounding_boxes(lat, lon, radius):
    """Spherical cap bounds, split at the antimeridian and safe at the poles."""
    angle = radius / EARTH_KM
    delta = math.degrees(angle)
    south, north = max(-90, lat - delta), min(90, lat + delta)
    if south == -90 or north == 90:
        ranges = [(-180, 180)]
    else:
        width = math.degrees(math.asin(min(1, math.sin(angle) / math.cos(math.radians(lat)))))
        west, east = lon - width, lon + width
        if west < -180:
            ranges = [(west + 360, 180), (-180, east)]
        elif east > 180:
            ranges = [(west, 180), (-180, east - 360)]
        else:
            ranges = [(west, east)]
    return [dict(lamin=south, lamax=north, lomin=w, lomax=e) for w, e in ranges]


@dataclass
class Aircraft:
    icao: str
    callsign: str
    country: str
    lat: float
    lon: float
    distance: float
    altitude: float | None
    speed: float | None
    heading: float | None
    age: int


def finite_number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def nearby_aircraft(states, lat, lon, radius, now):
    aircraft = {}
    for row in states:
        if not isinstance(row, list) or len(row) < 14:
            continue
        x, y, timestamp = row[5], row[6], row[3]
        if row[8] is not False or not valid_location(y, x) or not finite_number(timestamp):
            continue
        age = now - timestamp
        if age < -30 or age > 120:
            continue
        distance = distance_km(lat, lon, y, x)
        if distance > radius:
            continue
        icao = str(row[0] or "").strip()
        if len(icao) != 6 or any(c not in "0123456789abcdef" for c in icao.lower()):
            continue
        altitude = row[13] if finite_number(row[13]) else row[7]
        aircraft[icao] = Aircraft(
            icao, str(row[1] or "Unknown callsign").strip() or "Unknown callsign",
            str(row[2] or "Unknown"), y, x, distance,
            altitude if finite_number(altitude) else None,
            row[9] if finite_number(row[9]) else None,
            row[10] if finite_number(row[10]) else None, max(0, int(age)))
    return sorted(aircraft.values(), key=lambda plane: plane.distance)


class OpenSky:
    def __init__(self, client_id="", client_secret=""):
        self.client_id, self.client_secret = client_id, client_secret
        self.token, self.expires_at, self.blocked_until = "", 0, 0

    def headers(self):
        if not self.client_id:
            return {}
        if time.monotonic() >= self.expires_at:
            response = request_json(TOKEN_URL, form={"grant_type": "client_credentials",
                                    "client_id": self.client_id, "client_secret": self.client_secret})
            if not response.get("access_token"):
                raise APIError(401)
            self.token = response["access_token"]
            self.expires_at = time.monotonic() + max(0, float(response.get("expires_in", 1800)) - 30)
        return {"Authorization": f"Bearer {self.token}"}

    def nearby(self, lat, lon, radius):
        if time.monotonic() < self.blocked_until:
            raise APIError(429, self.blocked_until - time.monotonic())
        states = []
        try:
            for box in bounding_boxes(lat, lon, radius):
                url = "https://opensky-network.org/api/states/all?" + urlencode(box)
                try:
                    data = request_json(url, headers=self.headers())
                except APIError as error:
                    if error.status != 401 or not self.client_id:
                        raise
                    self.expires_at = 0
                    data = request_json(url, headers=self.headers())
                states.extend(data.get("states") or [])
        except APIError as error:
            if error.status == 429:
                self.blocked_until = time.monotonic() + error.retry_after
            raise
        now = time.time()
        return nearby_aircraft(states, lat, lon, radius, now), now


def format_report(aircraft, radius, timestamp):
    stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%H:%M UTC")
    if not aircraft:
        return (f"No recently reported airborne aircraft within {radius:g} km.\n"
                f"Checked {stamp}. Coverage can be incomplete; try again shortly.")
    lines = [f"✈️ <b>{len(aircraft)} aircraft within {radius:g} km</b>", f"Checked {stamp} · nearest first"]
    for plane in aircraft[:8]:
        altitude = f"{plane.altitude:,.0f} m" if plane.altitude is not None else "unknown"
        speed = f"{plane.speed * 3.6:.0f} km/h" if plane.speed is not None else "unknown"
        heading = f"{plane.heading:.0f}°" if plane.heading is not None else "unknown"
        lines.append(
            f'\n<b>{html.escape(plane.callsign[:32])}</b> · {plane.distance:.1f} km away\n'
            f'Altitude {altitude} · Speed {speed}\nHeading {heading} · Position {plane.age}s ago\n'
            f'ICAO {plane.icao} · Registered country: {html.escape(plane.country[:64])}\n'
            f'<a href="https://www.google.com/maps/search/?api=1&amp;query={plane.lat},{plane.lon}">Map</a> · '
            f'<a href="https://globe.adsbexchange.com/?icao={plane.icao}">Aircraft tracker</a>')
    if len(aircraft) > 8:
        lines.append(f"\nShowing the nearest 8 of {len(aircraft)} aircraft.")
    lines.append("\nSource: OpenSky Network · coverage may be incomplete.\nRoutes and destinations aren't available in this feed.")
    return "\n".join(lines)


class Bot:
    def __init__(self, token, provider, radius=50):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.provider, self.radius = provider, radius
        self.cooldowns = {}

    def call(self, method, **payload):
        data = request_json(self.base + method, payload=payload, timeout=40)
        if not data.get("ok"):
            raise APIError(data.get("error_code", 0), data.get("parameters", {}).get("retry_after", 5))
        return data["result"]

    def send(self, chat_id, text, keyboard=False):
        payload = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                       link_preview_options={"is_disabled": True})
        if keyboard:
            payload["reply_markup"] = KEYBOARD
        return self.call("sendMessage", **payload)

    def handle(self, update):
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message["chat"]
        chat_id = chat["id"]
        if chat.get("type") != "private":
            self.send(chat_id, "Please open a private chat with me to share your location.")
            return
        location = message.get("location")
        if not location:
            if "edited_message" in update:
                return
            self.send(chat_id,
                      f"✈️ <b>Airways</b>\nShare your location to find airborne aircraft within {self.radius:g} km.\n\n"
                      "Tap the button below, or use 📎 → Location. Each location message returns a snapshot; "
                      "live location edits also refresh the results (at most once every 15 seconds).\n\n"
                      "I don't save your location. The search area is sent to OpenSky to look up aircraft. "
                      "Use /help to see these instructions again.", keyboard=True)
            return
        lat, lon = location.get("latitude"), location.get("longitude")
        if not valid_location(lat, lon):
            self.send(chat_id, "Please send a valid location using 📎 → Location.")
            return
        now = time.monotonic()
        self.cooldowns = {key: expiry for key, expiry in self.cooldowns.items() if expiry > now}
        if chat_id in self.cooldowns:
            if "edited_message" not in update:
                self.send(chat_id, "Please wait 15 seconds between flight searches.")
            return
        self.cooldowns[chat_id] = now + 15
        try:
            aircraft, timestamp = self.provider.nearby(lat, lon, self.radius)
            report = format_report(aircraft, self.radius, timestamp)
        except APIError as error:
            LOG.warning("Aircraft lookup failed (status %s)", error.status)
            if error.status == 429:
                report = f"The flight data limit has been reached. Try again in {math.ceil(error.retry_after)} seconds."
            else:
                report = "Flight data is temporarily unavailable. Please send your location again later."
        self.send(chat_id, report)

    def run(self):
        self.call("getMe")
        if self.call("getWebhookInfo").get("url"):
            raise SystemExit("A webhook is configured. Remove it before running this polling bot.")
        self.call("setMyCommands", commands=[{"command": "start", "description": "Find nearby aircraft"},
                                           {"command": "help", "description": "How to share your location"}])
        LOG.info("Airways is polling for location messages")
        offset, backoff = 0, 1
        while True:
            try:
                updates = self.call("getUpdates", offset=offset, timeout=25,
                                    allowed_updates=["message", "edited_message"])
                for update in updates:
                    # Failed sends don't stall the whole bot or repeat expensive searches.
                    try:
                        self.handle(update)
                    except APIError as error:
                        LOG.warning("Reply failed (status %s)", error.status)
                        if error.status == 429:
                            time.sleep(error.retry_after)
                    offset = update["update_id"] + 1
                backoff = 1
            except APIError as error:
                if error.status in (401, 404, 409):
                    raise SystemExit(f"Telegram startup/polling failed ({error.status}). Check the token and run only one bot instance.") from None
                LOG.warning("Telegram unavailable (status %s); retrying", error.status)
                time.sleep(max(backoff, error.retry_after) if error.status == 429 else backoff)
                backoff = min(30, backoff * 2)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    client_id = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (see .env.example and README.md).")
    if bool(client_id) != bool(client_secret):
        raise SystemExit("Set both OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET, or neither.")
    try:
        radius = float(os.environ.get("SEARCH_RADIUS_KM", "50"))
        if not math.isfinite(radius) or not 1 <= radius <= 250:
            raise ValueError
    except ValueError:
        raise SystemExit("SEARCH_RADIUS_KM must be a number between 1 and 250.") from None
    try:
        Bot(token, OpenSky(client_id, client_secret), radius).run()
    except KeyboardInterrupt:
        LOG.info("Airways stopped")
    except APIError as error:
        raise SystemExit(f"Telegram setup failed (status {error.status}). Check connectivity and bot credentials.") from None


if __name__ == "__main__":
    main()

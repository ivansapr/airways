"""Airways: location-to-aircraft Telegram bot, using only the Python standard library."""

import html
import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from tracking import WatchStore

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
    aircraft_type: str = "Unknown"
    route: str = "Unknown"
    photo_url: str = ""


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


def aircraft_name(aircraft):
    manufacturer = str(aircraft.get("manufacturer") or "").strip()
    for brand in ("Boeing", "Airbus", "Embraer", "Cessna", "Bombardier", "Dassault", "Beechcraft", "ATR"):
        if brand.casefold() in manufacturer.casefold():
            manufacturer = brand
            break
    model = str(aircraft.get("type") or "").strip()
    if model:
        if manufacturer and model.casefold().startswith(manufacturer.casefold()):
            return (manufacturer + model[len(manufacturer):])[:80]
        return " ".join(part for part in (manufacturer, model) if part)[:80]
    code = str(aircraft.get("icao_type") or "").strip().upper()
    names = {"B737": "Boeing 737-700", "B738": "Boeing 737-800", "B739": "Boeing 737-900",
             "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9",
             "B744": "Boeing 747-400", "B748": "Boeing 747-8", "B77W": "Boeing 777-300ER",
             "B788": "Boeing 787-8", "B789": "Boeing 787-9", "A319": "Airbus A319",
             "A320": "Airbus A320", "A321": "Airbus A321", "A20N": "Airbus A320neo",
             "A21N": "Airbus A321neo", "A359": "Airbus A350-900", "A388": "Airbus A380-800"}
    return names.get(code, " ".join(part for part in (manufacturer, code) if part) or "Unknown")[:80]


class FlightDetails:
    """Best-effort adsbdb lookups; cache only in memory, never build a route database."""

    def __init__(self):
        self.cache = {}
        self.lock = Lock()

    def enrich(self, plane):
        key = (plane.icao, plane.callsign)
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(key)
        if cached and cached[0] > now:
            plane.aircraft_type, plane.route, plane.photo_url = cached[1:]
            return
        url = f"https://api.adsbdb.com/v0/aircraft/{plane.icao}?" + urlencode({"callsign": plane.callsign})
        ttl = 300
        try:
            data = request_json(url, timeout=4).get("response", {})
            if not isinstance(data, dict):
                data = {}
            aircraft = data.get("aircraft") or {}
            route = data.get("flightroute") or {}
            plane.aircraft_type = aircraft_name(aircraft)
            photo = aircraft.get("url_photo") or aircraft.get("url_photo_thumbnail") or ""
            if isinstance(photo, str):
                parsed = urlsplit(photo)
                if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                    plane.photo_url = photo
            airports = [route.get(part) or {} for part in ("origin", "midpoint", "destination")]
            cities = [str(port.get("municipality") or port.get("name") or "").strip()[:80] for port in airports]
            if cities[0] or cities[-1]:
                plane.route = " → ".join([cities[0] or "Unknown"] + ([cities[1]] if cities[1] else []) + [cities[-1] or "Unknown"])
            ttl = 3600
        except (APIError, AttributeError, TypeError, ValueError):
            pass  # Metadata must never prevent an alert with live position data.
        with self.lock:
            if len(self.cache) >= 2048:
                self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
                if len(self.cache) >= 2048:
                    self.cache.clear()
            self.cache[key] = (now + ttl, plane.aircraft_type, plane.route, plane.photo_url)

    def enrich_all(self, planes):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.enrich, planes))


def format_report(aircraft, radius, timestamp, alert=False):
    stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%H:%M UTC")
    if not aircraft:
        return (f"No recently reported airborne aircraft within {radius:g} km.\n"
                f"Checked {stamp}. Coverage can be incomplete; try again shortly.")
    title = "Passing nearby" if alert else f"{len(aircraft)} aircraft within {radius:g} km"
    lines = [f"✈️ <b>{title}</b>", f"Checked {stamp} · nearest first"]
    for plane in aircraft[:8]:
        altitude = f"{plane.altitude:,.0f} m" if plane.altitude is not None else "unknown"
        speed = f"{plane.speed * 3.6:.0f} km/h" if plane.speed is not None else "unknown"
        lines.append(
            f'\n<b>{html.escape(plane.callsign[:32])}</b> · {plane.distance:.1f} km away\n'
            f'Route: {html.escape(plane.route)}\nAircraft: {html.escape(plane.aircraft_type[:80])}\n'
            f'Altitude {altitude} · Speed {speed}\n'
            f'<a href="https://www.google.com/maps/search/?api=1&amp;query={plane.lat:.5f},{plane.lon:.5f}">Map</a> · '
            f'<a href="https://globe.adsbexchange.com/?icao={plane.icao}">Aircraft tracker</a>')
    if len(aircraft) > 8:
        lines.append(f"\nShowing the nearest 8 of {len(aircraft)} aircraft.")
    lines.append("\nOpenSky Network · coverage may be incomplete.\nRoute/type: adsbdb · route is a callsign lookup, may be outdated.")
    return "\n".join(lines)


class Bot:
    def __init__(self, token, provider, radius=50, store=None, details=None, interval=60):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.provider, self.radius = provider, radius
        self.cooldowns = {}
        self.store = store if store is not None else WatchStore()
        self.details = details
        self.interval = interval
        self.username = ""
        self.telegram_blocked_until = 0

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

    def send_aircraft(self, chat_id, plane, radius, timestamp, alert=False):
        caption = format_report([plane], radius, timestamp, alert=alert)
        if plane.photo_url:
            try:
                return self.call("sendPhoto", chat_id=chat_id, photo=plane.photo_url,
                                 caption=caption, parse_mode="HTML")
            except APIError as error:
                if error.status != 400:
                    raise  # Preserve rate limits and delivery retries.
                LOG.info("Aircraft photo unavailable; sending text card")
        return self.send(chat_id, caption)

    def handle(self, update):
        membership = update.get("my_chat_member")
        if membership:
            chat = membership["chat"]
            self.store.record_chat(chat)
            member = membership.get("new_chat_member", {})
            if member.get("status") in ("left", "kicked") or (member.get("status") == "restricted" and not member.get("is_member")):
                self.store.delete(chat["id"])
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message["chat"]
        chat_id = chat["id"]
        private = chat.get("type") == "private"
        if chat.get("type") not in ("private", "group", "supergroup"):
            return
        self.store.record_chat(chat)
        text = message.get("text", "")
        first = text.split(maxsplit=1)[0] if text else ""
        command, _, target = first.partition("@")
        if target and target.lower() != self.username.lower():
            return
        location = message.get("location")
        if command == "/watch":
            location = message.get("reply_to_message", {}).get("location")
        if "edited_message" in update:
            watch = self.store.get(chat_id)
            if not watch or watch.get("location_message_id") != message.get("message_id"):
                return  # A stopped or replaced live location must not restart the watch.
        if not private and (location or command in ("/watch", "/stop")):
            user_id = message.get("from", {}).get("id")
            # Anonymous administrators cannot be verified as an individual user.
            member = self.call("getChatMember", chat_id=chat_id, user_id=user_id) if user_id and not message.get("sender_chat") else {}
            if member.get("status") not in ("creator", "administrator"):
                self.send(chat_id, "Only group administrators can set the watch location or stop alerts.")
                return
        if command == "/stop":
            self.store.delete(chat_id)
            self.send(chat_id, "Alerts stopped and the saved watch location deleted. Share a location to start again.")
            return
        if command == "/status":
            watch = self.store.get(chat_id)
            self.send(chat_id, (f"Watching within {watch['radius']:g} km. Checking every {self.interval:g} seconds. "
                                "Use /stop to stop alerts and delete the saved location.") if watch else
                      "No active watch. Share a location to start alerts.")
            return
        if not location:
            if "edited_message" in update:
                return
            if not private and command not in ("/start", "/help", "/watch"):
                return  # Don't answer ordinary group conversation or service messages.
            self.send(chat_id,
                      f"✈️ <b>Airways</b>\nShare your location to find airborne aircraft within {self.radius:g} km.\n\n"
                      "I'll save one watch location for this chat and send alerts when aircraft enter the area. "
                      f"Checks run every {self.interval:g} seconds.\n\n"
                      "Use 📎 → Location. In groups, an administrator can reply to a location with /watch@"
                      f"{self.username or 'your_bot'}. Only group admins can change or stop the watch.\n\n"
                      "Live location edits move the watch; the last position remains active after sharing ends.\n"
                      "/status — check the watch\n/stop — stop alerts and delete the saved location\n\n"
                      "Search areas are sent to OpenSky; aircraft identifiers go to adsbdb for route/type lookup.", keyboard=private)
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
        source = message.get("reply_to_message", {}) if command == "/watch" else message
        self.store.set_location(chat_id, lat, lon, self.radius, source.get("message_id"))
        watch = self.store.get(chat_id)
        watch["next_check"] = time.time() + self.interval
        try:
            self.store.metric("flight_lookups")
            aircraft, timestamp = self.provider.nearby(lat, lon, self.radius)
            if self.details:
                self.details.enrich_all(aircraft[:8])
            report = format_report(aircraft, self.radius, timestamp)
        except APIError as error:
            self.store.metric("flight_errors")
            LOG.warning("Aircraft lookup failed (status %s)", error.status)
            if error.status == 429:
                report = f"The flight data limit has been reached. Try again in {math.ceil(error.retry_after)} seconds."
            else:
                report = "Flight data is temporarily unavailable. I'll retry automatically."
            aircraft, timestamp = [], time.time()
            if error.status == 429:
                watch["next_check"] = time.time() + max(self.interval, error.retry_after)
        self.store.save(watch)
        summary = (f"{len(aircraft)} aircraft within {self.radius:g} km. Showing the nearest {min(8, len(aircraft))}."
                   if aircraft else report)
        self.send(chat_id, "<b>Nearby-flight alerts are active.</b> Use /stop to stop and delete the saved location.\n\n" + summary)
        for plane in aircraft[:8]:
            if not self.store.get(chat_id):
                break
            self.send_aircraft(chat_id, plane, self.radius, timestamp)
            watch["seen"][plane.icao] = timestamp
            self.store.save(watch)

    def check_watches(self):
        """Run independently of incoming location messages; send each arrival once."""
        if time.time() < self.telegram_blocked_until:
            return
        for watch in self.store.due(time.time()):
            chat_id = watch["chat_id"]
            watch["next_check"] = time.time() + self.interval
            try:
                self.store.metric("flight_lookups")
                aircraft, timestamp = self.provider.nearby(watch["lat"], watch["lon"], watch["radius"])
            except APIError as error:
                self.store.metric("flight_errors")
                LOG.warning("Watch lookup failed (status %s)", error.status)
                watch["next_check"] = time.time() + max(self.interval, error.retry_after)
                self.store.save(watch)
                continue  # An outage is not evidence that an aircraft left the area.
            current = {plane.icao for plane in aircraft}
            seen = watch["seen"]
            # Rearm only after an aircraft was last observed at least ten minutes ago
            # AND a successful subsequent query reports it outside the area.
            watch["seen"] = {icao: timestamp if icao in current else last
                             for icao, last in seen.items() if icao in current or timestamp - last < 600}
            arrivals = [plane for plane in aircraft if plane.icao not in seen][:8]
            if self.details and arrivals:
                self.details.enrich_all(arrivals)
            for plane in arrivals:
                try:
                    if not self.store.get(chat_id):
                        continue
                    self.send_aircraft(chat_id, plane, watch["radius"], timestamp, alert=True)
                except APIError as error:
                    self.store.metric("delivery_errors")
                    if error.status == 403:
                        self.store.delete(chat_id)  # Blocked bot or removed from group.
                        break
                    LOG.warning("Alert delivery failed (status %s)", error.status)
                    if error.status == 429:
                        self.telegram_blocked_until = time.time() + error.retry_after
                        self.store.save(watch)
                        return
                    break  # Retry this and the remaining aircraft next check.
                else:
                    self.store.alert_sent(chat_id, 1)
                    watch["seen"][plane.icao] = timestamp
                    self.store.save(watch)
            self.store.save(watch)

    def run(self):
        self.username = self.call("getMe")["username"]
        if self.call("getWebhookInfo").get("url"):
            raise SystemExit("A webhook is configured. Remove it before running this polling bot.")
        self.call("setMyCommands", commands=[{"command": "start", "description": "Find nearby aircraft"},
                                           {"command": "help", "description": "How to share your location"},
                                           {"command": "watch", "description": "Watch a location (reply to a map pin)"},
                                           {"command": "status", "description": "Check active alerts"},
                                           {"command": "stop", "description": "Stop alerts and delete saved location"}])
        LOG.info("Airways is polling for location messages")
        offset, backoff = self.store.offset(), 1
        while True:
            try:
                self.store.heartbeat()
                self.check_watches()
                updates = self.call("getUpdates", offset=offset, timeout=5,
                                    allowed_updates=["message", "edited_message", "my_chat_member"])
                for update in updates:
                    # Failed sends don't stall the whole bot or repeat expensive searches.
                    try:
                        self.handle(update)
                    except APIError as error:
                        self.store.metric("delivery_errors")
                        LOG.warning("Reply failed (status %s)", error.status)
                        if error.status == 429:
                            time.sleep(error.retry_after)
                    offset = update["update_id"] + 1
                    self.store.set_offset(offset)
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
        interval = float(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
        if not math.isfinite(interval) or interval < 15:
            raise ValueError
    except ValueError:
        raise SystemExit("POLL_INTERVAL_SECONDS must be at least 15.") from None
    try:
        store = WatchStore(os.environ.get("STATE_DB_PATH", "data/airways.sqlite3"))
        admin_server = None
        if os.environ.get("ADMIN_PASSWORD"):
            from admin import start_admin
            admin_server = start_admin(os.environ.get("STATE_DB_PATH", "data/airways.sqlite3"))
        Bot(token, OpenSky(client_id, client_secret), radius, store, FlightDetails(), interval).run()
    except KeyboardInterrupt:
        LOG.info("Airways stopped")
    except APIError as error:
        raise SystemExit(f"Telegram setup failed (status {error.status}). Check connectivity and bot credentials.") from None
    finally:
        if 'admin_server' in locals() and admin_server:
            admin_server.shutdown()


if __name__ == "__main__":
    main()

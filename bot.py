"""Airways: nearby-flight alerts with per-chat API access."""

import html
import json
import logging
import math
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from tracking import APIUsage, WatchStore
from access import CredentialVault

LOG = logging.getLogger("airways")
API_USAGE = None
EARTH_KM = 6371.0088
TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
OPENSKY_ACCOUNT_URL = "https://opensky-network.org/my-opensky/account"
KEYBOARD = {"keyboard": [[{"text": "📍 Share my location", "request_location": True}]],
            "resize_keyboard": True}


class APIError(Exception):
    def __init__(self, status=0, retry_after=5, reason=""):
        super().__init__(f"API request failed (status {status})")
        self.status = status
        self.retry_after = retry_after
        self.reason = reason


def telegram_error_reason(description, url):
    """Keep widget validation diagnostics while stripping URLs, tokens, and IDs."""
    if not isinstance(description, str):
        return ""
    route = urlsplit(url).path.strip("/").split("/")[0]
    if route.startswith("bot") and len(route) > 3:
        description = description.replace(route[3:], "[redacted]")
    description = re.sub(r"https?://\S+", "[url]", description)
    description = re.sub(r"\d{6,}", "[id]", description)
    return " ".join(description.split())[:240]


def request_json(url, *, payload=None, form=None, headers=None, timeout=20, widget_errors=False, files=None,
                 usage_scope="shared"):
    """Never include request URLs, credentials, or response bodies in errors."""
    headers = dict(headers or {})
    data = None
    if files:
        boundary = "airways-" + uuid.uuid4().hex
        parts = []
        for key, value in (payload or {}).items():
            encoded = json.dumps(value) if isinstance(value, (bool, dict, list)) else str(value)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{encoded}\r\n'.encode())
        for key, (filename, content_type, content) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode() + content + b"\r\n")
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    status, remaining, retry_after = 0, None, 0
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
            remaining = response.headers.get("X-Rate-Limit-Remaining")
            result = json.load(response)
            status = response.status
            if urlsplit(url).hostname == "api.telegram.org" and isinstance(result, dict) and result.get("ok") is False:
                status = result.get("error_code", 500)
                retry_after = result.get("parameters", {}).get("retry_after", 5)
            return result
    except HTTPError as error:
        status = error.code
        remaining = error.headers.get("X-Rate-Limit-Remaining")
        body = {}
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
        if not math.isfinite(delay):
            delay = 5
        retry_after = delay
        reason = telegram_error_reason(body.get("description", ""), url) if widget_errors and isinstance(body, dict) else ""
        raise APIError(error.code, delay, reason) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise APIError() from None
    finally:
        provider = {"opensky-network.org": "opensky", "auth.opensky-network.org": "opensky_auth",
                    "api.adsbdb.com": "adsbdb", "api.telegram.org": "telegram"}.get(urlsplit(url).hostname)
        if API_USAGE is not None and provider:
            try:
                API_USAGE.record(provider, usage_scope, status, remaining, retry_after)
            except Exception:
                LOG.warning("Could not persist API usage")


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
    def __init__(self, client_id="", client_secret="", usage_scope="owner"):
        self.client_id, self.client_secret = client_id, client_secret
        self.usage_scope = usage_scope
        self.token, self.expires_at, self.blocked_until = "", 0, 0
        self.search_cache = {}

    def headers(self):
        if not self.client_id:
            return {}
        if time.monotonic() >= self.expires_at:
            response = request_json(TOKEN_URL, form={"grant_type": "client_credentials",
                                    "client_id": self.client_id, "client_secret": self.client_secret},
                                    usage_scope=self.usage_scope)
            if not response.get("access_token"):
                raise APIError(401)
            self.token = response["access_token"]
            self.expires_at = time.monotonic() + max(0, float(response.get("expires_in", 1800)) - 30)
        return {"Authorization": f"Bearer {self.token}"}

    def nearby(self, lat, lon, radius):
        if time.monotonic() < self.blocked_until:
            raise APIError(429, self.blocked_until - time.monotonic())
        key = (lat, lon, radius)
        cached = self.search_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            # Return fresh objects: metadata enrichment mutates Aircraft instances.
            return nearby_aircraft(cached[1], lat, lon, radius, cached[2]), cached[2]
        states = []
        try:
            for box in bounding_boxes(lat, lon, radius):
                url = "https://opensky-network.org/api/states/all?" + urlencode(box)
                try:
                    data = request_json(url, headers=self.headers(), usage_scope=self.usage_scope)
                except APIError as error:
                    if error.status != 401 or not self.client_id:
                        raise
                    self.expires_at = 0
                    data = request_json(url, headers=self.headers(), usage_scope=self.usage_scope)
                states.extend(data.get("states") or [])
        except APIError as error:
            if error.status == 429:
                self.blocked_until = time.monotonic() + error.retry_after
            raise
        now = time.time()
        self.search_cache = {k: value for k, value in self.search_cache.items() if value[0] > time.monotonic()}
        if len(self.search_cache) >= 1024:
            self.search_cache.clear()
        self.search_cache[key] = (time.monotonic() + 10, states, now)
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


def airport_label(airport):
    name = str(airport.get("municipality") or airport.get("name") or "").strip()[:80]
    iata = str(airport.get("iata_code") or "").strip().upper()
    icao = str(airport.get("icao_code") or "").strip().upper()
    code = iata if re.fullmatch(r"[A-Z]{3}", iata) else icao if re.fullmatch(r"[A-Z]{4}", icao) else ""
    if code:
        return f"{name or 'Unknown'} ({code})"
    return name


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
            cities = [airport_label(port) for port in airports]
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
    if not aircraft:
        return (f"No recently reported airborne aircraft within {radius:g} km.\n"
                "Coverage can be incomplete; try again shortly.")
    lines = []
    for plane in aircraft[:8]:
        altitude = f"{plane.altitude:,.0f} m" if plane.altitude is not None else "unknown altitude"
        speed = f"{plane.speed * 3.6:.0f} km/h" if plane.speed is not None else "unknown speed"
        tracker = "https://globe.adsbexchange.com/?" + urlencode({"icao": plane.icao})
        lines.append(
            f'<b><a href="{html.escape(tracker, quote=True)}">{html.escape(plane.callsign[:32])}</a></b> '
            f'{html.escape(plane.route)}\n'
            f'{html.escape(plane.aircraft_type[:80])}\n'
            f'{altitude} · {speed}')
    if len(aircraft) > 8:
        lines.append(f"Showing the nearest 8 of {len(aircraft)} aircraft.")
    lines.append('<a href="https://opensky-network.org/">OpenSky Network</a> · '
                 '<a href="https://www.adsbdb.com/">adsbdb</a>')
    return "\n\n".join(lines)


class Bot:
    def __init__(self, token, provider, radius=50, store=None, details=None, interval=60, vault=None,
                 idle_max=None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.provider, self.radius = provider, radius
        self.cooldowns = {}
        self.store = store if store is not None else WatchStore()
        self.details = details
        self.interval = interval
        self.idle_max = max(interval, idle_max if idle_max is not None else interval)
        self.username = ""
        self.telegram_blocked_until = 0
        self.vault = vault
        self.customer_providers = {}
        self.activation_attempts = {}

    def provider_for(self, chat_id, snapshot=False):
        grant = self.store.get_access(chat_id)
        if not grant:
            self.customer_providers.pop(chat_id, None)
            return self.provider if snapshot else None
        if grant["mode"] == "promo":
            self.customer_providers.pop(chat_id, None)
            return self.provider
        if not self.vault:
            return None
        cached = self.customer_providers.get(chat_id)
        if cached and cached[0] == grant["credentials"]:
            return cached[1]
        try:
            client_id, secret = self.vault.open(grant["credentials"])
        except ValueError:
            return None
        provider = OpenSky(client_id, secret, usage_scope="customer")
        if len(self.customer_providers) >= 1024:
            self.customer_providers.clear()
        self.customer_providers[chat_id] = (grant["credentials"], provider)
        return provider

    def activation_help(self, chat_id):
        return ("<b>Free · current location checks</b>\n"
                "Send a location for a one-off check. Free checks use the owner's flight-data allowance; no watch is started.\n\n"
                "<b>Promo · checks + arrival watches</b>\n"
                "Redeem a code from the bot owner here: <code>/promo YOUR_CODE</code>\n\n"
                + self.key_setup_help(chat_id))

    def key_setup_help(self, chat_id):
        target = f"{chat_id} " if chat_id < 0 else ""
        return ("<b>Bring Your Key · checks + watches, no app usage quota</b>\n"
                f'1. <a href="{OPENSKY_ACCOUNT_URL}">Register or sign in at OpenSky</a>.\n'
                "2. In your OpenSky account, create an API client and copy its client ID and client secret.\n"
                '3. <a href="https://openskynetwork.github.io/opensky-api/rest.html#authentication">Read the API setup guide</a>, '
                "then send the credentials to me in a <b>private chat</b>:\n"
                f"<code>/credentials {target}CLIENT_ID CLIENT_SECRET</code>\n"
                "Your provider's quotas and rate limits still apply.\n"
                "Group activation requires a group administrator. Never post API secrets in a group.\n"
                "Use /access to check access or /disconnect to remove it.")

    def activation_allowed(self, user_id):
        now = time.monotonic()
        self.activation_attempts = {key: values for key, values in self.activation_attempts.items()
                                    if values and values[-1] > now - 60}
        values = [stamp for stamp in self.activation_attempts.get(user_id, []) if stamp > now - 60]
        if len(values) >= 5:
            return False
        self.activation_attempts[user_id] = values + [now]
        return True

    def handle_credentials(self, message, args):
        chat_id = message["chat"]["id"]
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message["message_id"])
        except APIError:
            pass
        if message["chat"]["type"] != "private":
            self.send(chat_id, "Send API credentials only in a private chat with me. If you posted a secret here, rotate it in OpenSky.")
            return
        if not self.vault:
            self.send(chat_id, "Credential setup is not configured by the bot owner yet. You can redeem a promo code instead.")
            return
        parts = args.split()
        target = chat_id
        if len(parts) == 3:
            try:
                target = int(parts.pop(0))
            except ValueError:
                target = 0
            if target >= 0:
                self.send(chat_id, "The optional target must be a group chat ID.\n" + self.activation_help(chat_id))
                return
            member = self.call("getChatMember", chat_id=target, user_id=message["from"]["id"])
            if member.get("status") not in ("administrator", "creator"):
                self.send(chat_id, "Only an administrator of that group can configure its credentials.")
                return
        if len(parts) != 2 or any(len(part) > 512 for part in parts):
            self.send(chat_id, self.activation_help(chat_id))
            return
        if not self.activation_allowed(message["from"]["id"]):
            self.send(chat_id, "Too many activation attempts. Please wait one minute.")
            return
        client_id, secret = parts
        provider = OpenSky(client_id, secret, usage_scope="customer")
        try:
            provider.headers()  # Validate OAuth without using the owner's data allowance.
        except APIError:
            self.send(chat_id, "Could not verify those OpenSky API client credentials. Check them and try again later.")
            return
        self.store.set_credentials(target, self.vault.seal(client_id, secret), message["from"]["id"])
        self.customer_providers.pop(target, None)
        self.send(chat_id, f"Own API credentials connected for chat <code>{target}</code>. Send a location in that chat to start alerts. "
                  "Use /disconnect to delete credentials for this chat, or /disconnect GROUP_ID for a group.")

    def disconnect(self, message, args):
        chat_id = message["chat"]["id"]
        target = chat_id
        if args:
            try:
                target = int(args)
            except ValueError:
                self.send(chat_id, "Use /disconnect or, in private, /disconnect GROUP_ID.")
                return
            if message["chat"]["type"] != "private" or target >= 0:
                self.send(chat_id, "Manage another group's access from a private chat using its group ID.")
                return
            grant = self.store.get_access(target)
            user_id = message["from"]["id"]
            if not grant or grant["configured_by"] != user_id:
                member = self.call("getChatMember", chat_id=target, user_id=user_id)
                if member.get("status") not in ("administrator", "creator"):
                    self.send(chat_id, "Only the credential owner or a group administrator can disconnect that group.")
                    return
        self.store.revoke_access(target)
        self.customer_providers.pop(target, None)
        self.send(chat_id, f"Access removed for chat <code>{target}</code>. Saved credentials and its watch were deleted.")

    def call(self, method, **payload):
        files = None
        if method == "sendAnimation" and isinstance(payload.get("animation"), bytes):
            files = {"animation": ("airways.gif", "image/gif", payload.pop("animation"))}
        options = dict(payload=payload, timeout=40, widget_errors=method == "sendWidget")
        if files:
            options["files"] = files
        data = request_json(self.base + method, **options)
        if not data.get("ok"):
            reason = telegram_error_reason(data.get("description", ""), self.base + method) if method == "sendWidget" else ""
            raise APIError(data.get("error_code", 0), data.get("parameters", {}).get("retry_after", 5), reason)
        return data["result"]

    def send(self, chat_id, text, keyboard=False):
        payload = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                       link_preview_options={"is_disabled": True})
        if keyboard:
            payload["reply_markup"] = KEYBOARD
        return self.call("sendMessage", **payload)

    def send_aircraft(self, chat_id, plane, radius, timestamp, alert=False):
        return self.send_aircraft_card(chat_id, plane, radius, timestamp, alert)

    def send_aircraft_card(self, chat_id, plane, radius, timestamp, alert=False):
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
                self.store.revoke_access(chat["id"])
                self.customer_providers.pop(chat["id"], None)
            elif membership.get("old_chat_member", {}).get("status") in ("left", "kicked"):
                self.send(chat["id"], self.activation_help(chat["id"]))
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
        parts = text.split(maxsplit=1)
        args = parts[1].strip() if len(parts) == 2 else ""
        if command == "/register":
            self.send(chat_id, self.key_setup_help(chat_id), keyboard=private)
            return
        if command == "/credentials":
            if not args:
                self.send(chat_id, self.activation_help(chat_id), keyboard=private)
                return
            self.handle_credentials(message, args)
            return
        location = message.get("location")
        if command == "/watch":
            location = message.get("reply_to_message", {}).get("location")
        if "edited_message" in update:
            watch = self.store.get(chat_id)
            if (not self.store.get_access(chat_id) or not watch
                    or watch.get("location_message_id") != message.get("message_id")):
                return  # A stopped or replaced live location must not restart the watch.
        if not private and (location or command in ("/watch", "/stop", "/promo", "/disconnect")):
            user_id = message.get("from", {}).get("id")
            # Anonymous administrators cannot be verified as an individual user.
            member = self.call("getChatMember", chat_id=chat_id, user_id=user_id) if user_id and not message.get("sender_chat") else {}
            if member.get("status") not in ("creator", "administrator"):
                self.send(chat_id, "Only group administrators can set the watch location or stop alerts.")
                return
        if command == "/disconnect":
            self.disconnect(message, args)
            return
        if command == "/promo":
            if not self.activation_allowed(message.get("from", {}).get("id", chat_id)):
                self.send(chat_id, "Too many activation attempts. Please wait one minute.")
            elif self.provider is None:
                self.send(chat_id, "The owner's API credentials are not configured. Connect your own credentials instead.")
            elif not args or not self.store.redeem(args, chat_id, message.get("from", {}).get("id", chat_id)):
                self.send(chat_id, "This promo code is invalid, expired, already redeemed here, revoked, or fully used.")
            else:
                self.customer_providers.pop(chat_id, None)
                self.send(chat_id, "Promo code accepted. This chat can use the owner's flight data. Send a location to start alerts.", keyboard=private)
            return
        if command == "/access":
            grant = self.store.get_access(chat_id)
            if not grant:
                self.send(chat_id, self.activation_help(chat_id), keyboard=private)
            elif grant["mode"] == "own":
                self.send(chat_id, "Bring Your Key: current checks and watches with no app usage quota. OpenSky quotas and rate limits still apply. Use /disconnect to delete credentials and stop alerts.")
            else:
                expiry = datetime.fromtimestamp(grant["expires_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                self.send(chat_id, f"Promo: current checks and arrival watches using the owner's flight data, active until {expiry}.")
            return
        if command == "/stop":
            self.store.delete(chat_id)
            self.send(chat_id, "Alerts stopped and the saved watch location deleted. Share a location for a new check.")
            return
        if command == "/status":
            if not self.provider_for(chat_id):
                self.send(chat_id, self.activation_help(chat_id))
                return
            watch = self.store.get(chat_id)
            self.send(chat_id, (f"Watching within {watch['radius']:g} km. Current check interval: {self.watch_interval(watch):g} seconds. "
                                "Use /stop to stop alerts and delete the saved location.") if watch else
                      "No active watch. Share a location to start alerts.")
            return
        if not location:
            if "edited_message" in update:
                return
            if not private and command not in ("/start", "/help", "/watch"):
                return  # Don't answer ordinary group conversation or service messages.
            if not self.store.get_access(chat_id):
                self.send(chat_id, self.activation_help(chat_id), keyboard=private)
                return
            self.send(chat_id,
                      f"✈️ <b>Airways</b>\nShare your location to find airborne aircraft within {self.radius:g} km.\n\n"
                      "I'll save one watch location for this chat and send alerts when aircraft enter the area. "
                      f"Checks run every {self.interval:g} seconds, slowing to at most {self.idle_max:g} seconds when empty.\n\n"
                      "Use 📎 → Location. In groups, an administrator can reply to a location with /watch@"
                      f"{self.username or 'your_bot'}. Only group admins can change or stop the watch.\n\n"
                      "Live location edits move the watch; the last position remains active after sharing ends.\n"
                      "/status — check the watch\n/stop — stop alerts and delete the saved location\n\n"
                      "Search areas are sent to OpenSky; aircraft identifiers go to adsbdb for route/type lookup.", keyboard=private)
            return
        can_watch = self.store.get_access(chat_id) is not None
        if command == "/watch" and not can_watch:
            self.send(chat_id, "Arrival watches require a promo code or your own API key.\n\n" + self.activation_help(chat_id))
            return
        provider = self.provider_for(chat_id, snapshot=True)
        if provider is None:
            self.send(chat_id, "Flight data is unavailable for this chat. The owner must configure OpenSky for Free checks, or you can connect your own key.\n\n" + self.activation_help(chat_id))
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
        if not can_watch:
            self.snapshot(chat_id, provider, lat, lon)
            return
        source = message.get("reply_to_message", {}) if command == "/watch" else message
        self.store.set_location(chat_id, lat, lon, self.radius, source.get("message_id"))
        watch = self.store.get(chat_id)
        watch["next_check"] = time.time() + self.interval
        try:
            self.store.metric("flight_lookups")
            aircraft, timestamp = provider.nearby(lat, lon, self.radius)
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
        else:
            self.schedule_watch(watch, aircraft)
        self.store.save(watch)
        summary = (f"{len(aircraft)} aircraft within {self.radius:g} km. Showing the nearest {min(8, len(aircraft))}."
                   if aircraft else report)
        self.send(chat_id, "<b>Nearby-flight alerts are active.</b> Use /stop to stop and delete the saved location.\n\n" + summary)
        for plane in aircraft[:8]:
            if time.time() < self.telegram_blocked_until:
                break
            if not self.store.get(chat_id):
                break
            self.send_aircraft(chat_id, plane, self.radius, timestamp)
            watch["seen"][plane.icao] = timestamp
            self.store.save(watch)

    def snapshot(self, chat_id, provider, lat, lon):
        """A Free check never writes a location, arrival history, or polling schedule."""
        try:
            self.store.metric("flight_lookups")
            aircraft, timestamp = provider.nearby(lat, lon, self.radius)
            if self.details:
                self.details.enrich_all(aircraft[:8])
        except APIError as error:
            self.store.metric("flight_errors")
            text = (f"Flight data is rate limited. Try again in {math.ceil(error.retry_after)} seconds."
                    if error.status == 429 else "Flight data is temporarily unavailable. Send your location again to retry.")
            self.send(chat_id, text)
            return
        summary = (f"{len(aircraft)} aircraft within {self.radius:g} km. Showing the nearest {min(8, len(aircraft))}."
                   if aircraft else format_report(aircraft, self.radius, timestamp))
        self.send(chat_id, "<b>Current location check · Free</b>\n" + summary +
                  "\n\nNo watch started. Use /access to see tiers and enable arrival alerts.")
        for plane in aircraft[:8]:
            if time.time() < self.telegram_blocked_until:
                break
            self.send_aircraft(chat_id, plane, self.radius, timestamp)

    def watch_interval(self, watch):
        return min(self.idle_max, self.interval * 2 ** min(watch.get("empty_checks", 0), 16))

    def schedule_watch(self, watch, aircraft):
        watch["empty_checks"] = 0 if aircraft else min(watch.get("empty_checks", 0) + 1, 16)
        watch["next_check"] = time.time() + self.watch_interval(watch)

    def check_watches(self):
        """Run independently of incoming location messages; send each arrival once."""
        if time.time() < self.telegram_blocked_until:
            return
        for watch in self.store.due(time.time()):
            if time.time() < self.telegram_blocked_until:
                return
            chat_id = watch["chat_id"]
            watch["next_check"] = time.time() + self.interval
            provider = self.provider_for(chat_id)
            if provider is None:
                self.store.save(watch)
                continue  # Never fall back to owner or anonymous data for a locked chat.
            try:
                self.store.metric("flight_lookups")
                aircraft, timestamp = provider.nearby(watch["lat"], watch["lon"], watch["radius"])
            except APIError as error:
                self.store.metric("flight_errors")
                LOG.warning("Watch lookup failed (status %s)", error.status)
                watch["next_check"] = time.time() + max(self.interval, error.retry_after)
                self.store.save(watch)
                continue  # An outage is not evidence that an aircraft left the area.
            self.schedule_watch(watch, aircraft)
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
                if time.time() < self.telegram_blocked_until:
                    return
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
                                           {"command": "stop", "description": "Stop alerts and delete saved location"},
                                           {"command": "promo", "description": "Redeem a promo code for this chat"},
                                           {"command": "register", "description": "Register with OpenSky and get API credentials"},
                                           {"command": "credentials", "description": "Connect own API credentials in private"},
                                           {"command": "access", "description": "Check flight-data access"},
                                           {"command": "disconnect", "description": "Remove access and saved credentials"}])
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
    vault = None
    encryption_key = os.environ.get("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if encryption_key:
        try:
            vault = CredentialVault(encryption_key)
        except (ValueError, TypeError):
            raise SystemExit("CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key (see README.md).") from None
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
        idle_max = float(os.environ.get("IDLE_POLL_MAX_SECONDS") or str(interval))
        if not math.isfinite(idle_max) or not interval <= idle_max <= max(interval, 3600):
            raise ValueError
    except ValueError:
        raise SystemExit("IDLE_POLL_MAX_SECONDS must be between POLL_INTERVAL_SECONDS and 3600.") from None
    try:
        store = WatchStore(os.environ.get("STATE_DB_PATH", "data/airways.sqlite3"))
        global API_USAGE
        API_USAGE = APIUsage(os.environ.get("STATE_DB_PATH", "data/airways.sqlite3"))
        admin_server = None
        if os.environ.get("ADMIN_PASSWORD"):
            from admin import start_admin
            admin_server = start_admin(os.environ.get("STATE_DB_PATH", "data/airways.sqlite3"))
        owner_provider = OpenSky(client_id, client_secret) if client_id else None
        Bot(token, owner_provider, radius, store, FlightDetails(), interval, vault=vault, idle_max=idle_max).run()
    except KeyboardInterrupt:
        LOG.info("Airways stopped")
    except APIError as error:
        raise SystemExit(f"Telegram setup failed (status {error.status}). Check connectivity and bot credentials.") from None
    finally:
        if 'admin_server' in locals() and admin_server:
            admin_server.shutdown()


if __name__ == "__main__":
    main()

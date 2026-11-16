"""Small authenticated admin server, served alongside the Telegram polling bot."""

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tracking import WatchStore

ASSETS = Path(__file__).parent / "admin_static"
LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · Airways</title>
<link rel="stylesheet" href="/admin.css"></head><body class="login-body"><main class="login-card">
<div class="brand-mark">↗</div><p class="eyebrow">AIRWAYS / CONTROL ROOM</p><h1>Your sky.<br>Your overview.</h1>
<p class="muted">Sign in to monitor your flight bot and its chats.</p>
<form action="/login" method="post"><label for="password">Admin password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button class="primary" type="submit">Enter control room <span>↗</span></button></form>
<p class="login-note">Private access · Airways administration</p></main></body></html>"""


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path, password, secure_cookie=True):
        if not password:
            raise ValueError("An admin password is required")
        self.db_path = db_path
        self.password_digest = hashlib.sha256(password.encode()).digest()
        self.secure_cookie = secure_cookie
        self.sessions = {}
        self.failures = deque()
        self.lock = threading.Lock()
        super().__init__(address, AdminHandler)


class AdminHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Avoid logging credentials, cookies, query strings, and chat identifiers.

    def respond(self, status, body=b"", content_type="text/plain; charset=utf-8", cookie=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if status == 303:
            self.send_header("Location", "/")
        self.end_headers()
        self.wfile.write(body)

    def token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        token = cookies.get("airways_admin")
        token = token.value if token else ""
        with self.server.lock:
            expiry = self.server.sessions.get(token, 0)
            if expiry <= time.time():
                self.server.sessions.pop(token, None)
                return ""
        return token

    def cookie(self, token, max_age=28800):
        suffix = "; Secure" if self.server.secure_cookie else ""
        return f"airways_admin={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{suffix}"

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if not 0 <= length <= 2048:
            return None
        return self.rfile.read(length)

    def do_GET(self):
        path = urlsplit(self.path).path
        # Static assets contain no private data.
        files = {"/admin.css": ("admin.css", "text/css"), "/admin.js": ("admin.js", "text/javascript")}
        if path in files:
            name, mime = files[path]
            self.respond(200, (ASSETS / name).read_bytes(), mime + "; charset=utf-8")
            return
        token = self.token()
        if path == "/":
            self.respond(200, (ASSETS / "index.html").read_bytes() if token else LOGIN, "text/html; charset=utf-8")
        elif path == "/api/overview":
            if not token:
                self.respond(401, "Sign in required")
                return
            store = WatchStore(self.server.db_path)
            try:
                data = store.dashboard()
            finally:
                store.db.close()
            data["csrf"] = token
            self.respond(200, json.dumps(data), "application/json; charset=utf-8")
        else:
            self.respond(404, "Not found")

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self.read_body()
        if body is None:
            self.respond(400, "Invalid request size")
            return
        if path == "/login":
            now = time.time()
            with self.server.lock:
                while self.server.failures and self.server.failures[0] <= now - 300:
                    self.server.failures.popleft()
                blocked = len(self.server.failures) >= 10
            if blocked:
                self.respond(429, "Too many sign-in attempts. Try again in five minutes.")
                return
            password = parse_qs(body.decode(errors="replace")).get("password", [""])[0]
            if not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self.server.password_digest):
                with self.server.lock:
                    self.server.failures.append(now)
                self.respond(401, "Incorrect password. Go back to try again.")
                return
            token = secrets.token_urlsafe(32)
            with self.server.lock:
                self.server.sessions = {k: expiry for k, expiry in self.server.sessions.items() if expiry > now}
                self.server.sessions[token] = now + 28800
            self.respond(303, cookie=self.cookie(token))
            return
        token = self.token()
        if not token:
            self.respond(401, "Sign in required")
            return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), token.encode()):
            self.respond(403, "Invalid request token")
            return
        if path == "/logout":
            with self.server.lock:
                self.server.sessions.pop(token, None)
            self.respond(200, "Signed out", cookie=self.cookie("", 0))
        elif path == "/api/stop":
            try:
                chat_id = int(json.loads(body)["chat_id"])
            except (ValueError, TypeError, KeyError):
                self.respond(400, "Invalid chat")
                return
            store = WatchStore(self.server.db_path)
            try:
                store.delete(chat_id)
            finally:
                store.db.close()
            self.respond(200, '{"ok":true}', "application/json")
        else:
            self.respond(404, "Not found")


def start_admin(db_path):
    server = AdminServer((os.environ.get("ADMIN_HOST", "127.0.0.1"), int(os.environ.get("ADMIN_PORT", "8080"))),
                         db_path, os.environ["ADMIN_PASSWORD"], os.environ.get("ADMIN_SECURE_COOKIE", "1") != "0")
    threading.Thread(target=server.serve_forever, daemon=True, name="airways-admin").start()
    logging.getLogger("airways").info("Admin page listening on port %s", server.server_port)
    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    path = os.environ.get("STATE_DB_PATH", "data/airways.sqlite3")
    WatchStore(path).db.close()
    server = start_admin(path)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()

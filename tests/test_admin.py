import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from admin import AdminServer
from tracking import WatchStore


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.path = str(Path(cls.directory.name) / "test.sqlite3")
        store = WatchStore(cls.path)
        store.record_chat({"id": -123, "title": "<script>hello</script>", "type": "supergroup"})
        store.set_location(-123, 38.7, -9.1, 50)
        store.metric("flight_lookups", 4)
        store.alert_sent(-123, 2)
        store.heartbeat()
        store.db.close()
        cls.server = AdminServer(("127.0.0.1", 0), cls.path, "test-password", secure_cookie=False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.directory.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def login(self):
        status, headers, _ = self.request("POST", "/login", "password=test-password")
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        return {"Cookie": cookie}, cookie.split("=", 1)[1]

    def test_anonymous_cannot_read_chat_data_or_stop_watches(self):
        self.assertEqual(self.request("GET", "/api/overview")[0], 401)
        self.assertEqual(self.request("POST", "/api/stop", '{"chat_id":-123}')[0], 401)
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Admin password", body)
        self.assertNotIn(b"hello", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_authenticated_overview_and_metrics(self):
        headers, _ = self.login()
        status, _, body = self.request("GET", "/api/overview", headers=headers)
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["chats"][0]["title"], "<script>hello</script>")
        self.assertEqual(data["chats"][0]["alerts"], 1)
        self.assertEqual(next(row["value"] for row in data["metrics"] if row["name"] == "flight_lookups"), 4)
        self.assertIn("csrf", data)

    def test_csrf_required_for_actions(self):
        headers, _ = self.login()
        self.assertEqual(self.request("POST", "/api/stop", '{"chat_id":-123}', headers)[0], 403)

    def test_stop_deletes_watch_and_stale_bot_save_cannot_restore_it(self):
        store = WatchStore(self.path)
        self.addCleanup(store.db.close)
        store.set_location(-456, 40, 10, 50)
        stale_watch = store.get(-456)
        headers, token = self.login()
        headers["X-CSRF-Token"] = token
        self.assertEqual(self.request("POST", "/api/stop", '{"chat_id":-456}', headers)[0], 200)
        store.save(stale_watch)
        self.assertIsNone(store.get(-456))

    def test_logout_invalidates_session(self):
        headers, token = self.login()
        headers["X-CSRF-Token"] = token
        self.assertEqual(self.request("POST", "/logout", "{}", headers)[0], 200)
        self.assertEqual(self.request("GET", "/api/overview", headers=headers)[0], 401)

    def test_expired_session_is_rejected(self):
        headers, token = self.login()
        self.server.sessions[token] = 0
        self.assertEqual(self.request("GET", "/api/overview", headers=headers)[0], 401)

    def test_invalid_password_is_rejected(self):
        self.assertEqual(self.request("POST", "/login", "password=wrong")[0], 401)

    def test_static_files_and_unknown_routes(self):
        self.assertEqual(self.request("GET", "/admin.css")[0], 200)
        self.assertEqual(self.request("GET", "/admin.js")[0], 200)
        self.assertEqual(self.request("GET", "/../bot.py")[0], 404)

    def test_oversized_request_is_rejected(self):
        self.assertEqual(self.request("POST", "/login", "x" * 2049)[0], 400)

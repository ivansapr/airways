import json
import io
import unittest
import zlib
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from email.message import Message

from bot import APIError, Aircraft, Bot, request_json, telegram_error_reason
from flight_widget import widget_html, widget_data, widget_caption


def aircraft():
    return Aircraft("abc123", "TAP123", "Portugal", 38.73, -9.14, 4.2, 9100, 200, 270, 5,
                    "Boeing 737-800", "Lisbon → London")


class WidgetTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot("token", Mock())
        self.addCleanup(self.bot.store.db.close)
        self.bot.call = Mock(return_value={"message_id": 1})
        self.plane = aircraft()

    def test_widget_fits_telegram_compressed_and_uncompressed_limits(self):
        source = widget_html().encode()
        self.assertLessEqual(len(source), 32768)
        self.assertLessEqual(len(zlib.compress(source, 9)), 4096)
        self.assertNotIn(b"\x00", source)
        self.assertIn(b"prefers-reduced-motion", source)
        for forbidden in (b"fetch(", b"XMLHttpRequest", b"rotateX", b"perspective", b"@import", b"https://"):
            self.assertNotIn(forbidden, source)

    def test_state_contains_only_display_data_and_fits_utf8_limit(self):
        state = widget_data(self.plane, 1800000000)
        self.assertLessEqual(len(state.encode()), 1024)
        data = json.loads(state)
        self.assertEqual(data["a"], "Boeing 737-800")
        self.assertEqual(data["r"], "Lisbon → London")
        self.assertEqual(data["v"], "720")
        self.assertNotIn("abc123", state)
        self.assertNotIn("Portugal", state)
        self.assertNotIn("38.73", state)

    def test_long_unicode_names_and_missing_metrics(self):
        self.plane.route = "東京 → " * 200
        self.plane.aircraft_type = "飛行機" * 100
        self.plane.callsign = "✈" * 100
        self.plane.altitude = self.plane.speed = None
        state = widget_data(self.plane, 1800000000)
        self.assertLessEqual(len(state.encode()), 1024)
        self.assertEqual(json.loads(state)["h"], "—")
        self.assertLessEqual(len(widget_caption(self.plane).encode()), 900)

    def test_private_chat_gets_captioned_widget_after_card_without_new_provider_calls(self):
        self.bot.send_aircraft(123, self.plane, 50, 1800000000)
        calls = self.bot.call.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["sendMessage", "sendWidget"])
        widget = calls[-1].kwargs
        self.assertEqual(json.loads(widget["chat_data"])["c"], "TAP123")
        self.assertIn("Lisbon → London", widget["caption"])
        self.assertFalse(widget["allow_queries"])
        self.assertNotIn("public_data", widget)
        self.bot.provider.nearby.assert_not_called()

    def test_photo_is_preserved_beside_offline_widget(self):
        self.plane.photo_url = "https://photos.example.com/aircraft.jpg"
        self.bot.send_aircraft(123, self.plane, 50, 1800000000)
        self.assertEqual([call.args[0] for call in self.bot.call.call_args_list], ["sendPhoto", "sendWidget"])

    def test_groups_never_call_production_widget_method(self):
        self.bot.send_aircraft(-123, self.plane, 50, 1800000000)
        self.assertEqual([call.args[0] for call in self.bot.call.call_args_list], ["sendMessage"])

    def test_disabled_widget_retains_standard_card(self):
        self.bot.widgets_enabled = False
        self.bot.send_aircraft(123, self.plane, 50, 1800000000)
        self.assertEqual(self.bot.call.call_count, 1)

    def test_unsupported_widget_does_not_lose_or_repeat_delivered_card(self):
        self.bot.call.side_effect = [{"message_id": 1}, APIError(400), {"message_id": 2}]
        self.bot.send_aircraft(123, self.plane, 50, 1800000000)
        self.bot.send_aircraft(123, self.plane, 50, 1800000060)
        self.assertEqual([call.args[0] for call in self.bot.call.call_args_list],
                         ["sendMessage", "sendWidget", "sendMessage"])

    def test_widget_rate_limit_keeps_card_delivered_and_pauses_subsequent_alerts(self):
        self.bot.call.side_effect = [{"message_id": 1}, APIError(429, 120)]
        with patch("bot.time.time", return_value=100):
            result = self.bot.send_aircraft(123, self.plane, 50, 1800000000)
        self.assertEqual(result["message_id"], 1)
        self.assertEqual(self.bot.telegram_blocked_until, 220)

    def test_failed_photo_prevents_widget_and_preserves_normal_retry_behavior(self):
        self.plane.photo_url = "https://photos.example.com/aircraft.jpg"
        self.bot.call.side_effect = APIError(503)
        with self.assertRaises(APIError):
            self.bot.send_aircraft(123, self.plane, 50, 1800000000)
        self.assertEqual(self.bot.call.call_count, 1)

    def test_widget_error_reason_redacts_tokens_urls_and_chat_ids(self):
        url = "https://api.telegram.org/bot123456:SECRET/sendWidget"
        reason = telegram_error_reason("Bad Request: WIDGET_INVALID 123456:SECRET chat 987654321 https://example.com/key", url)
        self.assertIn("WIDGET_INVALID", reason)
        for secret in ("SECRET", "123456", "987654321", "https://example.com"):
            self.assertNotIn(secret, reason)

    @patch("bot.urlopen")
    def test_http_widget_rejection_keeps_validation_reason(self, open_url):
        url = "https://api.telegram.org/bot123456:SECRET/sendWidget"
        def error():
            return HTTPError(url, 400, "Bad Request", Message(),
                             io.BytesIO(b'{"description":"Bad Request: INPUT_MEDIA_WIDGET_HTML_INVALID"}'))
        open_url.side_effect = error()
        with self.assertRaises(APIError) as caught:
            request_json(url, payload={}, widget_errors=True)
        self.assertIn("INPUT_MEDIA_WIDGET_HTML_INVALID", caught.exception.reason)
        open_url.side_effect = error()
        with self.assertRaises(APIError) as caught:
            request_json(url, payload={})
        self.assertEqual(caught.exception.reason, "")

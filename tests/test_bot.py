import math
import unittest
from unittest.mock import Mock, patch

from bot import APIError, Bot, OpenSky, bounding_boxes, distance_km, format_report, nearby_aircraft


NOW = 1_800_000_000


def state(icao="abc123", lat=38.73, lon=-9.14, **fields):
    row = [icao, "TAP123  ", "Portugal", NOW - 5, NOW, lon, lat,
           9000, False, 200, 270, 0, None, 9100, None, False, 0]
    indices = dict(position_time=3, altitude=7, ground=8, speed=9, heading=10, geo_altitude=13)
    for key, value in fields.items():
        row[indices[key]] = value
    return row


class GeographyTests(unittest.TestCase):
    def test_known_distance(self):
        self.assertAlmostEqual(distance_km(0, 0, 0, 1), 111.195, places=2)
        self.assertEqual(distance_km(90, 0, 90, 0), 0)

    def test_date_line_and_poles(self):
        for lon in (-179.9, 179.9):
            boxes = bounding_boxes(0, lon, 50)
            self.assertEqual(len(boxes), 2)
            for box in boxes:
                self.assertLessEqual(-180, box["lomin"])
                self.assertLess(box["lomin"], box["lomax"])
                self.assertLessEqual(box["lomax"], 180)
        for lat in (-90, 90):
            box, = bounding_boxes(lat, 0, 50)
            self.assertEqual((box["lomin"], box["lomax"]), (-180, 180))

    def test_bounds_include_circle_at_various_latitudes(self):
        for lat in (-89.9, -60, 0, 60, 89.9):
            for lon in (-179.9, 0, 179.9):
                boxes = bounding_boxes(lat, lon, 250)
                # Independent spherical destination calculation at points on the circle.
                a, b, d = math.radians(lat), math.radians(lon), 249.99 / 6371.0088
                for bearing in range(0, 360, 5):
                    t = math.radians(bearing)
                    y = math.asin(math.sin(a) * math.cos(d) + math.cos(a) * math.sin(d) * math.cos(t))
                    x = b + math.atan2(math.sin(t) * math.sin(d) * math.cos(a), math.cos(d) - math.sin(a) * math.sin(y))
                    y, x = math.degrees(y), (math.degrees(x) + 180) % 360 - 180
                    self.assertTrue(any(box["lamin"] <= y <= box["lamax"] and
                                        box["lomin"] <= x <= box["lomax"] for box in boxes))


class AircraftTests(unittest.TestCase):
    def test_filters_sorts_and_deduplicates(self):
        near = state()
        farther = state("abc124", lat=38.85)
        rows = [farther, near, near, state("abc125", ground=True),
                state("abc126", position_time=NOW - 121), state("abc127", lat=None),
                state("abc128", lat=40), state("abc129", position_time=None),
                state("abc130", position_time=NOW + 100), [], state("bad")]
        result = nearby_aircraft(rows, 38.73, -9.14, 50, NOW)
        self.assertEqual([plane.icao for plane in result], ["abc123", "abc124"])
        self.assertEqual(result[0].altitude, 9100)

    def test_date_line_aircraft(self):
        result = nearby_aircraft([state(lat=0, lon=-179.9)], 0, 179.9, 50, NOW)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].distance, 22.239, places=2)

    def test_report_handles_missing_values_and_escapes_html(self):
        row = state(altitude=None, geo_altitude=None, speed=None, heading=None)
        row[1] = "<TAP&123>"
        result = nearby_aircraft([row], 38.73, -9.14, 50, NOW)
        report = format_report(result, 50, NOW)
        self.assertIn("&lt;TAP&amp;123&gt;", report)
        self.assertIn("Altitude unknown", report)
        self.assertIn("Position 5s ago", report)
        self.assertIn("coverage may be incomplete", report)

    def test_report_limits_results_and_fits_telegram(self):
        rows = [state(f"{i:06x}") for i in range(20)]
        report = format_report(nearby_aircraft(rows, 38.73, -9.14, 50, NOW), 50, NOW)
        self.assertIn("nearest 8 of 20", report)
        self.assertLess(len(report), 4096)


class ProviderTests(unittest.TestCase):
    @patch("bot.request_json")
    def test_oauth_refresh_on_unauthorized(self, request):
        request.side_effect = [{"access_token": "first", "expires_in": 1800},
                               APIError(401), {"access_token": "second", "expires_in": 1800},
                               {"states": None}]
        provider = OpenSky("id", "secret")
        aircraft, _ = provider.nearby(38, -9, 50)
        self.assertEqual(aircraft, [])
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer second"})

    @patch("bot.request_json")
    def test_date_line_queries_and_deduplicates(self, request):
        request.return_value = {"states": [state(lat=0, lon=-179.9)]}
        with patch("bot.time.time", return_value=NOW):
            aircraft, _ = OpenSky().nearby(0, 179.9, 50)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(aircraft), 1)

    @patch("bot.request_json", side_effect=APIError(429, 60))
    def test_rate_limit_prevents_more_provider_calls(self, request):
        provider = OpenSky()
        for _ in range(2):
            with self.assertRaises(APIError) as error:
                provider.nearby(38, -9, 50)
            self.assertEqual(error.exception.status, 429)
        self.assertEqual(request.call_count, 1)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock()
        self.provider.nearby.return_value = (nearby_aircraft([state()], 38.73, -9.14, 50, NOW), NOW)
        self.bot = Bot("test-token", self.provider)
        self.bot.call = Mock()
        self.message = {"chat": {"id": 123, "type": "private"},
                        "location": {"latitude": 38.73, "longitude": -9.14}}

    def test_location_to_reply_flow(self):
        self.bot.handle({"message": self.message})
        self.provider.nearby.assert_called_once_with(38.73, -9.14, 50)
        args = self.bot.call.call_args
        self.assertEqual(args.args, ("sendMessage",))
        self.assertEqual(args.kwargs["chat_id"], 123)
        self.assertIn("TAP123", args.kwargs["text"])

    def test_start_shows_location_button(self):
        self.bot.handle({"message": {"chat": self.message["chat"], "text": "/start"}})
        keyboard = self.bot.call.call_args.kwargs["reply_markup"]["keyboard"]
        self.assertTrue(keyboard[0][0]["request_location"])
        self.provider.nearby.assert_not_called()

    def test_live_location_cooldown(self):
        with patch("bot.time.monotonic", side_effect=[100, 105, 116]):
            self.bot.handle({"message": self.message})
            self.bot.handle({"edited_message": self.message})
            self.bot.handle({"edited_message": self.message})
        self.assertEqual(self.provider.nearby.call_count, 2)
        self.assertEqual(self.bot.call.call_count, 2)

    def test_provider_failure_is_not_reported_as_empty_sky(self):
        self.provider.nearby.side_effect = APIError(503)
        self.bot.handle({"message": self.message})
        self.assertIn("temporarily unavailable", self.bot.call.call_args.kwargs["text"])

    def test_group_does_not_query_provider(self):
        self.message["chat"]["type"] = "group"
        self.bot.handle({"message": self.message})
        self.provider.nearby.assert_not_called()

    def test_invalid_location_does_not_query_provider(self):
        self.message["location"]["latitude"] = float("nan")
        self.bot.handle({"message": self.message})
        self.provider.nearby.assert_not_called()


if __name__ == "__main__":
    unittest.main()

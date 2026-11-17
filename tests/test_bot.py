import math
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from bot import APIError, Bot, FlightDetails, OpenSky, aircraft_name, airport_label, bounding_boxes, distance_km, format_report, nearby_aircraft
from tracking import WatchStore


NOW = 1_800_000_000


def authorize(store, *chat_ids):
    promo = store.create_promo("Test", max_uses=len(chat_ids), grant_days=365)
    for chat_id in chat_ids:
        assert store.redeem(promo["code"], chat_id, 456)


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
        self.assertIn("unknown altitude · unknown speed", report)
        for removed in ("Heading", "Position", "ICAO", "Registered country"):
            self.assertNotIn(removed, report)
        self.assertIn('<a href="https://opensky-network.org/">OpenSky Network</a>', report)
        self.assertIn('<a href="https://www.adsbdb.com/">adsbdb</a>', report)
        self.assertNotIn("Checked", report)
        self.assertNotIn("UTC", report)
        self.assertNotIn("may be outdated", report)
        for removed in ("Passing nearby", "km away", "Map</a>", "Route:", "Aircraft:", "Altitude", "Speed"):
            self.assertNotIn(removed, report)
        self.assertIn('<b><a href="https://globe.adsbexchange.com/?icao=abc123">', report)

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
        authorize(self.bot.store, 123, -123)
        self.addCleanup(self.bot.store.db.close)
        self.bot.call = Mock()
        self.message = {"chat": {"id": 123, "type": "private"}, "message_id": 42, "from": {"id": 456},
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
        self.assertEqual(self.bot.call.call_count, 4)  # Watch confirmation and aircraft card per location.

    def test_aircraft_photo_has_readable_caption(self):
        plane, = self.provider.nearby.return_value[0]
        plane.aircraft_type = "Boeing 737-800"
        plane.route = "Lisbon → London"
        plane.photo_url = "https://photos.example.com/aircraft.jpg"
        self.bot.send_aircraft(123, plane, 50, NOW, alert=True)
        args = self.bot.call.call_args
        self.assertEqual(args.args, ("sendPhoto",))
        self.assertEqual(args.kwargs["photo"], plane.photo_url)
        self.assertIn("Boeing 737-800", args.kwargs["caption"])
        self.assertIn("Lisbon → London", args.kwargs["caption"])
        self.assertLess(len(args.kwargs["caption"]), 1024)

    def test_rejected_photo_falls_back_to_text(self):
        plane, = self.provider.nearby.return_value[0]
        plane.photo_url = "https://photos.example.com/missing.jpg"
        self.bot.call.side_effect = [APIError(400), {}]
        self.bot.send_aircraft(123, plane, 50, NOW)
        self.assertEqual([call.args[0] for call in self.bot.call.call_args_list], ["sendPhoto", "sendMessage"])

    def test_photo_rate_limit_is_not_bypassed_by_text(self):
        plane, = self.provider.nearby.return_value[0]
        plane.photo_url = "https://photos.example.com/aircraft.jpg"
        self.bot.call.side_effect = APIError(429, 60)
        with self.assertRaises(APIError):
            self.bot.send_aircraft(123, plane, 50, NOW)
        self.assertEqual(self.bot.call.call_count, 1)

    def test_provider_failure_is_not_reported_as_empty_sky(self):
        self.provider.nearby.side_effect = APIError(503)
        self.bot.handle({"message": self.message})
        self.assertIn("temporarily unavailable", self.bot.call.call_args.kwargs["text"])

    def test_group_member_cannot_change_watch(self):
        self.message["chat"]["type"] = "group"
        self.bot.call.return_value = {"status": "member"}
        self.bot.handle({"message": self.message})
        self.provider.nearby.assert_not_called()

    def test_group_admin_starts_watch_by_reply(self):
        self.bot.username = "airways_bot"
        self.bot.call.return_value = {"status": "administrator"}
        self.bot.handle({"message": {"chat": {"id": -123, "type": "supergroup"},
                                    "from": {"id": 456}, "text": "/watch@airways_bot",
                                    "reply_to_message": self.message}})
        self.assertEqual(self.bot.store.get(-123)["lat"], 38.73)
        self.assertNotIn("reply_markup", self.bot.call.call_args.kwargs)
        self.provider.nearby.assert_called_once()

    def test_ordinary_group_conversation_is_ignored(self):
        self.bot.handle({"message": {"chat": {"id": -123, "type": "group"}, "text": "Hello!"}})
        self.bot.call.assert_not_called()

    def test_command_for_another_bot_is_ignored(self):
        self.bot.username = "airways_bot"
        self.bot.handle({"message": {"chat": self.message["chat"], "text": "/stop@another_bot"}})
        self.bot.call.assert_not_called()

    def test_stop_deletes_watch_and_live_edits_cannot_restart_it(self):
        self.bot.handle({"message": self.message})
        self.bot.handle({"message": {"chat": self.message["chat"], "text": "/stop"}})
        self.bot.handle({"edited_message": self.message})
        self.assertIsNone(self.bot.store.get(123))
        self.assertEqual(self.provider.nearby.call_count, 1)

    def test_old_live_pin_cannot_move_replaced_watch(self):
        self.bot.store.set_location(123, 40, 10, 50, location_message_id=99)
        self.bot.handle({"edited_message": self.message})
        self.assertEqual(self.bot.store.get(123)["lat"], 40)

    def test_removing_bot_from_group_stops_watch(self):
        self.bot.store.set_location(-123, 40, 10, 50)
        self.bot.handle({"my_chat_member": {"chat": {"id": -123, "type": "supergroup"},
                                           "new_chat_member": {"status": "kicked"}}})
        self.assertIsNone(self.bot.store.get(-123))

    def test_invalid_location_does_not_query_provider(self):
        self.message["location"]["latitude"] = float("nan")
        self.bot.handle({"message": self.message})
        self.provider.nearby.assert_not_called()


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.provider = Mock()
        self.store = WatchStore()
        authorize(self.store, 123)
        self.addCleanup(self.store.db.close)
        self.bot = Bot("token", self.provider, store=self.store)
        self.bot.send = Mock()
        self.store.set_location(123, 38.73, -9.14, 50)

    def tick(self, rows, now):
        self.provider.nearby.return_value = (nearby_aircraft(rows, 38.73, -9.14, 50, NOW), now)
        with patch("bot.time.time", return_value=now):
            self.bot.check_watches()

    def test_alerts_without_incoming_messages_and_deduplicates(self):
        self.tick([state()], NOW)
        self.tick([state()], NOW + 60)
        self.assertEqual(self.bot.send.call_count, 1)
        self.assertIn("globe.adsbexchange.com", self.bot.send.call_args.args[1])
        self.tick([state(), state("abc124")], NOW + 120)
        self.assertEqual(self.bot.send.call_count, 2)
        self.assertIn("abc124", self.bot.send.call_args.args[1])

    def test_rearms_only_after_successfully_observed_absence(self):
        self.tick([state()], NOW)
        self.tick([], NOW + 60)
        self.tick([state()], NOW + 120)
        self.assertEqual(self.bot.send.call_count, 1)
        self.tick([], NOW + 721)
        self.tick([state()], NOW + 781)
        self.assertEqual(self.bot.send.call_count, 2)

    def test_provider_outage_does_not_reset_arrival_history(self):
        self.tick([state()], NOW)
        self.provider.nearby.side_effect = APIError(503)
        with patch("bot.time.time", return_value=NOW + 700):
            self.bot.check_watches()
        self.provider.nearby.side_effect = None
        self.tick([state()], NOW + 760)
        self.assertEqual(self.bot.send.call_count, 1)

    def test_failed_send_is_retried_on_next_check(self):
        self.bot.send.side_effect = APIError(500)
        self.tick([state()], NOW)
        self.assertEqual(self.store.get(123)["seen"], {})
        self.bot.send.side_effect = None
        self.tick([state()], NOW + 60)
        self.assertEqual(self.bot.send.call_count, 2)
        self.assertIn("abc123", self.store.get(123)["seen"])

    def test_partial_delivery_does_not_repeat_successful_aircraft(self):
        self.bot.send.side_effect = [{}, APIError(500)]
        self.tick([state(), state("abc124")], NOW)
        self.assertEqual(set(self.store.get(123)["seen"]), {"abc123"})
        self.bot.send.side_effect = None
        self.tick([state(), state("abc124")], NOW + 60)
        self.assertEqual(self.bot.send.call_count, 3)
        self.assertEqual(set(self.store.get(123)["seen"]), {"abc123", "abc124"})

    def test_telegram_rate_limit_pauses_alerts_globally(self):
        self.bot.send.side_effect = APIError(429, 120)
        self.tick([state()], NOW)
        self.tick([state()], NOW + 60)
        self.assertEqual(self.bot.send.call_count, 1)
        self.bot.send.side_effect = None
        self.tick([state()], NOW + 120)
        self.assertEqual(self.bot.send.call_count, 2)

    def test_removed_bot_watch_is_deleted(self):
        self.bot.send.side_effect = APIError(403)
        self.tick([state()], NOW)
        self.assertIsNone(self.store.get(123))

    def test_watches_and_duplicate_history_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            store = WatchStore(path)
            authorize(store, 123)
            store.set_location(123, 38.73, -9.14, 50, 42)
            watch = store.get(123)
            watch["seen"] = {"abc123": NOW}
            store.save(watch)
            store.set_offset(12345)
            store.db.close()
            restored = WatchStore(path)
            self.addCleanup(restored.db.close)
            self.assertEqual(restored.offset(), 12345)
            bot = Bot("token", self.provider, store=restored)
            bot.send = Mock()
            self.provider.nearby.return_value = (nearby_aircraft([state()], 38.73, -9.14, 50, NOW), NOW + 60)
            with patch("bot.time.time", return_value=NOW + 60):
                bot.check_watches()
            bot.send.assert_not_called()


class FlightDetailsTests(unittest.TestCase):
    @patch("bot.request_json")
    def test_type_and_route_with_midpoint_are_enriched_and_cached(self, request):
        request.return_value = {"response": {"aircraft": {"type": "737-800", "manufacturer": "BOEING",
                                                         "url_photo": "https://photos.example.com/aircraft.jpg"},
                                "flightroute": {"origin": {"municipality": "Lisbon", "iata_code": "LIS"},
                                                "midpoint": {"municipality": "Madrid", "iata_code": "MAD"},
                                                "destination": {"municipality": "London", "icao_code": "EGLL"}}}}
        details = FlightDetails()
        for _ in range(2):
            plane, = nearby_aircraft([state()], 38.73, -9.14, 50, NOW)
            details.enrich(plane)
            self.assertEqual(plane.aircraft_type, "Boeing 737-800")
            self.assertEqual(plane.route, "Lisbon (LIS) → Madrid (MAD) → London (EGLL)")
            self.assertEqual(plane.photo_url, "https://photos.example.com/aircraft.jpg")
        self.assertEqual(request.call_count, 1)

    def test_airport_codes_prefer_iata_and_handle_missing_or_invalid_values(self):
        self.assertEqual(airport_label({"municipality": "Valencia", "iata_code": " vlc ", "icao_code": "LEVC"}), "Valencia (VLC)")
        self.assertEqual(airport_label({"municipality": "Venice", "iata_code": "VCE"}), "Venice (VCE)")
        self.assertEqual(airport_label({"municipality": "Lisbon"}), "Lisbon")
        self.assertEqual(airport_label({"municipality": "London", "iata_code": "N/A", "icao_code": "EGLL"}), "London (EGLL)")
        self.assertEqual(airport_label({}), "")

    def test_aircraft_name_avoids_duplicate_manufacturer_and_expands_code(self):
        self.assertEqual(aircraft_name({"manufacturer": "AIRBUS", "type": "Airbus A320"}), "Airbus A320")
        self.assertEqual(aircraft_name({"icao_type": "B738"}), "Boeing 737-800")
        self.assertEqual(aircraft_name({}), "Unknown")

    @patch("bot.request_json")
    def test_airport_name_fallback_and_missing_city(self, request):
        request.return_value = {"response": {"aircraft": {"url_photo": None},
                                "flightroute": {"origin": {"municipality": "", "name": "London Heathrow Airport"},
                                                "destination": {"iata_code": "LIS"}}}}
        plane, = nearby_aircraft([state()], 38.73, -9.14, 50, NOW)
        FlightDetails().enrich(plane)
        self.assertEqual(plane.route, "London Heathrow Airport → Unknown (LIS)")
        self.assertEqual(plane.photo_url, "")

    @patch("bot.request_json", side_effect=APIError(404))
    def test_missing_metadata_preserves_live_data(self, request):
        plane, = nearby_aircraft([state()], 38.73, -9.14, 50, NOW)
        FlightDetails().enrich_all([plane])
        self.assertEqual(plane.route, "Unknown")
        report = format_report([plane], 50, NOW)
        self.assertIn("9,100 m · 720 km/h", report)
        self.assertNotIn("Speed", report)
        self.assertIn("\nUnknown\n", report)


if __name__ == "__main__":
    unittest.main()

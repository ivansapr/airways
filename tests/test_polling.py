import unittest
from unittest.mock import Mock, patch

from bot import APIError, Bot, OpenSky
from tracking import WatchStore


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.store = WatchStore()
        self.addCleanup(self.store.db.close)
        promo = self.store.create_promo('Watch', grant_days=365)
        self.store.redeem(promo['code'], 123, 123)
        self.store.set_location(123, 38.7, -9.1, 50)
        self.provider = Mock()
        self.bot = Bot('token', self.provider, store=self.store, idle_max=300)
        self.bot.send_aircraft = Mock()

    def tick(self, now, aircraft):
        self.provider.nearby.return_value = (aircraft, now)
        with patch('bot.time.time', return_value=now):
            self.bot.check_watches()

    def test_empty_backoff_is_capped_and_busy_sky_restores_base(self):
        now = 1800000000
        for interval in (120, 240, 300, 300):
            self.tick(now, [])
            self.assertEqual(self.store.get(123)['next_check'], now + interval)
            now += interval
        plane = Mock(icao='abc123')
        self.tick(now, [plane])
        self.assertEqual(self.store.get(123)['empty_checks'], 0)
        self.assertEqual(self.store.get(123)['next_check'], now + 60)

    def test_errors_do_not_count_as_empty_and_respect_provider_wait(self):
        self.tick(1800000000, [])
        self.provider.nearby.side_effect = APIError(429, 600)
        with patch('bot.time.time', return_value=1800000120):
            self.bot.check_watches()
        watch = self.store.get(123)
        self.assertEqual(watch['empty_checks'], 1)
        self.assertEqual(watch['next_check'], 1800000720)

    def test_fixed_polling_is_default_and_moving_resets_backoff(self):
        self.bot.idle_max = 60
        self.tick(1800000000, [])
        self.assertEqual(self.store.get(123)['next_check'], 1800000060)
        self.store.set_location(123, 39, -9, 50)
        self.assertEqual(self.store.get(123)['empty_checks'], 0)


class SearchCacheTests(unittest.TestCase):
    @patch('bot.request_json', return_value={'states': []})
    def test_identical_search_is_reused_for_ten_seconds(self, request):
        provider = OpenSky()
        with patch('bot.time.monotonic', return_value=100):
            first = provider.nearby(38.7, -9.1, 50)
        with patch('bot.time.monotonic', return_value=109):
            self.assertEqual(provider.nearby(38.7, -9.1, 50), first)
        self.assertEqual(request.call_count, 1)
        with patch('bot.time.monotonic', return_value=110):
            provider.nearby(38.7, -9.1, 50)
        self.assertEqual(request.call_count, 2)

    @patch('bot.request_json', return_value={'states': []})
    def test_searches_and_provider_accounts_do_not_share_cache(self, request):
        first, second = OpenSky(), OpenSky()
        first.nearby(38.7, -9.1, 50)
        first.nearby(38.8, -9.1, 50)
        second.nearby(38.7, -9.1, 50)
        self.assertEqual(request.call_count, 3)

    @patch('bot.request_json', side_effect=[APIError(503), {'states': []}])
    def test_failed_search_is_not_cached(self, request):
        provider = OpenSky()
        with self.assertRaises(APIError):
            provider.nearby(38.7, -9.1, 50)
        provider.nearby(38.7, -9.1, 50)
        self.assertEqual(request.call_count, 2)

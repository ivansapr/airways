import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from bot import APIError, OpenSky, request_json
from tracking import APIUsage, WatchStore


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}


class UsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'usage.sqlite3')
        self.store = WatchStore(self.path)
        self.addCleanup(self.store.db.close)
        recorder = patch('bot.API_USAGE', APIUsage(self.path))
        recorder.start()
        self.addCleanup(recorder.stop)

    def test_success_and_balance_survive_restart_without_request_data(self):
        with patch('bot.urlopen', return_value=Response({'states': []}, {'X-Rate-Limit-Remaining': '3999'})):
            request_json('https://opensky-network.org/api/states/all?secret-location=123', usage_scope='owner')
        restored = WatchStore(self.path)
        self.addCleanup(restored.db.close)
        data = restored.dashboard()
        self.assertEqual(data['api_usage'][0]['requests'], 1)
        self.assertEqual(data['api_usage'][0]['errors'], 0)
        self.assertEqual(data['api_status'][0]['remaining'], 3999)
        self.assertNotIn('secret-location', json.dumps(data))

    def test_rate_limit_error_records_backoff_and_customer_balance_is_not_combined(self):
        error = HTTPError('https://opensky-network.org/api/states/all', 429, 'limited',
                          {'X-Rate-Limit-Retry-After-Seconds': '120', 'X-Rate-Limit-Remaining': '0'}, io.BytesIO(b'{}'))
        self.addCleanup(error.close)
        with patch('bot.urlopen', side_effect=error), self.assertRaises(APIError):
            request_json(error.url, usage_scope='customer')
        data = self.store.dashboard()
        self.assertEqual(data['api_usage'][0]['rate_limits'], 1)
        self.assertEqual(data['api_usage'][0]['errors'], 1)
        self.assertIsNone(data['api_status'][0]['remaining'])
        self.assertGreater(data['api_status'][0]['retry_until'], data['generated_at'] + 110)

    def test_transport_and_invalid_json_errors_count(self):
        for response in (URLError('private-url'), ValueError('invalid JSON')):
            with patch('bot.urlopen', side_effect=response), self.assertRaises(APIError):
                request_json('https://api.adsbdb.com/v0/aircraft/abc123')
        self.assertEqual(self.store.dashboard()['api_usage'][0]['errors'], 2)

    def test_telegram_application_errors_count_and_honor_retry_after(self):
        with patch('bot.urlopen', return_value=Response({'ok': False, 'error_code': 429,
                                                       'parameters': {'retry_after': 30}})):
            request_json('https://api.telegram.org/botSECRET/sendMessage', payload={'chat_id': 123})
        data = self.store.dashboard()
        self.assertEqual(data['api_usage'][0]['rate_limits'], 1)
        self.assertNotIn('SECRET', json.dumps(data))
        self.assertGreater(data['api_status'][0]['retry_until'], data['generated_at'] + 25)

    def test_retries_and_oauth_are_distinct_requests(self):
        responses = [Response({'access_token': 'first'}),
                     HTTPError('https://opensky-network.org/api/states/all', 401, 'expired', {}, io.BytesIO(b'{}')),
                     Response({'access_token': 'second'}),
                     Response({'states': []}, {'X-Rate-Limit-Remaining': '3998'})]
        self.addCleanup(responses[1].close)
        with patch('bot.urlopen', side_effect=responses):
            OpenSky('id', 'secret').nearby(38.7, -9.1, 50)
        usage = {row['provider']: row for row in self.store.dashboard()['api_usage']}
        self.assertEqual(usage['opensky']['requests'], 2)
        self.assertEqual(usage['opensky']['errors'], 1)
        self.assertEqual(usage['opensky_auth']['requests'], 2)

    def test_metadata_workers_can_write_concurrently(self):
        recorder = APIUsage(self.path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: recorder.record('adsbdb', 'shared', 200), range(24)))
        self.assertEqual(self.store.dashboard()['api_usage'][0]['requests'], 24)

    def test_telemetry_failure_does_not_break_requests(self):
        with patch('bot.API_USAGE.record', side_effect=OSError), patch('bot.urlopen', return_value=Response({'ok': True})):
            self.assertEqual(request_json('https://api.telegram.org/botSECRET/getMe'), {'ok': True})

    def test_utc_counters_and_invalid_remaining_header(self):
        recorder = APIUsage(self.path)
        with patch('tracking.time.time', return_value=1800000000):
            recorder.record('opensky', 'owner', 200, '3999')
        with patch('tracking.time.time', return_value=1800086400):
            recorder.record('opensky', 'owner', 200, 'invalid')
        data = self.store.dashboard()
        self.assertEqual(data['api_usage'], [])  # Historical counts are not attributed to today.
        self.assertEqual(data['api_status'][0]['remaining'], 3999)
        self.assertEqual(data['api_status'][0]['observed_at'], 1800000000)

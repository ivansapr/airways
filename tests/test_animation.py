import io
import unittest
from unittest.mock import patch, MagicMock
from email import policy
from email.parser import BytesParser

from PIL import Image
from bot import Aircraft, Bot, request_json
from flight_animation import board_pages, flight_gif


def plane():
    return Aircraft('abc123', 'TAP123', 'Portugal', 38.7, -9.1, 4.2, 9100, 200, 270, 5,
                    'Boeing 737-800', 'Lisbon → London')


class AnimationTests(unittest.TestCase):
    def test_gif_decodes_all_frames_and_loops_with_readable_holds(self):
        data = flight_gif(plane(), 1800000000)
        self.assertLess(len(data), 500000)
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.size, (960, 656))
        self.assertEqual(image.info['loop'], 0)
        holds = []
        for index in range(image.n_frames):
            image.seek(index)
            image.load()
            holds.append(image.info['duration'])
        self.assertGreaterEqual(image.n_frames, 10)
        self.assertEqual(sum(duration >= 3000 for duration in holds), 2)

    def test_pages_keep_full_city_names_and_convert_speed(self):
        p = plane()
        p.route = 'San Francisco → Rio de Janeiro'
        pages = board_pages(p, 1800000000)
        self.assertIn(('FROM', 'San Francisco'), pages[0][1])
        self.assertIn(('TO', 'Rio de Janeiro'), pages[0][1])
        self.assertIn(('GROUND SPEED', '720 KM/H'), pages[1][1])
        p.altitude = p.speed = None
        p.route = 'Route unavailable'
        p.aircraft_type = 'A very long aircraft model ' * 10
        self.assertTrue(flight_gif(p, 1800000000).startswith(b'GIF89a'))

    @patch('bot.urlopen')
    def test_binary_upload_is_valid_multipart(self, urlopen):
        response = MagicMock()
        response.read.return_value = b'{"ok":true,"result":{"message_id":7}}'
        urlopen.return_value.__enter__.return_value = response
        gif = flight_gif(plane(), 1800000000)
        bot = Bot('test', None, widgets_enabled=False)
        self.addCleanup(bot.store.db.close)
        bot.call('sendAnimation', chat_id=-123, caption='Lisbon → London', animation=gif,
                 disable_notification=True)
        request = urlopen.call_args.args[0]
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: '+request.get_header('Content-type')+'\r\n\r\n').encode()+request.data)
        fields = {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}
        self.assertEqual(fields['animation'].get_content_type(), 'image/gif')
        self.assertEqual(fields['animation'].get_payload(decode=True), gif)
        self.assertEqual(fields['chat_id'].get_payload(decode=True), b'-123')
        self.assertEqual(fields['disable_notification'].get_payload(decode=True), b'true')
        self.assertEqual(fields['caption'].get_payload(decode=True).decode(), 'Lisbon → London')

"""Offline amber dot-matrix flight boards, rendered from an existing observation."""
import io
import textwrap
import unicodedata
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 960, 656
BG, PANEL, GRID = '#171b23', '#1d222b', '#303640'
AMBER, LIT, MUTED = '#ffb82e', '#ffdc47', '#949ca6'
# Five-column LED glyphs. No downloaded fonts or external assets are needed.
GLYPHS = dict(zip('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-./:>? ', [
'01110 10001 10001 11111 10001 10001 10001',
'11110 10001 10001 11110 10001 10001 11110',
'01111 10000 10000 10000 10000 10000 01111',
'11110 10001 10001 10001 10001 10001 11110',
'11111 10000 10000 11110 10000 10000 11111',
'11111 10000 10000 11110 10000 10000 10000',
'01111 10000 10000 10111 10001 10001 01111',
'10001 10001 10001 11111 10001 10001 10001',
'11111 00100 00100 00100 00100 00100 11111',
'00111 00010 00010 00010 10010 10010 01100',
'10001 10010 10100 11000 10100 10010 10001',
'10000 10000 10000 10000 10000 10000 11111',
'10001 11011 10101 10101 10001 10001 10001',
'10001 11001 10101 10011 10001 10001 10001',
'01110 10001 10001 10001 10001 10001 01110',
'11110 10001 10001 11110 10000 10000 10000',
'01110 10001 10001 10001 10101 10010 01101',
'11110 10001 10001 11110 10100 10010 10001',
'01111 10000 10000 01110 00001 00001 11110',
'11111 00100 00100 00100 00100 00100 00100',
'10001 10001 10001 10001 10001 10001 01110',
'10001 10001 10001 10001 10001 01010 00100',
'10001 10001 10001 10101 10101 10101 01010',
'10001 10001 01010 00100 01010 10001 10001',
'10001 10001 01010 00100 00100 00100 00100',
'11111 00001 00010 00100 01000 10000 11111',
'01110 10001 10011 10101 11001 10001 01110',
'00100 01100 00100 00100 00100 00100 01110',
'01110 10001 00001 00010 00100 01000 11111',
'11110 00001 00001 01110 00001 00001 11110',
'00010 00110 01010 10010 11111 00010 00010',
'11111 10000 10000 11110 00001 00001 11110',
'01110 10000 10000 11110 10001 10001 01110',
'11111 00001 00010 00100 01000 01000 01000',
'01110 10001 10001 01110 10001 10001 01110',
'01110 10001 10001 01111 00001 00001 01110',
'00000 00000 00000 11111 00000 00000 00000',
'00000 00000 00000 00000 00000 00110 00110',
'00001 00010 00010 00100 01000 01000 10000',
'00000 00110 00110 00000 00110 00110 00000',
'10000 01000 00100 00010 00100 01000 10000',
'01110 10001 00001 00010 00100 00000 00100',
'00000 00000 00000 00000 00000 00000 00000']))
GLYPHS = {key: value.split() for key, value in GLYPHS.items()}


def led_text(value):
    return unicodedata.normalize('NFKD', str(value).replace('→', '>').replace('—', '-')).encode('ascii', 'ignore').decode().upper()


def draw_led(draw, value, box, max_step=7):
    x, y, width = box
    value = led_text(value) or 'UNKNOWN'
    # Long names wrap into two lines before shrinking the LED pitch.
    lines = textwrap.wrap(value[:240], width=30, break_long_words=True) or ['UNKNOWN']
    if len(lines) > 2:
        lines = textwrap.wrap(value[:240], width=60, break_long_words=True)
    lines = lines[:4]
    step = min(max_step, width / (max(map(len, lines)) * 6), 64 / (len(lines) * 8))
    radius = max(.6, step * .33)
    for row, line in enumerate(lines):
        for char_index, char in enumerate(line):
            glyph = GLYPHS.get(char, GLYPHS['?'])
            for gy in range(7):
                for gx in range(5):
                    cx, cy = x + (char_index * 6 + gx) * step, y + (row * 8 + gy) * step
                    draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=LIT if glyph[gy][gx]=='1' else GRID)


def board_pages(plane, timestamp):
    route = str(plane.route).split('→', 1)
    route = [part.strip() for part in route]
    origin, destination = route if len(route)==2 else (str(plane.route), 'Route unavailable')
    alt = f'{plane.altitude:,.0f} M'.replace(',', ' ') if plane.altitude is not None else 'UNKNOWN'
    speed = f'{plane.speed * 3.6:,.0f} KM/H'.replace(',', ' ') if plane.speed is not None else 'UNKNOWN'
    return [('FLIGHT OVERVIEW', [('FLIGHT', plane.callsign), ('FROM', origin), ('TO', destination), ('NEAR YOU', f'{plane.distance:.1f} KM AWAY')]),
            ('AIRCRAFT DETAILS', [('AIRCRAFT', plane.aircraft_type), ('ALTITUDE', alt), ('GROUND SPEED', speed), ('DISTANCE', f'{plane.distance:.1f} KM AWAY')])]


def render_page(plane, timestamp, index):
    title, rows = board_pages(plane, timestamp)[index]
    image = Image.new('RGB', (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20,20,939,635), radius=22, fill=PANEL, outline='#7b8286', width=5)
    font = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=16)
    draw.text((48,46), 'AIRWAYS  /  NEARBY FLIGHTS', font=font, fill=AMBER)
    draw.text((900,49), f'0{index+1} / 02', font=small, fill=MUTED, anchor='ra')
    draw.line((48,86,910,86), fill='#414750', width=1)
    for i, (label, value) in enumerate(rows):
        y = 108 + i * 112
        draw.text((50,y), label, font=small, fill=AMBER)
        draw_led(draw, value, (53,y+34,850))
        draw.line((48,y+99,910,y+99), fill='#303640', width=1)
    stamp = datetime.fromtimestamp(timestamp, timezone.utc).strftime('%H:%M UTC')
    draw.text((50,591), title, font=small, fill=AMBER)
    draw.text((908,591), f'SNAPSHOT  {stamp}', font=small, fill=MUTED, anchor='ra')
    return image


def flight_gif(plane, timestamp):
    """Loop overview → details with a staggered LED refresh, not new observations."""
    pages = [render_page(plane, timestamp, i) for i in range(2)]
    frames, durations = [], []
    for index in range(2):
        current, upcoming = pages[index], pages[1-index]
        frames.append(current)
        durations.append(3200)
        for phase in range(1,9):
            frame = current.copy()
            for row in range(4):
                progress = max(0, min(1, (phase-row) / 5))
                right = 48 + int(864 * progress)
                if right > 48:
                    frame.paste(upcoming.crop((48,108+row*112,right,207+row*112)), (48,108+row*112))
                if 0 < progress < 1:
                    ImageDraw.Draw(frame).rectangle((right,108+row*112,right+4,207+row*112), fill=AMBER)
            if phase == 8:
                frame = upcoming.copy()
            frames.append(frame)
            durations.append(70)
    # One fixed palette prevents flicker between frames.
    palette = pages[0].quantize(colors=32)
    frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    output = io.BytesIO()
    frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True, disposal=1)
    return output.getvalue()

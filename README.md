# Airways

A Telegram bot: send your location and get a snapshot of nearby airborne aircraft, sorted by distance. Includes callsign, ICAO address, distance, altitude, ground speed, heading, position age, registered country, and map/tracker links. Shows up to eight aircraft within 50 km by default.

## Run locally

Requires Python 3.10+; no packages to install.

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, and follow its instructions to obtain your bot token.
2. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`. Keep this file private.
3. Optionally create API client credentials on your [OpenSky account](https://opensky-network.org/my-opensky/account) and fill in both OpenSky variables. These use OAuth2, not your account password. Anonymous requests work with a smaller quota.
4. Start the bot from this directory:

```sh
cp .env.example .env # only on initial setup; then edit .env before continuing
set -a
source .env
set +a
python3 bot.py
```

Open your bot in a private Telegram chat, send `/start`, and tap **Share my location**. You can also send a map pin using **Attach → Location**. Location sharing is easiest in the mobile Telegram app. `/help` displays instructions.

Set `SEARCH_RADIUS_KM` to a number from 1 to 250 to change the search radius. A live location's edits trigger new snapshots, with a 15-second cooldown; there is no background subscription or automatic alert when a new aircraft enters the area. Send another location for a fresh search.

## Docker

```sh
docker build -t airways .
docker run --rm --env-file .env airways
```

Run one instance per bot token. Long polling needs outbound HTTPS and no public server or inbound port. A preexisting Telegram webhook must be removed before starting. For continuous operation, run the container with your host's restart policy or use a process manager. The bot is not deployed by this repository.

## Data and privacy

Aircraft positions come from the [OpenSky Network REST API](https://openskynetwork.github.io/opensky-api/rest.html); its coverage and API quota affect results. Searches query a bounding box, then filter by great-circle distance, exclude ground vehicles/aircraft and positions older than two minutes, and handle the date line and poles. “No aircraft” means no recent reports in the feed, not necessarily an empty sky. Distances are horizontal, not slant distances. Altitude uses geometric altitude when available, otherwise barometric altitude. Registered country is inferred from the transponder address, not the flight's origin.

Live state vectors do not include routes, destinations, airline names, aircraft models, or passenger flight numbers. A callsign may differ from a booking flight number. Those details require an additional provider. Review [OpenSky's terms](https://opensky-network.org/about/terms-of-use) for your intended use.

The bot does not persist or log locations or messages. Search areas are sent to OpenSky, and Telegram handles messages under its own policies. A short-lived in-memory chat-ID cooldown limits requests; bot restarts clear it. Update offsets are held in memory, so pending messages may be processed again after a restart. Failed Telegram replies are logged by status and skipped; users can resend their location. Provider errors are shown as unavailable, rather than as an empty sky.

## Verify

```sh
python3 -m unittest discover -s tests -v
```

Tests use mocked services; live operation requires your Telegram token and network access. API behavior follows [Telegram's Bot API](https://core.telegram.org/bots/api) and the OpenSky documentation linked above.

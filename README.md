# Airways

Send a location to the Telegram bot to start live nearby-flight alerts. Airways works in private chats and groups, saves one watch location per chat, and notifies the chat when aircraft enter the search radius.

Reports include callsign, route, aircraft type, distance, altitude, speed, heading, position age, and map/tracker links. Live positions come from OpenSky; route and aircraft details are looked up with adsbdb. Missing details appear as **Unknown**. Routes are callsign-based database matches, not a verified live itinerary, and can be outdated.

A password-protected admin dashboard shows usage, chats, active watches, and bot health. No external Python packages are required. Python 3.10+ is supported.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) using `/newbot`.
2. Copy `.env.example` to `.env`, then set `TELEGRAM_BOT_TOKEN`.
3. Set `ADMIN_PASSWORD` to enable the dashboard. Leave it empty to disable it.
4. Optionally set both `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` from your [OpenSky account](https://opensky-network.org/my-opensky/account). These are OAuth2 API client credentials, not your account password.
5. Load your environment and run:

```sh
set -a
source .env
set +a
python3 bot.py
```

For local HTTP dashboard access, set `ADMIN_SECURE_COOKIE=0` in `.env` before loading it. Open [the local admin page](http://127.0.0.1:8080) and sign in using `ADMIN_PASSWORD`. The local default binds only to `127.0.0.1`.

The dashboard can also run independently against the same state database:

```sh
python3 admin.py
```

## Using the bot

In a private chat, send `/start` and tap **Share my location**, or use **Attach → Location**. Sharing a pin immediately starts a persistent watch, returns current aircraft, and enables arrival alerts. A location message is enough; you do not need to keep sharing a live location.

For groups:

1. Add the bot and promote it to administrator so Telegram reliably delivers location messages and administrator checks.
2. A group administrator sends a location pin, or replies to an existing pin with `/watch@YourBotUsername`.
3. Alerts are delivered to the group. Only group administrators can change the location or stop the watch.

The location-request keyboard is available in private chats only. With Telegram privacy mode enabled and the bot not promoted, use an explicitly addressed `/watch@YourBotUsername` command replying to a map pin. Do not use anonymous administrator mode when configuring a watch: the bot verifies the sender's user ID. The bot ignores ordinary group conversation.

| Command | Action |
| --- | --- |
| `/start`, `/help` | Show location-sharing instructions |
| `/watch` | Reply to a map pin to start or move a watch |
| `/status` | Show whether this chat has an active watch |
| `/stop` | Stop alerts and delete the chat's saved watch location |

Live-location edits update the active watch, at most once every 15 seconds. Once sharing expires, monitoring continues around its last position until `/stop`. Edits to stopped or replaced pins cannot reactivate the watch.

## Arrival alerts

The default radius is **50 km** and checks run approximately every **60 seconds**. Set `SEARCH_RADIUS_KM` (1–250) and `POLL_INTERVAL_SECONDS` (at least 15) to change them. Polling is sequential; network latency and the number of active chats can lengthen the interval. Aircraft that enter and leave between polls may be missed.

The initial report shows up to eight nearest aircraft. Subsequent checks send a combined alert for up to eight newly detected aircraft, retrying remaining arrivals on later checks if they are still nearby. Aircraft already reported do not generate another arrival alert while continuously present. An aircraft can trigger again after at least ten minutes since its last observation and a successful check confirming its absence. Provider outages do not clear duplicate history. If an alert cannot be delivered, it is retried on subsequent checks while the aircraft remains nearby. A crash between delivery and saving history may produce a duplicate after restart.

Watches, duplicate history, and Telegram update offsets are stored in SQLite. Keep the database on persistent storage. `/stop` deletes location and duplicate history, while aggregate chat activity remains visible in the dashboard. The bot also removes watches when Telegram reports that it was removed or blocked.

## Admin dashboard

The dashboard refreshes every 15 seconds and includes:

- Total chats and active watches.
- Today's arrival alerts, aircraft alerted, flight searches, and errors.
- A seven-day chart of alert messages, using UTC dates.
- Bot heartbeat and flight-data/delivery error counts.
- Searchable chats with group/private filters, saved watch coordinates, radius, update count, alert count, and last activity.
- A **Stop watch** action with a confirmation dialog; it deletes that watch's location without deleting chat usage history.

Chat activity counts refer to updates the bot receives, including live-location edits; they do not measure all messages or members in a group. Flight searches count application-level lookup attempts, not provider billing credits or HTTP requests. Initial snapshots are not counted as arrival alerts. Metrics begin when this version is installed; historical data is not imported from Telegram. A heartbeat older than three minutes is shown as delayed, which can also indicate a slow or overloaded polling loop.

The dashboard uses an eight-hour, HttpOnly, SameSite session cookie, CSRF-protected actions, and throttled failed sign-ins. Sessions expire on process restart. It never displays the bot token or API credentials. Leave `ADMIN_SECURE_COOKIE=1` behind HTTPS. Set `ADMIN_HOST=0.0.0.0` when exposing a locally run server through a reverse proxy. Default port: `8080`.

## Docker and Coolify

```sh
docker build -t airways .
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 -v airways-data:/data airways
```

The image binds the admin server to `0.0.0.0:8080` when a password is set. For local HTTP access use `ADMIN_SECURE_COOKIE=0`; for an HTTPS reverse proxy keep it `1`.

For Coolify, create an application from this repository using its Dockerfile, configure the environment variables, and mount persistent storage at `/data`. Route your HTTPS admin domain to port `8080`, and set `ADMIN_PASSWORD` to enable the web server. Disable HTTP health checks if running with the dashboard disabled. SQLite's data directory must be writable by container UID `65534`.

Run one bot instance per Telegram token. Long polling requires outbound HTTPS, and any existing Telegram webhook must be removed before starting. The admin web server is optional; the bot itself does not need inbound ports. This repository does not create a Coolify deployment automatically.

## Data sources, quotas, and privacy

Aircraft positions use the [OpenSky REST API](https://openskynetwork.github.io/opensky-api/rest.html). Each active watch normally consumes one states query per interval, or two near the date line. At 60-second intervals, a continuously running watch needs about 1,440 queries per day, plus initial searches. Anonymous access has a much smaller allowance; use OAuth credentials and an interval appropriate to your quota and chat count. Rate-limit responses pause queries until the provider permits retries. There is no guarantee that a free quota supports every watch all day.

Searches use great-circle horizontal distance, exclude ground positions, and discard positions older than two minutes. Coverage is incomplete: an empty response does not necessarily mean an empty sky. Altitude uses geometric altitude when available, otherwise barometric altitude, in metres; speed is ground speed in km/h. Registered country is inferred from the aircraft transponder address, not its departure airport.

[adsbdb](https://www.adsbdb.com/) supplies aircraft type and origin/destination airport codes, with an intermediate airport when supplied. Lookups use public aircraft identifiers and callsigns; a short-lived, bounded in-memory cache reduces repeat requests. Route information is not copied into the persistent database. Review [OpenSky's terms](https://opensky-network.org/about/terms-of-use) and [adsbdb's data-source notes](https://github.com/FugginOld/adsb-db) for your use.

SQLite stores chat IDs, display names/titles, chat type, aggregate counts, activity timestamps, and active watch coordinates/history. The bot does not store message contents or log location coordinates. Search areas are sent to OpenSky, aircraft identifiers are sent to adsbdb, and Telegram processes bot messages under its own policies. The database is ignored by Git and excluded from Docker build context.

## Verification

```sh
python3 -m unittest discover -s tests -v
node --check admin_static/admin.js
```

The Python tests include local HTTP dashboard authentication/actions and mocked Telegram/provider flows, arrival deduplication, persistence, group permissions, missing metadata, rate limits, date-line searches, and polar bounds. Live Telegram operation requires credentials and network access. API integration references: [Telegram Bot API](https://core.telegram.org/bots/api), [Telegram privacy-mode behavior](https://core.telegram.org/bots/faq), and the provider documentation above.

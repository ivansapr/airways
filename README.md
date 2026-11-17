# Airways

Send a location to the Telegram bot to check nearby flights. Free chats get a one-off snapshot; Promo and Bring Your Key chats also get a persistent arrival watch. Airways works in private chats and groups and saves one watch location per eligible chat.

Each aircraft gets its own flight card with a photo when available, callsign, full route city names, readable aircraft name (for example, Boeing 737-800), altitude, speed, and a tracker link on the callsign. Heading, position age, ICAO address, and registered country are omitted from the displayed card. Live positions come from OpenSky; route and aircraft details are looked up with adsbdb. Missing details appear as **Unknown**. Routes are callsign-based database matches, not a verified live itinerary, and can be outdated.

A password-protected admin dashboard shows usage, chats, active watches, and bot health. Python 3.10+ is supported. The cryptography package encrypts customer API credentials at rest.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) using `/newbot`.
2. Copy `.env.example` to `.env`, then set `TELEGRAM_BOT_TOKEN`.
3. Set `ADMIN_PASSWORD` to enable the dashboard. Leave it empty to disable it.
4. Set `OPENSKY_CLIENT_ID` and `OPENSKY_CLIENT_SECRET` from your [OpenSky account](https://opensky-network.org/my-opensky/account) to fund Free snapshots and Promo watches. These are OAuth2 API client credentials, not your account password. Leaving both empty disables these tiers' flight data; Bring Your Key still works.
5. Install dependencies and generate a credential-encryption key once:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Save the generated key as `CREDENTIAL_ENCRYPTION_KEY` in `.env` or Coolify. Keep it stable across restarts, and back it up separately from the database. If it is absent, promo codes still work, but own-credential setup is disabled.

6. Load your environment and run:

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

In a private chat, send `/start` and tap **Share my location**, or use **Attach → Location**. Free chats receive current aircraft without saving a location or starting a watch. Promo and Bring Your Key chats also start a persistent watch and receive arrival alerts. A location message is enough; you do not need to keep sharing a live location.

For groups:

1. Add the bot and promote it to administrator so Telegram reliably delivers location messages and administrator checks.
2. A group administrator activates the group using a promo code or connects credentials privately, then sends a location pin or replies to an existing pin with `/watch@YourBotUsername`.
3. Alerts are delivered to the group. Only group administrators can change the location or stop the watch.

The location-request keyboard is available in private chats only. With Telegram privacy mode enabled and the bot not promoted, use an explicitly addressed `/watch@YourBotUsername` command replying to a map pin. Do not use anonymous administrator mode when configuring a watch: the bot verifies the sender's user ID. The bot ignores ordinary group conversation.

| Command | Action |
| --- | --- |
| `/start`, `/help` | Show location-sharing instructions |
| `/watch` | Reply to a map pin to start or move a watch |
| `/status` | Show whether this chat has an active watch |
| `/stop` | Stop alerts and delete the chat's saved watch location; retain access |
| `/promo CODE` | Redeem an invitation for this chat |
| `/register` | OpenSky registration/sign-in link and API credential setup instructions |
| `/credentials CLIENT_ID CLIENT_SECRET` | Connect own OpenSky credentials in a private chat |
| `/credentials GROUP_ID CLIENT_ID CLIENT_SECRET` | Privately connect credentials for a group you administer |
| `/access` | Show this chat's access mode and promo expiry |
| `/disconnect` | Remove access, encrypted credentials, and watch |
| `/disconnect GROUP_ID` | Privately remove a group's access as its credential owner or administrator |

Live-location edits update the active watch, at most once every 15 seconds. Once sharing expires, monitoring continues around its last position until `/stop`. Edits to stopped or replaced pins cannot reactivate the watch.

## Activation and promo codes

| Tier | Current location check | Arrival watch | Flight-data account |
| --- | --- | --- | --- |
| Free | Yes, on demand | No | Owner |
| Promo | Yes | Yes, until expiry/revocation | Owner |
| Bring Your Key | Yes | Yes, no app usage quota | User |

Free is the default. Free checks are explicit snapshots, never background polling or live-location edit requests. Promo expiry/revocation returns the chat to Free; existing watches remain paused until watch access is restored, and `/stop` deletes them. Provider quotas and rate limits still apply to every tier, as does the 15-second search cooldown. Free snapshots consume the same owner allowance as Promo watches. There is no anonymous provider fallback, and an invalid Bring Your Key credential never silently falls back to the owner.

To enable watches, users choose one of two options:

- **Use your account:** In the admin dashboard, create an invitation under **Promo codes**, copy the code shown once, and share it with the intended recipient. The recipient sends `/promo CODE` in the private chat or group to activate. A group administrator must redeem it. A code grants access only to the redeeming chat, not every group the user joins.
- **Bring Your Key:** `/register`, `/start`, `/access`, or `/credentials` without arguments shows a registration/sign-in link to the [OpenSky account website](https://opensky-network.org/my-opensky/account) and the official [API setup guide](https://openskynetwork.github.io/opensky-api/rest.html#authentication). Users create an OpenSky account, create an API client in their account, and send `/credentials CLIENT_ID CLIENT_SECRET` privately to the bot. Use API client credentials, not the website password. For a group, a group administrator privately sends `/credentials GROUP_ID CLIENT_ID CLIENT_SECRET`; the group ID appears in the instructions. The bot validates the credentials through OpenSky OAuth before storing them. Credentials are never accepted from group messages.

Codes default to one chat, a seven-day redemption deadline, and thirty days of access starting at redemption. Configure these fields when issuing a code. The redemption deadline does not shorten an already issued access grant. Redemptions are atomic so concurrent users cannot exceed a code's chat limit. Replaying a code in the same chat does not renew or restore access; use a new code to renew. Failed activation attempts are throttled.

The dashboard accepts an optional **Custom code**, for example `FAMILY2026`. Leave it blank to generate a random code. Custom codes use 6–64 letters, digits, hyphens or underscores, are normalized to uppercase, and cannot duplicate any existing code (including expired/revoked codes). After creation, use **Copy code** or **Copy /promo command**. If the clipboard is unavailable, the field is selected for manual copying. Codes are shown only at creation and stored as hashes, so copy them before leaving the page.

Revoke an invitation in the dashboard to disable owner access for every chat currently using it, or use **Revoke access** on a particular chat to remove its access, stored credentials, and watch. Users can do the same through `/disconnect`. Expiry/revocation is checked before each search; requests already in progress may finish. Promo codes authorize use of your API allowance; they do not buy provider credits or guarantee capacity within your provider quota.

User credentials are encrypted with Fernet before entering SQLite. Raw codes are stored only as hashes; the full code is returned once when created and cannot be recovered from the dashboard. Neither credentials nor encrypted credential values are included in dashboard responses or application logs. Each credential-backed chat has its own OAuth client/token and rate-limit handling; failures never switch it onto the owner's account. Changing or losing the encryption key requires affected users to reconnect their credentials. `/stop` preserves access for reuse, while `/disconnect` deletes access and the saved watch.

Credential commands pass through Telegram private messages; the bot attempts to delete the command immediately but deletion can fail. It never echoes secrets. If credentials were accidentally posted in a group, rotate them at OpenSky. Removing/blocking the bot removes the chat's access and saved credentials when Telegram delivers the membership update.

## Arrival alerts

The default radius is **50 km** and checks run approximately every **60 seconds**. Set `SEARCH_RADIUS_KM` (1–250) and `POLL_INTERVAL_SECONDS` (at least 15) to change them. Polling is sequential; network latency and the number of active chats can lengthen the interval. Aircraft that enter and leave between polls may be missed.

Optional quiet-area polling: set `IDLE_POLL_MAX_SECONDS=300` with a 60-second base to use 120, 240, then 300 seconds after consecutive successful empty checks. Any detected aircraft restores 60 seconds. Errors never count as empty sky. The empty-check count persists across restarts; replacing/moving a watch resets it. Leave this setting blank to preserve fixed polling. The maximum must be at least the base interval and no more than 3600 seconds (or the base when the base is already larger). Slower checks can miss short visits: an aircraft arriving once an hour is not necessarily present for an hour. `/status` and the dashboard show each watch's current cadence.

The initial report shows up to eight nearest aircraft. Subsequent checks send individual cards for up to eight newly detected aircraft, retrying remaining arrivals on later checks if they are still nearby. A matching aircraft photo is attached when available; if Telegram cannot load it, the bot sends the same card as text. Successful card deliveries are saved individually, so retrying a partially delivered batch does not repeat its successful cards. Aircraft already reported do not generate another arrival alert while continuously present. An aircraft can trigger again after at least ten minutes since its last observation and a successful check confirming its absence. Provider outages do not clear duplicate history. If an alert cannot be delivered, it is retried on subsequent checks while the aircraft remains nearby. A crash between delivery and saving history may produce a duplicate after restart.

Watches, duplicate history, and Telegram update offsets are stored in SQLite. Keep the database on persistent storage. `/stop` deletes location and duplicate history, while aggregate chat activity remains visible in the dashboard. The bot also removes watches when Telegram reports that it was removed or blocked.

## Telegram flight preview

Each flight sends one aircraft photo with a compact caption: a bold tracker-linked callsign beside the route, aircraft model on the next line, then altitude and speed. The footer contains only OpenSky Network and adsbdb source links. Distance, date/time, field labels, and the separate map link are omitted. If a matching photo is unavailable or rejected by Telegram, the same information is sent as a text card. No additional animation message is sent; the old `TELEGRAM_ANIMATIONS` and `TELEGRAM_WIDGETS` settings are no longer used.

See the [static photo-card preview](preview/README.md) for a sample alert.

## Admin dashboard

The dashboard refreshes every 15 seconds and includes:

- Total chats and active watches.
- Today's arrival alerts, aircraft alerted, flight searches, and errors.
- A seven-day chart of alert messages, using UTC dates.
- Bot heartbeat and flight-data/delivery error counts.
- Searchable chats with group/private filters, saved watch coordinates, radius, update count, alert count, and last activity.
- A **Stop watch** action with a confirmation dialog; it deletes that watch's location without deleting chat usage history.

Chat activity counts refer to updates the bot receives, including live-location edits; they do not measure all messages or members in a group. Flight searches count application-level lookup attempts, not provider billing credits or HTTP requests. Initial snapshots are not counted as arrival alerts. Metrics begin when this version is installed; historical data is not imported from Telegram. A heartbeat older than three minutes is shown as delayed, which can also indicate a slow or overloaded polling loop.

**API usage & limits** shows actual HTTP attempts, errors (including transport/JSON failures), 429 responses, and observed retry windows for OpenSky flight data, OpenSky OAuth, adsbdb metadata, and Telegram. Daily counters use UTC and persist across restarts. They start after deploying this version; existing flight-check metrics cannot reconstruct past API requests. OpenSky owner and customer traffic are counted separately; customer account balances are not combined. The owner balance comes from `X-Rate-Limit-Remaining`, includes an observation time, and is marked stale on later UTC dates. It is a snapshot, not a live balance or a measurement of this bot's credit spend. No additional provider calls are made to populate the dashboard. Only service labels, account categories, counters, balances, and timestamps are stored, never URLs, request bodies, tokens, or credentials.

The [OpenSky credit model](https://openskynetwork.github.io/opensky-api/rest.html#api-credits) currently gives standard users 4,000 credits/day and active feeders 8,000/day; licensed plans differ. State queries cost 1–4 credits according to bounding-box area. Telegram's [delivery limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this) apply to messages and chats, not all API calls; polling requests count in usage but do not consume a message allowance. adsbdb's quota is shown as unknown rather than assumed unlimited. Observed backoff is informational; existing provider retry handling remains responsible for scheduling.

To reduce usage, increase `POLL_INTERVAL_SECONDS` from 60 to 120 (half as many scheduled queries), optionally enable quiet-area polling, stop unused watches, and limit owner-funded invitations. Longer intervals can miss aircraft passing between checks. The dashboard estimates scheduled checks/day using each watch's current cadence; Free snapshots, date-line splits and retries add requests. Identical OpenSky searches within ten seconds reuse a successful response on the same provider instance, with fresh aircraft objects per caller. Cached timestamps remain the original observation time; failures are not cached. The owner provider is shared by Free and Promo chats; Bring Your Key providers remain isolated per chat. Broader regional query sharing and coalescing simultaneous adsbdb cache misses are future improvements. Metadata already has a one-hour success cache and five-minute error cache, with lookups limited to aircraft being shown in a snapshot or announced as arrivals.

OpenSky supports authenticated state snapshots up to one hour in the past. This is not a complete list of aircraft that crossed a location: recovering missed crossings requires sufficiently frequent historical samples, each costing provider credits. Historical polling is not implemented and is not assumed to save quota.

The dashboard uses an eight-hour, HttpOnly, SameSite session cookie, CSRF-protected actions, and throttled failed sign-ins. Sessions expire on process restart. It never displays the bot token or API credentials. Leave `ADMIN_SECURE_COOKIE=1` behind HTTPS. Set `ADMIN_HOST=0.0.0.0` when exposing a locally run server through a reverse proxy. Default port: `8080`.

## Docker and Coolify

```sh
docker build -t airways .
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 -v airways-data:/data airways
```

The image binds the admin server to `0.0.0.0:8080` when a password is set. For local HTTP access use `ADMIN_SECURE_COOKIE=0`; for an HTTPS reverse proxy keep it `1`.

For Coolify, create an application from this repository using its Dockerfile, configure the environment variables, and mount persistent storage at `/data`. Route your HTTPS admin domain to port `8080`, and set `ADMIN_PASSWORD` to enable the web server. Disable HTTP health checks if running with the dashboard disabled. SQLite's data directory must be writable by container UID `65534`.

### Keep data across redeployments

For a Dockerfile application, open **Persistent Storage → Add Mount → Volume mount** in Coolify and configure:

| Setting | Value |
| --- | --- |
| Name | `airways-data` |
| Destination path | `/data` |
| Environment variable | `STATE_DB_PATH=/data/airways.sqlite3` |

Coolify adds the application's resource prefix to the volume name. Save the mount and redeploy. Keep this same volume attached on every deployment. Mount the whole directory, so SQLite's database, WAL, and shared-memory files are together. Keep `CREDENTIAL_ENCRYPTION_KEY` unchanged as well, otherwise saved customer credentials cannot be decrypted.

The Dockerfile's `VOLUME /data` declaration alone does **not** configure a reusable named volume in Coolify. Without an explicit mount, a fresh anonymous volume can be attached to each replacement container, making existing data appear to disappear. See [Coolify's persistent storage documentation](https://coolify.io/docs/applications/configuration/persistent-storage).

For a **new** Docker Compose deployment, [compose.yaml](compose.yaml) declares a named volume and fixes the database path at `/data/airways.sqlite3`. It exposes internal port `8080` for your Coolify domain; it does not publish a host port. If migrating an existing application to Compose, set `AIRWAYS_DATA_VOLUME` to the exact existing Docker volume name before deploying. Do not switch an existing application to an empty volume. Never run `docker compose down -v` for data you want to retain.

If records disappear after a deployment, check retained Docker volumes **before deleting or pruning them**: the old database may still exist. Back up a candidate using SQLite's backup API so any committed WAL content is included, verify its integrity and record counts, then restore it into the named volume while no app is using that destination. A raw copy of an active `.sqlite3` file alone may omit recent changes. Retain the original volume and recovery backup until the restored app is verified. Persistent storage is local to the deployment server; use separate backups for server loss.

Run one bot instance per Telegram token. Long polling requires outbound HTTPS, and any existing Telegram webhook must be removed before starting. The admin web server is optional; the bot itself does not need inbound ports. This repository does not create a Coolify deployment automatically.

## Data sources, quotas, and privacy

Aircraft positions use the [OpenSky REST API](https://openskynetwork.github.io/opensky-api/rest.html). Each active watch normally consumes one states query per interval, or two near the date line, unless its search is served by the short-lived cache. At fixed 60-second intervals, a continuously running watch needs about 1,440 queries per day, plus initial searches. Free snapshots add owner-funded requests. Use OAuth credentials and an interval appropriate to the credential owner's quota and chat count. Anonymous access is disabled. Rate-limit responses pause queries until the provider permits retries. There is no guarantee that a free provider quota supports every watch all day.

Searches use great-circle horizontal distance, exclude ground positions, and discard positions older than two minutes. Coverage is incomplete: an empty response does not necessarily mean an empty sky. Altitude uses geometric altitude when available, otherwise barometric altitude, in metres; speed is ground speed in km/h. Transponder addresses and position age are still used internally to identify aircraft and discard stale data, but are not displayed.

[adsbdb](https://www.adsbdb.com/) supplies aircraft type, manufacturer, photo URLs, and origin/destination city names, with an intermediate city when supplied. Route labels show the city followed by its IATA code, for example Valencia (VLC). If IATA is unavailable, the ICAO code is used when supplied. City labels use the airport municipality; when it is missing, the full airport name is used, otherwise Unknown. Missing codes are omitted. Photos are looked up for the particular aircraft, not generated or substituted with an unrelated plane. Lookups use public aircraft identifiers and callsigns; a short-lived, bounded in-memory cache reduces repeat requests. Route information is not copied into the persistent database. Review [OpenSky's terms](https://opensky-network.org/about/terms-of-use) and [adsbdb's data-source notes](https://github.com/FugginOld/adsb-db) for your use.

SQLite stores chat IDs, display names/titles, chat type, aggregate counts, activity timestamps, active watch coordinates/history, access grants, encrypted API credentials, hashed promo codes, and redemption records. The bot does not store message contents or log location coordinates. Search areas are sent to OpenSky, aircraft identifiers are sent to adsbdb, and Telegram processes bot messages under its own policies. The database is ignored by Git and excluded from Docker build context.

## Verification

```sh
source .venv/bin/activate
python -m unittest discover -s tests -v
node --check admin_static/admin.js
```

The Python tests include local HTTP dashboard authentication/actions and mocked Telegram/provider flows, arrival deduplication, persistence, group permissions, missing metadata, aircraft photos and text fallbacks, city-name routes, partial-delivery retries, rate limits, date-line searches, polar bounds, promo redemption races/expiry/revocation, encrypted credential persistence, protection against unauthorized owner-account usage, widget payload limits, and widget delivery fallbacks. Live Telegram operation requires credentials and network access. API integration references: [Telegram Bot API](https://core.telegram.org/bots/api), [Telegram privacy-mode behavior](https://core.telegram.org/bots/faq), and the provider documentation above.

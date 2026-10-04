# DriveScore

Usage-based car insurance proof of concept for the HKAI Summit hackathon (Hong Kong).

DriveScore turns phone sensor data into a driving-risk score, and the score into a premium
multiplier. This repository holds the FastAPI + PostgreSQL backend (ingestion, signal
processing, model hook, reports) and the Expo / React Native app that records trips.

Contents: [What it is](#1-what-it-is) | [How it works](#2-how-it-works) |
[Repository layout](#3-repository-layout) | [Tech stack](#4-tech-stack) |
[Quick start](#5-quick-start) | [API overview](#6-api-overview) |
[Processing pipeline](#7-processing-pipeline) | [Scoring](#8-scoring) |
[Data model](#9-data-model) | [Privacy](#10-privacy-pdpo) | [Development](#11-development) |
[Limitations](#12-assumptions-and-known-limitations) | [Roadmap](#13-roadmap) | [Team](#14-team)

## 1. What it is

Built for the bolttech hackathon track in Hong Kong: a usage-based insurance (UBI) POC where
premiums follow how a person actually drives rather than who they are.

**Pitch.** A driver installs the app and consents once. From then on the phone records
accelerometer, gyroscope and GPS speed (no coordinates) during trips and uploads them in small
chunks. The backend
removes noise, works out whether the person was driving (or a passenger),
detects harsh braking, harsh acceleration, sharp cornering, speeding and possible crashes,
and turns each trip into features for a risk model. Trip scores roll up into a
distance-weighted 90-day driver score, which maps to a tier (A to E) and a premium
multiplier between 0.80 and 1.30. The driver sees their score and trip explanations in the
app; the insurer sees drivers by id with tier, multiplier and event rates. No
protected attributes (age, gender, etc.) are used, and a driver can delete all their data.

Status: the backend API, pipeline, classification, scoring, the insurer web dashboard and the
mobile app (onboarding with consent, Home / Trips / Coach / Privacy, English and Chinese) work
end to end against synthetic and real sensor data. The whole stack (API + PostgreSQL + dashboard)
runs with `docker compose up`. Still unbuilt: automatic car-audio and activity detection (phase H),
dashboard login hardening, and rewritten handoff docs (G1); see [Roadmap](#13-roadmap).

## 2. How it works

```
 Phone (Expo app)
 accelerometer 50 Hz, gyroscope 50 Hz, GPS speed 1 Hz (time, speed, accuracy only), (car Bluetooth flag)
        |
        |  POST /v1/trips/start            (needs prior consent)
        |  POST /v1/trips/{id}/chunks      (~60 s chunks, idempotent by seq)
        |  POST /v1/trips/{id}/end         (only after last chunk acknowledged)
        v
 +----------------------+      raw chunks, gzip JSON, one file per chunk
 | FastAPI (sync)       |----> data/raw/<trip_id>/<seq>.json.gz
 | ingestion router     |      + trip, trip_chunks rows in PostgreSQL
 +----------+-----------+
            |  BackgroundTasks (thread pool): process_trip(trip_id)
            v
 +-------------------------------------------------------------------+
 | Pipeline (app/pipeline/, app/classify.py)                         |
 |  1. load chunks -> IMU / GPS-speed tables, bluetooth ratio        |
 |  2. classify trip: driver | unknown (+ driver_likelihood)         |
 |       transit or passenger -> saved as done, NOT scored           |
 |  3. quality checks (samples, GPS-speed gap, duration, distance)   |
 |  4. resample 50 Hz -> remove gravity -> car frame -> low-pass     |
 |  5. detect events: harsh_brake / harsh_accel / sharp_corner /     |
 |     speeding                                                      |
 |  6. detect crash -> Incident row (needs driver confirmation)      |
 |  7. build features (FEATURE_ORDER vector)                         |
 +-------------------------------+-----------------------------------+
                                 v
                  app/model.py predict(features)
                  placeholder rules  OR  models/model.pkl (MODEL_PATH)
                  OR  window model (MODEL_KIND=window, v2 default):
                  250-sample windows, share with risk > 0.5 -> confidence
                                 v
              confidence (0..1) -> score (0..100) -> tier A..E
                                 |
                                 v
        PostgreSQL: events, trip_features, trip_scores, incidents
                                 |
              +------------------+--------------------+
              v                                       v
   Driver app (X-API-Key = driver)          Insurer views (X-API-Key = insurer)
   GET /v1/me/summary  (score, tier,        GET /v1/insurer/overview
       premium multiplier, trend)           GET /v1/insurer/drivers
   GET /v1/me/trips, /v1/me/trips/{id}      GET /v1/insurer/drivers/{driver_id}
   label trips, confirm incidents           POST /v1/trips/{id}/reprocess
```

Trip lifecycle (`trips.status`): `uploading` -> `processing` -> `done` or `failed`
(`failure_reason` explains quality-check failures). The app polls
`GET /v1/trips/{id}/status` after ending a trip.

## 3. Repository layout

```
.
|-- README.md                    this file (main entry point)
|-- docker-compose.yml           services: db (PostgreSQL 16, creates drivescore_test) and api
|-- .github/workflows/ci.yml     CI: ruff format/check, ty, pytest on Postgres
|-- docs/
|   |-- roadmap.md               phased plan, one line = one small PR
|   |-- data-model.md            detailed data model: ER diagram, tables, allowed values
|   `-- handoff.md               older team handoff guide (stale, see limitations)
|-- contracts/                   older hand-written API contract (stale, replaced by OpenAPI)
|-- model/                       window model: research, training, artifacts (see model/README.md)
|   |-- README.md, TRAINING.md   overview; findings, assumptions, retraining record
|   |-- remade_model/            v1 model.json + serve.py (reference scorer)
|   |-- retrained_model/         v2 model.json + metrics.json (served by default)
|   |-- retrain/                 v2 feature and training code (features_v2.py, train_v2.py, ...)
|   |-- eval_ferreira.py         evaluation on the Ferreira 2017 phone events
|   `-- data/                    external datasets (gitignored, not in the repo)
|-- backend/                     FastAPI service (see backend/README.md)
|   |-- app/
|   |   |-- main.py              app, CORS, router mounting, /health, model load on startup
|   |   |-- config.py            pydantic-settings; all env variables
|   |   |-- database.py          sync SQLAlchemy engine and session
|   |   |-- models.py            8 typed ORM tables (Mapped), with column comments
|   |   |-- values.py            Literal types for allowed values (status, tier, ...)
|   |   |-- schemas.py           API contract: Pydantic models with field docs (see /docs)
|   |   |-- features.py          TripFeatures: feature contract with the model team
|   |   |-- auth.py              API key hashing, driver and insurer dependencies
|   |   |-- pipeline/            loading + quality checks, signal, events + crash, feature_calc,
|   |   |                        process (process_trip)
|   |   |-- services/scoring.py  driver score, scoreable trips, passenger stats, explanations
|   |   |-- classify.py          driver / unknown classification
|   |   |-- model.py             model plug-in point, FEATURE_ORDER, score/tier/multiplier
|   |   |-- window_model.py      window model loader and v1 scorer (MODEL_KIND=window)
|   |   |-- window_features_v2.py  v2 window features and scorer
|   |   `-- routers/
|   |       |-- ingestion.py     register, consent, trip start / chunks / end / status
|   |       |-- driver.py        /me summary, trips, trip detail, labelling, DELETE /me
|   |       |-- insurer.py       /insurer overview, drivers, driver detail
|   |       |-- incidents.py     /me/incidents create, confirm, list
|   |       |-- dashboard*.py    insurer dashboard pages (login, overview, drivers, trips, incidents,
|   |       |                    users, API keys); templates in app/templates/
|   |       `-- admin.py         reprocess a trip
|   |-- Dockerfile               API image (uv, non-root user)
|   |-- migrations/              Alembic env + versions/ (0001_initial)
|   |-- scripts/                 seed.py, simulate.py, export_openapi.py
|   |-- data/raw/                raw sensor chunks at runtime (gitignored)
|   |-- contract/openapi.json    exported OpenAPI contract (+ examples/)
|   |-- docker/initdb.sql        creates the drivescore_test database on first start
|   |-- docker/entrypoint.sh     API container start: alembic upgrade head, then uvicorn
|   |-- tests/                   pytest suite (Postgres)
|   |-- conftest.py              test DB safety check, migrations, per-test truncate
|   |-- pyproject.toml, uv.lock  dependencies and tool config (uv)
|   `-- .env.example             environment template
`-- mobile/                      Expo / React Native app
    |-- App.tsx                  onboarding, then tab bar: Home, Trips, Coach, Privacy
    |-- app.json                 permissions (location, motion, background)
    `-- src/
        |-- api/client.ts        typed API client (base URL from src/config.ts)
        |-- sensors/SensorManager.ts   accelerometer / gyroscope / GPS-speed subscriptions
        |-- sensors/TripDetector.ts    trip start/end by speed, chunking and upload
        |-- screens/             Onboarding, Home, Trips, Trip detail, Coach, Privacy
        |-- AppHeader.tsx        BT header with language button, on every screen
        |-- LanguagePicker.tsx   language picker, opened from the header
        |-- connectivity.ts      offline detection (drives the offline banner)
        |-- i18n/                en, zh-CN, zh-HK strings
        |-- reminders.ts         daily trip reminder (expo-notifications)
        |-- theme.ts, ui.tsx     BT StyleSheet theme and shared primitives
        `-- types.ts             sensor sample types
```

## 4. Tech stack

| Area | Choice | Version |
|---|---|---|
| Language | Python | >=3.12, <3.13 |
| Web framework | FastAPI (sync handlers) + uvicorn | 0.115.0, 0.31.0 |
| Validation / settings | Pydantic, pydantic-settings | 2.9.0, 2.5.0 |
| Database | PostgreSQL (Docker image `postgres:16`) | 16 |
| ORM / driver | SQLAlchemy (sync), psycopg 3 | 2.0.35, >=3.3.6 |
| Migrations | Alembic | 1.13.0 |
| Signal processing | numpy, pandas, scipy | 1.26.4, 2.2.3, 1.13.1 |
| Model | placeholder rules by default; optional pickle (`MODEL_PATH`) or the JSON window model (logistic regression v2, numpy/scipy serving) via `MODEL_KIND=window` | n/a |
| Mobile | Expo SDK ~51, React Native 0.74.0, React 18.2.0, TypeScript ~5.3.3 | see `mobile/package.json` |
| Mobile libs | expo-sensors ~13.0, expo-location ~17.0, async-storage 1.23.1, `StyleSheet` theme | |
| Package manager | uv (backend), npm (mobile) | |
| Lint / format / types | ruff, ty (ruff line length 100) | latest via `uv.lock` |
| Tests | pytest 8.4.2, httpx 0.27.0, pytest-asyncio 0.24.0 | |
| CI | GitHub Actions: ruff format --check, ruff check, ty check, pytest | Postgres 16 service |

## 5. Quick start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you)
- Docker (for PostgreSQL)
- Node.js and npm (mobile app only), and Expo Go or a simulator

### Option A: everything in Docker

Needs Docker only (no local Python).

```bash
# from the repo root; compose refuses to start `api` if SESSION_SECRET is unset
export SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")   # or put it in a root .env
docker compose up -d --build

# create the first dashboard user (prompts for a password), then sign in at /dashboard
docker compose exec api python scripts/manage_users.py create admin

# demo data (the script targets http://localhost:8000, valid inside the container)
docker compose exec api python scripts/seed.py
```

The `api` container waits for a healthy `db`, runs `alembic upgrade head` (see
`backend/docker/entrypoint.sh`), then serves on `0.0.0.0:8000`. Check it with
`curl localhost:8000/health` and `docker compose logs api`. Raw chunks live in the `rawdata`
volume (`/data/raw`); put a trained `model.pkl` in the `models` volume (`/models`), otherwise
the placeholder rules are used. Phones on the same network reach the API at
`http://<laptop LAN IP>:8000` (see [Mobile app](#mobile-app)).

### Option B: local dev (compose db + uv)

```bash
# 1. PostgreSQL only (from the repo root); also creates the drivescore_test database
docker compose up -d db

# 2. Dependencies and environment
cd backend
uv sync
cp .env.example .env
#   edit .env: set SESSION_SECRET (see table below)
#   (.env.example already points DATABASE_URL and TEST_DATABASE_URL at the compose PostgreSQL;
#   the app only works with PostgreSQL.)

# 3. Create the schema (the app does not create tables itself)
uv run alembic upgrade head

#    ...and the first dashboard user (prompts for a password)
uv run python scripts/manage_users.py create admin

# 4. Run the API
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000/docs for the interactive API docs (field descriptions and examples
come from `app/schemas.py`) and http://localhost:8000/health for a liveness check.

`docker/initdb.sql` only runs on an empty volume. If your Postgres volume already existed,
create the test database with
`docker compose exec db createdb -U drivescore drivescore_test` (or `docker compose down -v`
to reset everything).

#### Environment variables (`backend/app/config.py`)

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_SECRET` | none, **required** | Signs the dashboard session cookie. The app will not start without it. |
| `SESSION_HTTPS_ONLY` | `false` | Set `true` behind HTTPS so the session cookie is `Secure`. |
| `DATABASE_URL` | `postgresql+psycopg://drivescore:drivescore@localhost:5432/drivescore` | SQLAlchemy URL of the main database. |
| `TEST_DATABASE_URL` | unset | Database used by pytest; its name must end in `_test`. |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:19006` | Comma-separated allowed origins. |
| `DATA_DIR` | `./data/raw` | Where raw gzip chunks are written (`<DATA_DIR>/<trip_id>/<seq>.json.gz`). |
| `MODEL_PATH` | `./models/model.pkl` | Optional pickled model, loaded once at startup. |
| `MODEL_KIND` | `placeholder` | `placeholder` (rules, or `MODEL_PATH` pickle if present) or `window` (250-sample window model, section 8.4). |
| `WINDOW_MODEL_PATH` | `../model/retrained_model/model.json` (compose: `/models/window/model.json`) | `model.json` of the window model; with `MODEL_KIND=window` the API refuses to start if it is missing or invalid. |
| `TRIP_MIN_DISTANCE_KM` | `1.0` | Quality check: minimum trip distance. |
| `TRIP_MAX_GPS_GAP_S` | `30.0` | Quality check: maximum gap between GPS speed samples. |
| `TRIP_MIN_DURATION_S` | `60.0` | Quality check: minimum trip duration. |

### Insurer dashboard

Staff-only HTML dashboard at `http://localhost:8000/dashboard` (Jinja2 + HTMX + Alpine.js + UnoCSS,
no JSON API calls). Staff users are rows in the Postgres `users` table (passwords stored as scrypt
hashes); session cookie (8 h, SameSite=Lax) plus per-session CSRF token. Not part of the OpenAPI
contract.

```bash
# from backend/: create the first user (prompts for a password; --generate makes a random one)
uv run python scripts/manage_users.py create admin
uv run python scripts/manage_users.py set-password admin
# in docker compose: docker compose exec api python scripts/manage_users.py create admin
#
# the only value kept in .env (docker compose reads it from the repo-root .env):
#   SESSION_SECRET=<long random string, e.g. python -c "import secrets; print(secrets.token_urlsafe(48))">
```

Once signed in, the **Users** screen adds users, changes passwords and deactivates users (a
deactivated user's session ends at once; the last active user cannot be deactivated). The
**API keys** screen generates and revokes insurer API keys. A new key is shown once and only its
salted hash is stored. Rebuild the stylesheet after editing templates or `backend/uno.config.ts` (Node needed only for
this; the generated `app/static/css/uno.css` is committed):

```bash
npm --prefix backend install   # once
npm --prefix backend run css
```

Tokens and components: `docs/design-system.md`. htmx, Alpine and Chart.js are vendored under
`backend/app/static/vendor/` (versions in its README).

**Overview page** (`/dashboard`): stat tiles (total drivers, scoreable trips in the last 90 days,
average premium multiplier), a Chart.js bar chart of scored trips per tier A-E (zero-filled) with
an equivalent data table, and an empty state when there are no scored trips. The stats block is the
partial `/dashboard/partials/overview`, refreshed every 30 s by HTMX. It uses the same service
(`app/services/insurer.py`) as `GET /v1/insurer/overview`.

**Drivers list** (`/dashboard/drivers`): table of drivers with score, tier badge, premium multiplier,
trips and distance over 90 days. Query params: `tier` (A-E, default all), `sort`
(`score_desc` default, `score_asc`, `multiplier_desc`, `multiplier_asc`), `page` (20 per page);
unknown values fall back to the defaults. Filter, sort and page changes swap the partial
`/dashboard/partials/drivers` via HTMX and update the address bar (`HX-Push-Url`); the page also works
without JavaScript. Score and Multiplier headers are sortable (`aria-sort`).

**Driver detail** (`/dashboard/drivers/{driver_id}`): score, tier, multiplier and trend, raw event counts
for 90 days, passenger share with a "Flagged for review" pill, label source breakdown, model version and
the 20 most recent trips (start time in Hong Kong time). Unknown ids show a dashboard-styled 404. Both
pages use `app/services/insurer.py`, the same code as `GET /v1/insurer/drivers[/{id}]`. Trip rows link to
`/dashboard/trips/{trip_id}`.

**Trip detail** (`/dashboard/trips/{trip_id}`): header (driver link, Hong Kong times, status, type, distance,
duration), score, tier and confidence, the explanation text, the events table (time, type, peak g) and any
crash incidents (time, peak g, confirmation). There is no map and no location anywhere. Unknown ids show a
dashboard-styled 404.

**Incidents** (`/dashboard/incidents`): crash incidents newest first (20 per page) with Hong Kong time, driver
and trip links, peak g and a confirmation pill (Help needed, No response, OK, Unconfirmed; help-needed rows are
highlighted). Query params: `status` (`help_needed`, `no_response`, `ok`, `unconfirmed`, default all) and
`page`; unknown values fall back to the defaults. Filter and page changes swap the partial
`/dashboard/partials/incidents` via HTMX with `HX-Push-Url`, and the page works without JavaScript. Uses
`app/services/incidents.py`.

### Demo data

With the API running (Option A: seed with `docker compose exec api python scripts/seed.py`;
Option B: in another terminal from `backend/`):

```bash
# Seed 30 drivers (12 calm, 10 moderate, 8 aggressive), 3 to 8 synthetic trips each.
# Takes a few minutes because every trip uploads real chunks and is processed.
uv run python scripts/seed.py

# Or generate trips yourself. Without --api-key it registers a new driver and gives consent.
uv run python scripts/simulate.py --type calm --count 2
uv run python scripts/simulate.py --type aggressive --count 3 --api-key <driver api key>
```

Both scripts (both included in the API image) talk to `http://localhost:8000/v1` (hardcoded `BASE_URL`). Simulated chunks set
`car_connected=true`, so trips classify as driver trips and are scored immediately. The
synthetic profiles are `calm`, `moderate` and `aggressive`.

Try the reports (generate a key on the dashboard's **API keys** screen first):

```bash
export INSURER_KEY=dsk_...   # the key shown once after "Generate key"
curl -H "X-API-Key: $INSURER_KEY" http://localhost:8000/v1/insurer/overview
curl -H "X-API-Key: $INSURER_KEY" "http://localhost:8000/v1/insurer/drivers?sort=score_asc"
```

### Export the API contract

```bash
cd backend && uv run python scripts/export_openapi.py   # writes contract/openapi.json
```

### Mobile app

The app reads GPS only for speed (and its accuracy), about once per second while recording. The app sends only time, speed and accuracy (no coordinates); no location is collected, uploaded or stored (no routes, no maps).

```bash
cd mobile
npm install
npx expo start        # or: npm run ios / npm run android
npm run ts:check      # TypeScript check
```

On first launch the app shows an onboarding flow with a consent screen; it registers the driver
and records consent (version "1.0") only after the user agrees, then stores the id and API key in
AsyncStorage. After that a bottom tab bar offers:

- **Home**: score, tier, premium multiplier (shown as a saving), recording switch, recent trips.
- **Trips** and **Trip detail**: trip list with score explanations, plus the "Trips to confirm" list.
- **Coach**: driving tips.
- **Privacy**: what data is collected, daily trip reminder (local notification), and "delete all my
  data" with an inline confirm (`DELETE /v1/me`); deletion is blocked while recording.

The UI is available in English, Simplified Chinese (zh-CN) and Traditional Chinese (zh-HK). The
language picker is opened from the header button, which is on every screen including onboarding.
When the API cannot be reached the app shows an offline banner and keeps the last loaded data
visible. Trips rows show distance and duration.

Turn on the "I'm driving" switch before you drive. While it is on, every uploaded chunk carries
`car_connected: true`; while it is off, every chunk carries `car_connected: false`. The choice is
kept in AsyncStorage (`drivescore:driving_mode`) and is read at each chunk, so flipping it
mid-trip changes the share of "on" chunks from the next chunk. The backend needs more than 80%
"on" chunks to classify the trip as `driver`; otherwise it is typically `unknown` and goes to labelling.

#### Browser preview

`npm --prefix mobile run web` (or `npx expo start --web --port 19006`) previews the app in a
browser. Use port 19006 because the API CORS default allows it. Sensors do not work on web.

#### Styling (StyleSheet theme)

The app uses plain React Native `StyleSheet` with the BT theme in `mobile/src/theme.ts` (colors,
radii, spacing, type; see `docs/design-system.md` section 8). Shared primitives (cards, buttons)
are in `mobile/src/ui.tsx`. No hard-coded colors or sizes in screens.

#### Confirm trips

Trips the backend cannot classify (`needs_confirmation`) appear in a "Trips to confirm" list
with their time and distance. Tap "I was driving" or "I was a passenger"
(`POST /v1/me/trips/{trip_id}/label`). The app shows "Added to your score" or "Removed from
your score", drops the row and refreshes the score; a driver label reprocesses the trip in the
background, so the score and list refresh again after a few seconds. The list loads on start and
after each trip ends.

#### Point the app at the backend

The base URL comes from `EXPO_PUBLIC_API_BASE_URL` (default `http://localhost:8000`, which
only works in the iOS simulator and web). `/v1` is appended automatically; do not include it.

```bash
ipconfig getifaddr en0     # macOS: your laptop LAN IP
EXPO_PUBLIC_API_BASE_URL=http://192.168.x.y:8000 npx expo start
```

- The phone and the laptop must be on the same Wi-Fi network.
- The API must listen on all interfaces: compose `api` already does (`0.0.0.0:8000`); for
  local uvicorn use `--host 0.0.0.0`.
- Android emulator: use `http://10.0.2.2:8000`.
- If setup fails, the error alert shows the URL the app tried.

## 6. API overview

All routes are under `/v1` except `/health`. Interactive docs at `/docs`; machine-readable
contract in `backend/contract/openapi.json`.

### Authentication model

- Everything except register and `/health` needs an `X-API-Key` header.
- **Driver key.** `POST /v1/drivers/register` returns `driver_id` and a random `api_key`
  once. Only a salted SHA-256 hash is stored. POC identity rule: **one device = one driver**;
  each install registers once and keeps its key. There is no login, logout or separate
  device model.
- **Salt.** The salt for those hashes is generated by the server on first use and stored in the
  `app_settings` table (not in `.env`). Changing it would invalidate every driver key, so there
  is no screen to rotate it.
- **Insurer keys.** Rows in the `api_keys` table, generated and revoked on the dashboard's API
  keys screen (shown once, stored as a salted hash, `last_used_at` tracked). Any unrevoked key
  is accepted on the insurer and reprocess endpoints.
- Every `/v1/me/...` and trip route is scoped to the authenticated driver (other drivers' trip
  ids return 404).

### Endpoints

| Group | Method | Path | Auth | Purpose |
|---|---|---|---|---|
| Health | GET | `/health` | none | Liveness check |
| Ingestion | POST | `/v1/drivers/register` | none | Create a driver; returns `driver_id`, `api_key` (optional emergency contact in body) |
| Ingestion | POST | `/v1/consent` | driver | Record PDPO consent (`version`) |
| Ingestion | POST | `/v1/trips/start` | driver | Open a trip, returns `trip_id`; 403 if no consent |
| Ingestion | POST | `/v1/trips/{trip_id}/chunks` | driver | Upload one IMU + GPS speed chunk (`seq`, `imu[]`, `speed_samples[]` = GPS speed samples, `car_connected`); idempotent per `seq`; only while status is `uploading` |
| Ingestion | POST | `/v1/trips/{trip_id}/end` | driver | Close the trip and start background processing |
| Ingestion | GET | `/v1/trips/{trip_id}/status` | driver | `uploading`, `processing`, `done` or `failed` (+ `failure_reason`) |
| Driver reports | GET | `/v1/me/summary` | driver | 90-day score, confidence, tier, premium multiplier, trend, trip count, distance |
| Driver reports | GET | `/v1/me/trips` | driver | Trip list (`limit` 1..200, `offset`), with type, score, `needs_confirmation` |
| Driver reports | GET | `/v1/me/trips/{trip_id}` | driver | Trip detail: score, events, text explanation (no location data) |
| Driver reports | POST | `/v1/me/trips/{trip_id}/label` | driver | Label a trip `driver` or `passenger` (user label wins over auto-classification) |
| Incidents | POST | `/v1/me/incidents` | driver | Create an incident (crash) from the client |
| Incidents | POST | `/v1/me/incidents/{incident_id}/confirm` | driver | Confirm as `ok`, `help_needed` or `no_response` |
| Incidents | GET | `/v1/me/incidents` | driver | Latest 50 incidents |
| Insurer | GET | `/v1/insurer/overview` | insurer | Total drivers, 90-day trip count, tier distribution, average multiplier |
| Insurer | GET | `/v1/insurer/drivers` | insurer | All drivers; filter `tier`, sort `score_desc` (default), `score_asc`, `multiplier_desc`, `multiplier_asc` |
| Insurer | GET | `/v1/insurer/drivers/{driver_id}` | insurer | Driver detail: event counts, last 20 trips, model version, passenger share, `flagged_for_review` |
| Admin | POST | `/v1/trips/{trip_id}/reprocess` | insurer | Delete events, features, score, incidents of a trip and re-run the pipeline |
| Admin | DELETE | `/v1/me` | driver | Delete all of the driver's data and raw files (PDPO erasure) |

All datetimes in responses are timezone-aware UTC and end in `Z` (for example
`2026-01-02T03:04:05Z`). Naive datetimes sent by a client are read as UTC.

Errors use FastAPI's `{"detail": "..."}` shape. 401 wrong key (422 if the `X-API-Key` header is
missing), 403 consent required, 404 unknown or foreign trip, 400 wrong trip state (for
example uploading to a trip that is no longer `uploading`).

Chunk payload (`t` is epoch milliseconds; accelerometer in g, gyroscope in rad/s):

```json
{
  "seq": 0,
  "imu": [{"t": 1760000000000, "ax": 0.0, "ay": 0.0, "az": 1.0, "gx": 0.0, "gy": 0.0, "gz": 0.0}],
  "speed_samples": [{"t": 1760000000000, "speed": 8.3, "accuracy": 5.0}],
  "car_connected": true
}
```

The `speed_samples` array holds GPS speed samples only (time, speed, accuracy; no coordinates). A sample with
`lat`, `lon` or `lng` (or any unknown field), and an incident with coordinates, is rejected
with 422.

Raw chunk files written before the rename store the samples under `gps`; the pipeline and
`scripts/scrub_coordinates.py` read both keys. New files use `speed_samples`.

## 7. Processing pipeline

Entry point: `process_trip(trip_id)` in `backend/app/pipeline/process.py`, run as a FastAPI
`BackgroundTasks` job after `/end`. Fixed thresholds live next to the code as named constants
or in the `CONFIG` dict in `app/classify.py`.

### 7.1 Order of operations

1. Load all chunk files; drop duplicate timestamps; sort; compute the Bluetooth ratio.
2. Classify the trip (unless the user already labelled it). Classification runs **before**
   the quality check.
3. Transit and user-labelled passenger trips are stored as `done` and not scored.
4. Quality check, signal processing, events, crash detection, features, model, score.

### 7.2 Quality checks

A failed check sets the trip to `failed` with a readable `failure_reason`.

| Check | Threshold |
|---|---|
| IMU samples | at least 100 |
| GPS speed samples | at least 10 |
| Largest GPS speed gap | at most `TRIP_MAX_GPS_GAP_S` (30 s) |
| Duration (IMU span) | at least `TRIP_MIN_DURATION_S` (60 s) |
| Distance (integrated GPS speed) | at least `TRIP_MIN_DISTANCE_KM` (1.0 km) |

### 7.3 Signal processing

1. **Resample** IMU to 50 Hz with linear interpolation.
2. **Gravity removal** (orientation-free): subtract a centred 10 s rolling median from each of
   the six raw axes, so the phone can be mounted in any pose for this step.
3. **Car frame** (all channels in g):
   - `accel_forward` = dv/dt of GPS speed over a 2 s window, interpolated onto the IMU
     timeline, divided by 9.81
   - `accel_lateral` = gravity-free gyro `gz` (yaw rate, rad/s) x GPS speed / 9.81
   - `accel_vertical` = gravity-free `az`
4. **Low-pass**: 4th-order zero-phase Butterworth, 5 Hz cutoff, on the three car-frame
   channels to remove road vibration.

### 7.4 Events

Each event stores type, time and peak value in g (where relevant). No position is stored.
For the g-based events, one event is emitted per contiguous run above the threshold, at its
peak.

| Event type | Rule |
|---|---|
| `harsh_brake` | `accel_forward` < -0.4 g |
| `harsh_accel` | `accel_forward` > 0.3 g |
| `sharp_corner` | abs(`accel_lateral`) > 0.35 g |
| `speeding` | nearest GPS speed > 13.9 m/s (50 km/h, fixed HK urban default `SPEEDING_THRESHOLD_MS`) for a run longer than 10 s; one event per run, at the run start |

### 7.5 Crash detection

Creates an `incidents` row (`type="crash"`, `confirmed` null) with a small sensor snapshot
summary when all of the following hold for a group of samples:

1. Acceleration magnitude of the gravity-free raw axes exceeds `CRASH_PEAK_G` = 4.0 g
   (peaks within 1 s are grouped into one candidate).
2. GPS speed falls to `CRASH_STOP_SPEED_MS` = 1.0 m/s or less within
   `CRASH_STOP_WINDOW_S` = 5 s after the peak.
3. The phone then stays still: acceleration magnitude standard deviation at most 0.5 over
   the 35 s following the peak (5 s stop window + 30 s still, `CRASH_STILL_DURATION_S` = 30 s;
   at least 10 IMU samples).

The driver can then confirm the incident (`ok`, `help_needed`, `no_response`) through
`/v1/me/incidents/{id}/confirm`.

### 7.6 Trip classification (`app/classify.py`)

Evaluated in this order; the first match wins. Thresholds are in `CONFIG`.

| Order | Rule | Result |
|---|---|---|
| 1 | Fraction of chunks with `car_connected=true` is above 0.80 | `driver`, `label_source=bluetooth` |
| 2 | Otherwise | `unknown`, with `driver_likelihood` |

`driver_likelihood` = 0.3 base, plus 0.5 if the mean variance of gyro x/y/z is below 0.01
(a stable, probably mounted phone), capped at 1.0. It is informational; it does not decide
scoring.

Transit is never assigned automatically; the user confirms a transit trip. `transit` is a valid
label (`transit_line` is a legacy column, always NULL).

Users can override any label with `POST /v1/me/trips/{id}/label` (`driver` or `passenger`).
User labels are never overwritten by reprocessing. Labelling an unscored finished trip as
`driver` re-runs the pipeline to produce a score.

### 7.7 Features

Stored per trip in `trip_features.features` (JSON). The model sees only the ten values in
`FEATURE_ORDER` (`app/model.py`), in this exact order:

| # | Feature | Definition |
|---|---|---|
| 1 | `distance_km` | Integral of GPS speed over time (no coordinates) |
| 2 | `duration_min` | IMU time span in minutes |
| 3 | `night_driving_share` | Share of IMU samples with Hong Kong hour 23 to 05 (23:00 to 05:59) |
| 4 | `harsh_brake_per_100km` | Event count / max(distance, 0.1 km) x 100 |
| 5 | `harsh_accel_per_100km` | same |
| 6 | `sharp_corner_per_100km` | same |
| 7 | `speeding_per_100km` | same (speeding runs) |
| 8 | `mean_speed_ms` | Mean GPS speed (missing speed = 0) |
| 9 | `max_speed_ms` | Max GPS speed |
| 10 | `speeding_time_share` | Share of GPS speed samples above 13.9 m/s |

The stored JSON also holds `events_per_100km` (dict), which is used by the API but is not a model input. The pipeline validates the
computed features against the `TripFeatures` model in `app/features.py` before storing them.

## 8. Scoring

### 8.1 From confidence to premium

The model returns `confidence` in 0..1 = probability that the driver is risky.

`score = round(100 * (1 - confidence))` (100 = safest), then:

| Tier | Score | Premium multiplier |
|---|---|---|
| A | 90 to 100 | 0.80 |
| B | 75 to 89 | 0.90 |
| C | 60 to 74 | 1.00 |
| D | 40 to 59 | 1.15 |
| E | 0 to 39 | 1.30 |

### 8.2 Driver score (what the premium uses)

The premium uses the driver-level score, never a single trip:

- Window: trips created in the last 90 days with status `done` and a stored score.
- Weighting: average of trip scores weighted by each trip's `distance_km` (truncated to an
  integer); tier and multiplier come from that average.
- No eligible trips (or zero total distance): default score 60, tier C, multiplier 1.00.
- `trend`: compares the average trip score of the older half with the newer half; a
  difference over 2 points gives `improving` or `worsening`, otherwise `stable`.
- Insurer detail also reports `passenger_share` (share of transit and passenger trips) and
  sets `flagged_for_review` above 40%.

### 8.3 Which trips count

| Trip type | Counts toward the score? |
|---|---|
| `driver` (Bluetooth or user label) | Yes |
| `unknown`, labelled `driver` by the user | Yes |
| `unknown`, not labelled | No. It is still scored and shown in the app with `needs_confirmation=true`; if it stays unlabelled for 7 days (`UNCONFIRMED_EXPIRY_DAYS`) it stops asking and never counts |
| `transit`, `passenger` | No (saved, never scored) |

### 8.4 Model: placeholder and plug-in contract

`app/model.py` has two modes:

- **Placeholder (default).** `model_version = "placeholder"`. Weighted sum of capped event
  rates: `0.3 * min(brake/10, 1) + 0.3 * min(accel/10, 1) + 0.2 * min(corner/10, 1) +
  0.2 * min(speeding_time_share/0.2, 1)`.
- **Real model.** If the file at `MODEL_PATH` (default `backend/models/model.pkl`, gitignored)
  exists at startup, it is unpickled once and used with `model_version = "pkl-model"`.

Contract for the model team: `TripFeatures` in `app/features.py` (field names, units,
descriptions) and `FEATURE_ORDER` in `app/model.py` (the order of the model input vector).

1. Train on the 10 features in `FEATURE_ORDER`, in that order. Do not reorder or rename them;
   changing `FEATURE_ORDER` or a `TripFeatures` field is a coordinated change.
2. The pickled object must expose `predict_proba(X)` (the probability of column 1, "risky",
   is used) or `predict(X)` returning a value in 0..1. `X` is a list with one row of 10 floats.
3. It must be loadable in the backend environment (numpy 1.26.4, pandas 2.2.3, scipy 1.13.1;
   add scikit-learn to `pyproject.toml` if your pickle needs it).
4. Invalid output (outside 0..1) or an exception raises `ModelPredictionError` and the trip
   is marked `failed`; a pickle that fails to load stops the API at startup. This is
   intentional: fail loudly rather than score with a broken model.
5. The model is loaded only at startup. Restart the API after replacing the file, then use
   `POST /v1/trips/{id}/reprocess` to rescore old trips.

Only unpickle files you trust; pickle can execute code.

#### Window model (`MODEL_KIND=window`)

Optional second model: logistic regression over accelerometer window features, pure JSON, no
pickle. The default is v2 (`model/retrained_model/`, `window-logreg-v2`, feature set
`horizontal-mag-v2`); v1 (`model/remade_model/`, `window-logreg-v1`) is kept for reference. The
loader picks the scorer from the model file (`feature_set` present = v2) and fails fast on an
invalid or mismatched file. Default is `MODEL_KIND=placeholder`, which leaves scoring exactly as
above. To enable: set `MODEL_KIND=window` (docker compose mounts `./model/retrained_model`
read-only at `/models/window`; with `uv run` the default `WINDOW_MODEL_PATH` points at the repo
folder) and restart the API. To use v1, point `WINDOW_MODEL_PATH` at
`model/remade_model/model.json`.

- Input: the trip's accelerometer after the 50 Hz resample (units g, gravity included; the
  model removes gravity itself, per window). It does not use the car-frame channels. v2 projects
  each window onto two horizontal axes (gravity = window mean), so any phone orientation works.
- The grid is cut into non-overlapping 250-sample (5 s) windows; the short tail is dropped.
- Trip confidence = share of windows with `risk_score` > 0.5. Higher confidence is riskier, so
  `score = round(100 * (1 - share))` as before; tier and multiplier follow unchanged.
- `trip_scores.model_version` is the `version` in the model file (`window-logreg-v2` by default).
  A trip with no scorable window falls back to the placeholder result and is recorded as
  `placeholder-window-fallback`.
- Caveat: v1 saturated on real phone windows (`mag_min` scaler scale about 5.7e-18); v2 fixes
  that (floored scales, `z_clip`, orientation-robust features). Labels are still synthetic
  injected pulses, there is no validation set of phone data, and the 0.5 threshold sits at an
  artificial 50% prevalence. Treat `risk_score` as a demo signal, not a verified risk measure.
- External training/evaluation data is kept outside git under `model/data/`; see `model/data/external/*/SOURCE.md` on the data machine for sources and licences.
- Code: `app/window_model.py` (v1, port of `serve.py`, and loader) and
  `app/window_features_v2.py` (v2, port of `model/retrain/features_v2.py`); v2 parity is tested
  against `features_v2.score_window`, v1 against `serve.py`.

## 9. Data model

PostgreSQL, 8 tables, managed by Alembic. Ids are strings such as `drv-<12 hex>` and
`trp-<12 hex>`. All timestamps are `timestamptz` (timezone-aware UTC) and DB sessions are
pinned to UTC.

Detailed reference (ER diagram, every table and column, allowed values, trip lifecycle):
[docs/data-model.md](docs/data-model.md).

Where things live:

- `backend/app/models.py`: database truth (typed `Mapped` tables, column comments).
- `backend/app/values.py`: allowed values as `Literal` types, shared by models and schemas.
- `backend/app/schemas.py`: API contract with field descriptions (browse it at `/docs`,
  exported to `backend/contract/openapi.json`).
- `backend/app/features.py`: `TripFeatures`, the contract with the model team.
- `backend/migrations/`: schema history.

| Table | Key columns | Relations / notes |
|---|---|---|
| `drivers` | `id`, `api_key_hash`, optional `emergency_contact_name`, `emergency_contact_phone`, `created_at` | Root of all driver data |
| `consents` | `id`, `driver_id`, `version`, `granted_at` | FK to `drivers`; required before `trips/start` |
| `trips` | `id`, `driver_id`, `status`, `started_at`, `ended_at`, `failure_reason`, `trip_type`, `label_source`, `driver_likelihood`, `transit_line`, `bluetooth_connected_ratio`, `created_at` | FK to `drivers`; `trip_type`: driver / passenger / transit / unknown; `label_source`: bluetooth / rules / user |
| `trip_chunks` | `id`, `trip_id`, `seq`, `file_path`, `received_at` | FK to `trips`; unique `(trip_id, seq)` gives idempotent uploads |
| `events` | `id`, `trip_id`, `type`, `time`, `peak_g` | FK to `trips`; harsh_brake, harsh_accel, sharp_corner, speeding |
| `trip_features` | `id`, `trip_id` (unique), `features` (JSON) | One row per trip; model input |
| `trip_scores` | `id`, `trip_id` (unique), `confidence`, `score`, `tier`, `model_version`, `created_at` | One row per scored trip |
| `incidents` | `id`, `driver_id`, `trip_id` (nullable), `type`, `time`, `peak_g`, `confirmed`, `sensor_snapshot` (JSON), `created_at` | FK to `drivers` and `trips`; `confirmed`: ok / help_needed / no_response |

Raw sensor data is not in the database; it lives as gzip JSON files on disk
(`DATA_DIR/<trip_id>/<seq>.json.gz`), referenced by `trip_chunks.file_path`.

### Schema changes (Alembic)

Revisions so far: `0001_initial` (the whole schema: 8 tables, timezone-aware timestamps,
column comments, foreign-key indexes; no location columns). The earlier history was squashed
into this single revision before anything was deployed.

**Upgrading an existing dev database.** Because the history was squashed, an old database
(with `alembic_version` 0001-0005) no longer matches. Reset it: `docker compose down -v`
(WARNING: this deletes all local dev database data), then `docker compose up -d db --wait` and `uv run alembic upgrade head` (or drop and
recreate the `drivescore` and `drivescore_test` databases). Then remove coordinates still
present in raw chunk files on disk, once:

```bash
cd backend
uv run python scripts/scrub_coordinates.py           # dry run: counts only
uv run python scripts/scrub_coordinates.py --apply   # rewrite files
# Docker: dry run first, then apply
docker compose exec api python scripts/scrub_coordinates.py
docker compose exec api python scripts/scrub_coordinates.py --apply
```

Never edit an applied revision; add a new one.

```bash
cd backend
# 1. edit app/models.py
uv run alembic revision --autogenerate -m "describe change"
# 2. review the generated file in migrations/versions/ (autogenerate is a draft)
uv run alembic upgrade head
```

`tests/test_migrations.py` fails if the models drift from the migrations, so CI catches a
forgotten revision.

## 10. Privacy (PDPO)

Designed with Hong Kong's Personal Data (Privacy) Ordinance in mind. This is a design
intent for the POC, not legal advice.

- **No coordinates, ever.** GPS is read for speed only (no coordinates).
  The server stores no locations (the schema has no location columns, and
  `scripts/scrub_coordinates.py` strips any left in old raw chunk files). Requests carrying `lat`, `lon`, `lng` or any
  unknown field are rejected with 422. There are no route maps and no event locations in
  the API or the dashboard. Transit trips are confirmed by the user, not matched to a line.

- **Consent first.** `POST /v1/trips/start` returns 403 until the driver has recorded
  consent (`POST /v1/consent`, versioned).
- **Right to erasure.** `DELETE /v1/me` removes the driver's raw chunk files and every row
  tied to them (events, features, scores, chunks, trips, incidents, consents, the driver).
- **No protected attributes.** The model features are driving behaviour only (distance,
  duration, night share, event rates, speeds). No name, age, gender or similar is collected
  or used. The only optional personal fields are emergency contact name and phone.
- **Pseudonymous insurer view.** Insurer endpoints identify drivers by `driver_id` only.
  They return tier, score, multiplier, event counts and trip summaries, not raw sensor data
  or names.
- **Data minimisation.** The driver API key is stored only as a salted hash.

## 11. Development

### Tests

Run from `backend/` against PostgreSQL (`docker compose up -d db` first):

```bash
uv run pytest -q
```

How the test setup protects you (`backend/conftest.py`):

- Tests use `TEST_DATABASE_URL` (default
  `postgresql+psycopg://drivescore:drivescore@localhost:5432/drivescore_test`).
- **Safety check:** the database name must end in `_test`, otherwise pytest aborts, because
  every test TRUNCATEs all tables.
- The schema is built once per session with `alembic upgrade head`, so migrations are
  exercised on every run, and tables are truncated before each test.
- `DATA_DIR` points to a fresh temporary directory, so tests never touch
  `backend/data/raw/`.
- Test insurer key and salt are set automatically.

The suite covers the API, pipeline, classification, crash detection, scoring integration,
model plug-in, config loading and migration drift.

### Lint, format, types

```bash
cd backend
uv run ruff format .
uv run ruff check --fix .
uv run ty check
npm --prefix ../mobile run ts:check     # mobile TypeScript
```

Limits: functions at most 100 lines, files at most 500 lines, line length 100. Ruff enforces
complexity (C901, max 8) and at most 5 arguments (PLR0913/PLR0917).

### CI (`.github/workflows/ci.yml`)

On every pull request and on pushes to `main`, with a PostgreSQL 16 service:
`uv sync --locked`, `ruff format --check`, `ruff check`, `ty check`, `pytest -q`.

### Workflow

- One roadmap line equals one small pull request, branched from `main`.
- PRs are merged manually on GitHub after CI is green.
- Conventional commit messages with the roadmap id, for example
  `feat(backend): PostgreSQL with Alembic baseline, tests on Postgres (A4)`.
- API changes: update `app/schemas.py`, regenerate `contract/openapi.json`, and update
  `mobile/src/api/client.ts` in the same PR.

## 12. Assumptions and known limitations

This is a hackathon POC. Be aware of the following.

**Architecture**
- Sync FastAPI with plain `def` handlers; processing runs in `BackgroundTasks` threads. No job
  queue: if the API restarts mid-processing, that trip stays `processing` until reprocessed.
- Raw chunks are stored on local disk, so only a single API instance works. Multiple
  instances need object storage (roadmap D5).
- The API container binds `0.0.0.0:8000` on purpose so phones on the LAN can reach it; there
  is no TLS or reverse proxy.
- Timestamps are timezone-aware UTC (`timestamptz`; API datetimes end in `Z`). Night driving
  converts to Asia/Hong_Kong.

**Signal processing**
- The lateral acceleration uses the gyro `gz` axis as yaw rate, so it assumes the phone's
  z axis is roughly vertical (phone lying flat or mounted upright in a typical pose). Other
  orientations will under- or over-report cornering. Gravity removal and the forward
  acceleration (from GPS speed) do not depend on orientation.
- Speeding uses a fixed 50 km/h, not the real road limit.
- Thresholds are hand-picked, not calibrated on real HK data.
- Crash detection uses fixed rules and always needs driver confirmation; there is no
  emergency notification flow yet.
- Transit is not detected automatically (no coordinates on the server); the user labels such trips.

**Mobile**
- `car_connected` comes from a manual "I'm driving" toggle (E2), not from the car's Bluetooth
  or audio. If the user forgets to turn it on, the trip is typically `unknown` and does not count toward the score. Native car-audio detection is planned (H2).
- The app has no screens for incidents, although the API supports them. Data deletion is on the
  Privacy screen.
- There is no offline queue; a failed final upload is retried 3 times and then dropped.
- Styling uses a `StyleSheet` theme; there is no dark mode.

**Security and data**
- Anyone can call `register`; there is no rate limiting. Dashboard login has no rate limiting or
  lockout yet (roadmap "Later" item).
- A `MODEL_PATH` model is loaded with `pickle`; only deploy trusted model files. The window
  model is plain JSON.
- Model: the window model's labels are synthetic injected pulses (VED), so it is a demo signal,
  not a validated risk measure. Its 0.5 threshold is calibrated at an artificial 50% prevalence.
  The only real-phone check is 69 Ferreira events from 2 drivers (evaluation only). See
  `model/TRAINING.md`.
- Insurer listing computes each driver's score in a loop, which will not scale beyond
  hundreds of drivers.

**Docs**
- `docs/handoff.md` and `contracts/` describe an earlier API design (`POST /trips`,
  `TripUpload`) and are stale. Use `backend/contract/openapi.json` and `/docs` (G1 will
  rewrite them).

## 13. Roadmap

Full plan and status in [docs/roadmap.md](docs/roadmap.md). One line is one small PR.

- Done: A1 uv tooling, A2 lint baseline, A3 CI, A4 PostgreSQL + Alembic, A5 Docker image and
  compose for the API, A6 Pydantic v2 and timezone-aware timestamps, A8 typed models, A10
  readable schema (allowed values, field docs, TripFeatures, data-model.md), A9 foreign-key
  indexes, A7 size limits (pipeline package, routers split), C2 pipeline error path fix,
  C1 incident trip ownership check, E1 configurable mobile API base URL.
- Done: A11 cleanups; mobile phase E (`car_connected` toggle, trip labelling) and phase M (BT
  redesign: StyleSheet theme, i18n, tabs, onboarding with consent, Privacy delete, daily
  reminders); privacy Option B (no coordinates) and `DELETE /v1/me`; insurer dashboard phase F
  (staff login, overview, drivers, trip detail, incidents; Jinja2 + HTMX + Alpine.js + UnoCSS).
- Done: follow-ups (chunk field renamed to `speed_samples`, trip `duration_min`, offline banner,
  mobile lockfile, `ty` clean), language picker in the header on every screen; phase ML (ML1 window
  model wired behind `MODEL_KIND`, v2 retrain with orientation-robust features, ML2 v2 served).
- Open: own labelled drives recorded with the BT app, threshold recalibration for real
  prevalence, real-device testing; dashboard hardening (login rate limiting, session revocation, CSP); docs (phase G):
  regenerate handoff and contracts from the OpenAPI export; native signals (phase H: car audio
  `car_connected`, OS activity recognition, dev build).

Backend-only details: [backend/README.md](backend/README.md).

## 14. Team

Roles only; add names and contacts here.

| Role | Owns |
|---|---|
| Data / Mobile | Sensor collection, trip detection, chunk upload, driver app screens |
| Model | Risk model behind `app/model.py` (`FEATURE_ORDER` contract), training data, calibration |
| UI | Insurer dashboard and driver app design |
| Backend | API, pipeline, classification, scoring rules, database, CI, contracts |

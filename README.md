# Proglintor — Backend (Django + PostgreSQL)

The central server for Proglintor:
- **Employee agents** ([Proglintor-Desktop](https://github.com/dhinakaranproglint/Proglintor-Desktop) agent) connect here with a one-time enrollment code, then send a heartbeat
  (live state, current app, running apps), daily app usage, installed apps and weekend-work answers.
- The **Proglintor Admin** desktop app signs in here to manage assets, see live status, review weekend alerts
  and edit the weekend policy.
- Data is stored in **PostgreSQL**. Django's built-in admin is at `/django-admin/`.

## Local development
Requires Python 3.10+ and PostgreSQL (`brew install postgresql`).
```bash
cd Proglintor-Backend
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
./scripts/run-dev.sh                 # private Postgres on :5433 + Django on http://127.0.0.1:8000
.venv/bin/python manage.py createsuperuser   # once: your admin login for the desktop app
.venv/bin/python manage.py test pulse        # backend tests (run against PostgreSQL)
```
`scripts/dev-db.sh` creates a private database cluster in `./.pgdata`, so it doesn't touch any other Postgres
on the machine. Settings are read from `.env` (see `.env.example`).

To let other computers connect, run `./scripts/run-dev.sh 0.0.0.0` and give them this computer's network
address, e.g. `192.168.1.20:8000`. Corporate security software (CrowdStrike, FortiClient, firewalls) on a laptop
often blocks incoming connections. In that case, run the backend on a server instead.

## Deploying on a server
```bash
cp .env.example .env        # set DJANGO_SECRET_KEY, POSTGRES_PASSWORD, DJANGO_ALLOWED_HOSTS
docker compose up -d
docker compose exec backend python manage.py createsuperuser
```
Agents and the admin app then use `http://<server>:8000`, or `https://pulse.proglint.com` behind an HTTPS proxy
(set `DJANGO_BEHIND_PROXY=1`).

## Moving data from the earlier desktop-only admin
```bash
.venv/bin/python manage.py import_desktop_admin "~/Library/Application Support/Proglintor Admin/pulse-admin.db"
```

## API
| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/api/agent/enroll` | agent (code) | link a laptop → token |
| POST | `/api/agent/heartbeat` | agent (Bearer token) | live state, current/running apps → policy |
| POST | `/api/agent/usage` | agent | whole days of per-app usage (replaced, never doubled) |
| POST | `/api/agent/installed` | agent | installed apps |
| POST | `/api/agent/alerts` | agent | weekend answers (idempotent by uid) |
| POST | `/api/admin/login` · `/logout` · GET `/me` | staff user | desktop app sign-in (Token) |
| GET | `/api/admin/dashboard` | staff | KPIs, live status, top apps, 7-day trend, recent alerts |
| GET/POST | `/api/admin/assets` | staff | list / create assets |
| GET/PATCH/DELETE | `/api/admin/assets/<id>` | staff | detail (usage, installed, alerts) / edit / delete |
| POST | `/api/admin/assets/<id>/reset-code` | staff | new enrollment code (disconnects the laptop) |
| GET · PATCH | `/api/admin/alerts` · `/api/admin/alerts/<uid>` | staff | list / review + note |
| GET/PUT | `/api/admin/policy` | staff | weekend policy |
| GET | `/api/admin/export.csv` | staff | usage export |

Only staff users (`is_staff`) can use the admin API. Agent tokens and enrollment codes are single-purpose: tokens
are stored as SHA-256 hashes, codes work once, and repeated wrong codes are rate-limited.

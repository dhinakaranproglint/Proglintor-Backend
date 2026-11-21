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

### Production checklist
- Set `PULSE_ENV=production`, `DJANGO_DEBUG=0`, a real `DJANGO_SECRET_KEY` and explicit `DJANGO_ALLOWED_HOSTS` (the server refuses to start otherwise).
- Serve over HTTPS and set `DJANGO_BEHIND_PROXY=1` (secure cookies, HTTPS redirect, HSTS) and `PULSE_TRUSTED_PROXIES` to the number of proxies in front.
- Use a cache shared by all workers (`CACHE_BACKEND=db` after `createcachetable`, which the Docker image runs, or `redis`). `manage.py check` warns if not.
- Install assistant: the AI chat sends the employee's message text (with emails, numbers and secrets redacted) to Google Gemini. Get privacy/legal
  sign-off first, use a paid Gemini tier (free-tier data may be used for training), or switch it off with `ASSISTANT_CHAT_ENABLED=0` /
  `ASSISTANT_WEB_LOOKUP_ENABLED=0`. The catalogue install flow does not use it.
- Back up PostgreSQL; the audit trail (`SoftwareEvent`) and application log (`pulse.software`) record every install decision.

## Orbit: the employee's work companion
Orbit greets each employee by the time of day when they open their laptop, then acts like a friendly senior colleague: it knows their
assigned **tasks**, **workload** (open hours against available hours), **app activity**, **achievements** and **appreciation**, and answers
questions about them. When someone does good work (finishes tasks early, works a weekend, keeps a focus streak, or tells Orbit about extra
effort), Orbit proposes appreciation; **only an admin can approve it**, choose a reward and credits, and the employee then gets a certificate.
- Facts (hours, counts, streaks, credits) are computed by code; the model only phrases and advises. Chat tools are read-only and closed over
  one employee. Installs and awards are never decided by the model.
- Employee API `/api/agent/orbit/*`; admin API `/api/admin/orbit/*` (tasks, recognition decisions, team workload, analyst chat).
- Meetings, commits and PRs come from `ORBIT_WORK_SOURCE` (live Azure DevOps + Microsoft Graph, demo data, or off). Live connectors are read-only
  and fail soft; they have not been verified against a real tenant yet.
- Privacy: the chat sends the employee's message (emails, numbers and secrets redacted) plus the data the tools return (task titles, hours,
  achievements) to Google Gemini. Get sign-off first, use a paid tier, or switch it off with `ORBIT_ENABLED=0` / `ASSISTANT_CHAT_ENABLED=0`.

### Mock data (development and demos)
```bash
.venv/bin/python manage.py seed_mock_data                 # 8 people, 30 days: app usage, tasks, appreciation, weekend work
.venv/bin/python manage.py seed_mock_data --asset PAP001  # mock TASKS and appreciation for a real laptop (its app usage is never faked)
.venv/bin/python manage.py seed_mock_data --remove        # removes only the mock people (ids MOCK001..)
```
History always ends today, and today is only as full as the working day so far, so re-running keeps it looking live. It is deterministic per
person and day, safe to re-run, and never fakes a real laptop's app usage. Only tasks and meetings may be demo data (`ORBIT_WORK_SOURCE=simulated`
gives demo meetings, labelled "demo data"); commits and pull requests are never simulated. Real Azure DevOps and Graph replace it later by setting their credentials.

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
| GET · POST | `/api/admin/software/items` | staff | software catalogue (pinned installers) |
| GET · PATCH · DELETE | `/api/admin/software/items/<slug>` | staff | edit / disable / remove (blocked while requests are open) |
| GET | `/api/admin/software/approvals` | staff | install requests waiting for a decision |
| POST | `/api/admin/software/requests/<id>/decision` | staff | `{decision: APPROVED\|REJECTED, comment, expectedVersion}` |
| GET | `/api/admin/software/requests[/<id>]` | staff | request history; detail includes the audit trail |
| GET | `/api/agent/software/catalogue` | agent | what this laptop may request (no URLs or digests) |
| POST | `/api/agent/software/preview` | agent | “what would happen?” (nothing recorded) |
| GET · POST | `/api/agent/software/requests` | agent | this laptop's requests · ask for an install (`idempotencyKey`) |
| POST | `/api/agent/software/requests/<id>/cancel` | agent | cancel while waiting for approval / queued / a suggestion |
| POST | `/api/agent/software/requests/<id>/confirm` | agent | “yes, ask IT to review this app” (software that isn't in the catalogue) |
| POST | `/api/admin/software/requests/<id>/approve-new` | staff | `{item, expectedVersion, licenceConfirmed}`: add the admin-checked build to the catalogue and queue the install |
| POST | `/api/agent/software/jobs/next` | agent | claim the next approved install (one at a time) |
| POST | `/api/agent/software/jobs/<id>/progress` · `/result` | agent | phase + bytes · success or reason code |
| GET | `/api/admin/export.csv` | staff | usage export |

**Install requests** are matched deterministically against the catalogue (no model decides what gets installed), the installer's URL and SHA-256 are copied
onto the request when it is made, and every step is written to an append-only audit trail. Limits and timeouts are env settings:
`SOFTWARE_MAX_OPEN_PER_ASSET` (5), `SOFTWARE_SUBMITS_PER_10MIN` (20), `SOFTWARE_APPROVAL_TTL_HOURS` (168), `SOFTWARE_QUEUE_TTL_HOURS` (24), `SOFTWARE_STALE_MINUTES` (15).
**Conversational assistant (optional, Google Gemini).** Set `GEMINI_API_KEY` (and optionally `GEMINI_MODEL`, `GEMINI_TIMEOUT_SECONDS`, `ASSIST_PER_10MIN`) in `.env`. The model only chats and suggests:
small talk gets a friendly answer, anything not about installing software gets a polite redirect to the IT Service Desk, and a misspelled or loosely described app gets "Did you mean *Slack*?".
It never installs anything: a suggestion must exactly match an approved entry for that laptop, the employee has to answer "yes", and the request then goes through the normal approval and install flow.
Only the typed message, a few recent chat lines and the approved software names are sent to the model (never names, e-mails, asset ids or hostnames), the key stays on the server, answers are cleaned (no links or markdown),
and each laptop is limited to `ASSIST_PER_10MIN` calls. Without a key, or if the service is slow or down, the chat falls back to its built-in replies (close misspellings still get "Did you mean…?").
**Software that isn't approved yet** (`pulse/proposals.py`): for a real app missing from the catalogue the model (with Google Search) proposes the vendor's official download page and a licence assessment; `PROPOSALS_PER_DAY` (10) limits these web lookups per laptop and answers are cached for an hour.
The server only pre-checks the link (public https, every redirect re-validated against private networks, first 2 KB read to classify it, host vs vendor) and never downloads or runs anything; signature and checksum are verified by the admin app at review time.
An employee-confirmed suggestion appears in Approvals as *New software*; it cannot be approved with the ordinary Approve button, only through `approve-new`, which creates the pinned catalogue entry and queues the install atomically (a wrong-platform build or a missing licence confirmation changes nothing).
Run `python manage.py migrate` after updating (adds the software tables and `Asset.arch`).

Only staff users (`is_staff`) can use the admin API. Agent tokens and enrollment codes are single-purpose: tokens
are stored as SHA-256 hashes, codes work once, and repeated wrong codes are rate-limited.

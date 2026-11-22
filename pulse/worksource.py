"""Where Orbit's meetings, commits and pull requests come from.

Three interchangeable sources behind one small interface:
  * Live: Azure DevOps (commits, pull requests; PAT) and Microsoft Graph (calendar; app-only client credentials). Read-only.
  * Simulated: deterministic demo data (seeded), for demos and development. The UI labels it "demo data".
  * Off: nothing; Orbit simply leaves those sections out rather than inventing anything.
Facts here are computed by code; the model only phrases them. Every source fails soft: an error becomes an empty section.
"""
import hashlib
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

log = logging.getLogger("pulse.orbit")

WORK_START, WORK_END = 9, 18


def _http_json(url, headers=None, data=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 (fixed https endpoints from settings)
        return json.load(res)


def ado_configured():
    return bool(settings.ADO_ORG_URL.startswith("https://") and settings.ADO_PAT and settings.ADO_PROJECT)


def graph_configured():
    return bool(settings.AZURE_TENANT_ID and settings.AZURE_CLIENT_ID and settings.AZURE_CLIENT_SECRET)


def mode():
    """'live' | 'simulated' | 'off' for the current configuration."""
    want = settings.ORBIT_WORK_SOURCE
    if want in ("live", "simulated", "off"):
        return want
    if ado_configured() or graph_configured():
        return "live"
    return "simulated" if settings.DEBUG else "off"


# ---- simulated ------------------------------------------------------------------------------------

def _rng(*parts):
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def _sim_meetings(key, day):
    if day.weekday() >= 5:
        return []
    r = _rng("meet", key, day)
    out, cursor = [], 9.5
    titles = ["Daily stand-up", "Sprint planning", "1:1", "Design review", "Architecture sync", "Customer call", "Retro"]
    for _ in range(r.randint(2, 4)):
        length = r.choice([0.5, 1.0])
        start = cursor + r.choice([0, 0.5, 1.0, 1.5])
        if start + length > 17.5:
            break
        s = datetime.combine(day, time(int(start), int((start % 1) * 60)))
        out.append({"title": r.choice(titles), "start": s.isoformat(), "end": (s + timedelta(hours=length)).isoformat(),
                    "hours": length, "online": r.random() < 0.6})
        cursor = start + length
    return out


# ---- live -----------------------------------------------------------------------------------------

def _graph_token():
    body = urllib.parse.urlencode({"client_id": settings.AZURE_CLIENT_ID, "client_secret": settings.AZURE_CLIENT_SECRET,
                                   "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}).encode()
    return _http_json(f"https://login.microsoftonline.com/{urllib.parse.quote(settings.AZURE_TENANT_ID)}/oauth2/v2.0/token",
                      {"content-type": "application/x-www-form-urlencoded"}, body)["access_token"]


def _live_meetings(email, start, end):
    token = _graph_token()
    q = urllib.parse.urlencode({"startDateTime": f"{start}T00:00:00Z", "endDateTime": f"{end}T23:59:59Z", "$top": "100",
                                "$select": "subject,start,end,isOnlineMeeting,isCancelled"})
    data = _http_json(f"https://graph.microsoft.com/v1.0/users/{urllib.parse.quote(email)}/calendarView?{q}",
                      {"authorization": f"Bearer {token}", "prefer": 'outlook.timezone="UTC"'})
    out = []
    for e in data.get("value", []):
        if e.get("isCancelled"):
            continue
        s = datetime.fromisoformat(e["start"]["dateTime"][:19])
        f = datetime.fromisoformat(e["end"]["dateTime"][:19])
        out.append({"title": str(e.get("subject") or "Meeting")[:120], "start": s.isoformat(), "end": f.isoformat(),
                    "hours": round((f - s).total_seconds() / 3600, 2), "online": bool(e.get("isOnlineMeeting"))})
    return out


def _ado_headers():
    return {"authorization": "Basic " + b64encode(f":{settings.ADO_PAT}".encode()).decode()}


def _live_ado(email, start, end):
    base = f"{settings.ADO_ORG_URL}/{urllib.parse.quote(settings.ADO_PROJECT)}/_apis/git"
    commits, prs = [], []
    repos = _http_json(f"{base}/repositories?api-version=7.1", _ado_headers()).get("value", [])
    for repo in repos[:25]:
        q = urllib.parse.urlencode({"searchCriteria.author": email, "searchCriteria.fromDate": f"{start}T00:00:00Z",
                                    "searchCriteria.toDate": f"{end}T23:59:59Z", "searchCriteria.$top": "200", "api-version": "7.1"})
        try:
            for c in _http_json(f"{base}/repositories/{repo['id']}/commits?{q}", _ado_headers()).get("value", []):
                commits.append({"sha": str(c.get("commitId", ""))[:7], "repo": repo["name"], "message": str(c.get("comment", ""))[:120].splitlines()[0] if c.get("comment") else "",
                                "day": str(c.get("author", {}).get("date", ""))[:10]})
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            continue
    q = urllib.parse.urlencode({"searchCriteria.status": "all", "searchCriteria.creatorId": "", "$top": "100", "api-version": "7.1"})
    try:
        for p in _http_json(f"{base}/pullrequests?{q}", _ado_headers()).get("value", []):
            if str(p.get("createdBy", {}).get("uniqueName", "")).lower() != email.lower():
                continue
            prs.append({"id": p.get("pullRequestId"), "repo": p.get("repository", {}).get("name", ""), "title": str(p.get("title", ""))[:120],
                        "state": {"active": "open", "completed": "merged"}.get(p.get("status"), "closed"), "review": "none"})
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    return commits, prs


# ---- public ---------------------------------------------------------------------------------------

def code_source():
    """'azure devops' (real), 'demo data' (derived from the employee's tasks, simulated mode only) or '' (nothing to show)."""
    if mode() == "live" and ado_configured():
        return "azure devops"
    return "demo data" if mode() == "simulated" else ""


def code_connected():
    return bool(code_source())


REPOS = ["payments-api", "web-app", "mobile-app", "data-pipeline", "auth-service"]
PREFIX = ["feat", "fix", "refactor", "test", "docs"]


def _sim_code(asset, start, end, today):
    """Demo commits and pull requests that follow the employee's real task activity: commits on the days a task was worked on, and a pull request for
    every completed task (merged) or well-advanced one (open). Same inputs, same output. Only ever used in simulated mode and labelled as demo data."""
    from .models import Task, TaskEvent
    commits, prs = [], []
    for t in Task.objects.filter(asset=asset):
        repo = _rng("repo", t.id).choice(REPOS)
        days = {timezone.localtime(e.at).date() for e in TaskEvent.objects.filter(task=t) if e.status != "todo" or e.percent}
        if t.completed_at:
            days.add(timezone.localtime(t.completed_at).date())
        for d in sorted(x for x in days if start <= x <= end):
            r = _rng("commit", t.id, d)
            for i in range(r.randint(1, 4)):
                sha = hashlib.sha1(f"{t.id}{d}{i}".encode()).hexdigest()[:7]
                commits.append({"sha": sha, "repo": repo, "message": f"{r.choice(PREFIX)}: {t.title[:90]}", "day": d.isoformat(), "task": t.title})
        if t.status == "done" and t.completed_at and start <= timezone.localtime(t.completed_at).date() <= end:
            prs.append({"id": 1000 + int(_rng("pr", t.id).random() * 8000), "repo": repo, "title": t.title[:120], "state": "merged",
                        "review": "approved", "day": timezone.localtime(t.completed_at).date().isoformat()})
        elif t.status == "in_progress" and t.percent >= 50:
            prs.append({"id": 1000 + int(_rng("pr", t.id).random() * 8000), "repo": repo, "title": t.title[:120], "state": "open",
                        "review": "pending", "day": end.isoformat()})
    # A working day with little or no task activity still has some commits: people commit between task updates. Deterministic, weekdays only.
    titles = [t.title for t in Task.objects.filter(asset=asset).order_by("-updated_at")[:6]] or ["Code review follow-ups", "Dependency updates", "Unit tests"]
    for d in _days(start, end):
        if d.weekday() >= 5 or d > today:
            continue
        have = sum(1 for c in commits if c["day"] == d.isoformat())
        r = _rng("base", asset.asset_id, d)
        want = r.randint(1, 3) if d == today else r.randint(3, 6)
        for i in range(max(0, want - have)):
            title = r.choice(titles)
            commits.append({"sha": hashlib.sha1(f"{asset.asset_id}{d}b{i}".encode()).hexdigest()[:7], "repo": r.choice(REPOS),
                            "message": f"{r.choice(PREFIX)}: {title[:90]}", "day": d.isoformat(), "task": title})
        if d < today and r.random() < 0.45 and not any(p["day"] == d.isoformat() for p in prs):
            prs.append({"id": 1000 + int(r.random() * 8000), "repo": r.choice(REPOS), "title": r.choice(titles)[:120], "state": "merged" if d < today - timedelta(days=1) else "open",
                        "review": "approved" if d < today - timedelta(days=1) else "pending", "day": d.isoformat()})
    commits.sort(key=lambda c: c["day"], reverse=True)
    prs.sort(key=lambda p: p["day"], reverse=True)
    return commits, prs


def meetings(asset, start, end):
    m = mode()
    try:
        if m == "simulated":
            return [x for d in _days(start, end) for x in _sim_meetings(asset.asset_id, d)]
        if m == "live" and graph_configured():
            return _live_meetings(asset.employee_email, start, end)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError) as err:
        log.warning("meetings source failed: %s", type(err).__name__)
    return []


def code_activity(asset, start, end, today):
    """-> {commits: [...], prs: [...]}"""
    # Real (Azure DevOps) when connected. In simulated mode only, demo commits/PRs follow the employee's task activity. Otherwise nothing.
    src = code_source()
    try:
        if src == "azure devops":
            commits, prs = _live_ado(asset.employee_email, start, end)
            return {"commits": commits, "prs": prs}
        if src == "demo data":
            commits, prs = _sim_code(asset, start, end, today)
            return {"commits": commits, "prs": prs}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError) as err:
        log.warning("code activity source failed: %s", type(err).__name__)
    return {"commits": [], "prs": []}


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

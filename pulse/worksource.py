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

def code_connected():
    """Is there a real source of commits / pull requests?"""
    return mode() == "live" and ado_configured()


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
    # Commits and pull requests are never simulated: they are either real (Azure DevOps) or absent. Only meetings and tasks may be demo data.
    m = mode()
    try:
        if m == "live" and ado_configured():
            commits, prs = _live_ado(asset.employee_email, start, end)
            return {"commits": commits, "prs": prs}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError) as err:
        log.warning("code activity source failed: %s", type(err).__name__)
    return {"commits": [], "prs": []}


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

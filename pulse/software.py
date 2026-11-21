"""Software catalogue, request evaluation and the install-request lifecycle.

Used by the employee-agent API (chat, job claim, progress) and the admin API (catalogue, approvals).
Everything here is deterministic: a request is matched against the catalogue with plain rules, never by a model,
so what an approver reviews is exactly what the laptop later installs (the spec is pinned onto the request).

Roles: an *employee* is an enrolled laptop (agent token) and may only act on its own requests;
an *admin* is a staff user and is the only one who can approve, reject or manage the catalogue.
"""
import difflib
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    REQUEST_ACTIVE, REQUEST_OPEN, Asset, InstalledApps, SoftwareEvent, SoftwareItem, SoftwareRequest,
)

log = logging.getLogger("pulse.software")

MAX_PROMPT = 500


class SoftwareError(Exception):
    """A problem the caller can show to the user as-is."""

    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class Answered(Exception):
    """The request was understood and answered, but there is nothing to track (a question, or nothing to do).
    Carries the evaluation result; no request row is created."""

    def __init__(self, result):
        super().__init__(result["explanation"])
        self.result = result


UNTRACKED = ("NEEDS_CLARIFICATION", "NEEDS_CONFIRMATION", "CHAT", "ALREADY_INSTALLED", "INCOMPATIBLE")


# ---- text matching -----------------------------------------------------------------------------

FILLER = {
    "please", "pls", "kindly", "can", "could", "would", "you", "i", "me", "my", "we", "want", "need", "like", "to",
    "a", "an", "the", "for", "on", "in", "into", "onto", "of", "and", "also", "it", "this", "that", "some", "with",
    "install", "installing", "installation", "installed", "get", "add", "setup", "set", "up", "download", "provision",
    "deploy", "update", "upgrade", "reinstall", "app", "application", "software", "program", "latest", "new", "version", "laptop", "macbook", "computer",
    "device", "machine", "pc", "thanks", "thank", "now", "help",
}


def tokens(text):
    out = []
    for t in re.sub(r"[^a-z0-9+#.]+", " ", str(text or "").lower()).split():
        t = t.strip(".")
        if t:
            out.append(t)
    return out


def compact(text):
    return re.sub(r"[^a-z0-9+#]", "", str(text or "").lower())


def clean_prompt(raw):
    """Collapses whitespace and rejects empty, over-long or control-character input."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        raise SoftwareError("Tell me which software you'd like to install.", code="empty")
    if len(text) > MAX_PROMPT:
        raise SoftwareError(f"Please keep requests under {MAX_PROMPT} characters.", code="too_long")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise SoftwareError("That request contains characters I can't process. Please use plain text.", code="control_chars")
    return text


def score(query_tokens, query_compact, item):
    labels = [item.name, item.slug.replace("-", " ")] + [str(a) for a in (item.aliases or [])]
    best = 0
    qset = set(query_tokens)
    for label in labels:
        ct, cc = tokens(label), compact(label)
        if not ct:
            continue
        cset = set(ct)
        if query_tokens == ct:
            s = 100
        elif query_compact and query_compact == cc:
            s = 96
        elif cset <= qset:  # the whole product name appears in the request
            s = 85 - min(10, 2 * (len(query_tokens) - len(ct)))
        elif qset <= cset:  # the request is part of a product name ("code" -> "Visual Studio Code")
            s = 75 - min(10, 2 * (len(ct) - len(query_tokens)))
        elif len(cc) >= 4 and cc in query_compact:
            s = 70
        elif len(query_compact) >= 4 and query_compact in cc:
            s = 62
        else:
            s = 0
        best = max(best, s)
    return best


def version_key(value):
    nums = [int(n) for n in re.findall(r"\d+", str(value or ""))[:6]]
    return tuple(nums + [0] * (6 - len(nums)))


def compatible(item, asset):
    if item.platform != (asset.platform or ""):
        return False
    return item.arch in ("any", "") or not asset.arch or item.arch == asset.arch


def installed_version(asset, item):
    """Version of this software already on the laptop (from its last installed-apps report), or None.
    The report is only a snapshot: apps get uninstalled. A snapshot older than SOFTWARE_INVENTORY_MAX_AGE_MINUTES is not trusted
    (the agent re-reports a fresh one every time its employee asks for something); the laptop also re-checks before installing."""
    row = InstalledApps.objects.filter(asset=asset).first()
    if not row or row.updated_at < timezone.now() - timedelta(minutes=settings.SOFTWARE_INVENTORY_MAX_AGE_MINUTES):
        return None
    wanted_id = (item.app_id or "").lower()
    wanted_name = compact(item.name)
    wanted_stem = compact(re.sub(r"\.app$", "", item.app_name or "", flags=re.I))  # a .pkg's app: "zoom.us.app" -> "zoomus"
    for app in row.apps or []:
        if not isinstance(app, dict):
            continue
        have = compact(app.get("name"))
        if (wanted_id and str(app.get("id", "")).lower() == wanted_id) or have == wanted_name or (wanted_stem and have == wanted_stem):
            return str(app.get("version") or "0")
    return None


# ---- evaluation --------------------------------------------------------------------------------

def outcome(status, reason="", explanation="", item=None, candidates=None, upgrade_from=None):
    return {"status": status, "reason": reason, "explanation": explanation, "item": item,
            "candidates": candidates or [], "upgradeFrom": upgrade_from}


REINSTALL_RE = re.compile(r"\b(re-?install|repair|again)\b", re.I)  # "reinstall git", "install git again", "repair git"


def evaluate(asset, prompt, reinstall=False):
    """Decides what would happen to this request. Has no side effects.
    `reinstall` (or the words "reinstall" / "install again" in the request) skips the "already installed" shortcut."""
    reinstall = reinstall or bool(REINSTALL_RE.search(str(prompt or "")))
    q_tokens = [t for t in tokens(prompt) if t not in FILLER]
    q_compact = compact("".join(q_tokens))
    if not q_tokens:
        return outcome("NEEDS_CLARIFICATION", "EMPTY_QUERY", "Which software would you like me to install? For example “Install Visual Studio Code”.")

    items = list(SoftwareItem.objects.filter(enabled=True))
    by_name = {}
    for it in items:
        s = score(q_tokens, q_compact, it)
        if s:
            by_name.setdefault(compact(it.name), []).append((s, it))
    ranked = sorted(((max(s for s, _ in rows), key, rows) for key, rows in by_name.items()), key=lambda r: -r[0])

    if not ranked or ranked[0][0] < 60:
        names = {compact(i.name): i.name for i in items if compatible(i, asset)}
        close = difflib.get_close_matches(q_compact, list(names), n=3, cutoff=0.6)
        hint = f" Did you mean {', '.join(names[c] for c in close)}?" if close else ""
        return outcome(
            "NOT_FOUND", "NOT_IN_CATALOGUE",
            "I couldn't find that in your company's approved software list." + hint +
            " If it isn't listed, ask your IT admin to add it.",
            candidates=[names[c] for c in close],
        )

    top_score, _, rows = ranked[0]
    if len(ranked) > 1 and top_score < 96 and ranked[1][0] >= top_score - 8:
        names = [rows[0][1].name] + [r[2][0][1].name for r in ranked[1:4] if r[0] >= top_score - 8]
        return outcome("NEEDS_CLARIFICATION", "AMBIGUOUS",
                       "That could mean more than one thing: " + ", ".join(names) + ". Which one do you want?", candidates=names)

    ok = [(s, it) for s, it in rows if compatible(it, asset)]
    name = rows[0][1].name
    if not ok:
        if any(it.platform == (asset.platform or "") for _, it in rows):
            return outcome("INCOMPATIBLE", "ARCH_MISMATCH",
                           f"The approved {name} build doesn't support this computer's processor. Ask IT to add a build for it.",
                           item=rows[0][1])
        platforms = ", ".join(sorted({dict(SoftwareItem.PLATFORMS).get(it.platform, it.platform) for _, it in rows}))
        return outcome("INCOMPATIBLE", "PLATFORM_MISMATCH",
                       f"{name} is only available for {platforms}, so I can't install it on this computer. Ask IT if there is an alternative.",
                       item=rows[0][1])
    # Prefer the build made for this CPU over a generic one.
    item = sorted(ok, key=lambda r: (r[1].arch == "any", -r[0]))[0][1]

    have = None if reinstall else installed_version(asset, item)
    if have is not None:
        if version_key(have) > version_key(item.version):
            return outcome("ALREADY_INSTALLED", "NEWER_VERSION_INSTALLED",
                           f"This computer already has {item.name} {have}, which is newer than the approved {item.version}. Nothing to do.",
                           item=item, candidates=[item.name])
        if version_key(have) == version_key(item.version):
            return outcome("ALREADY_INSTALLED", "ALREADY_INSTALLED",
                           f"{item.name} {have} is already installed and up to date on this computer. If it isn't working properly, say “reinstall {item.name}”.",
                           item=item, candidates=[item.name])

    if item.requires_approval:
        extra = f" It will update {item.name} {have}." if have else ""
        return outcome("AWAITING_APPROVAL", "APPROVAL_REQUIRED",
                       f"{item.name} {item.version} needs an admin's approval first.{extra} I've sent it to them and I'll install it as soon as it's approved.",
                       item=item, upgrade_from=have)
    return outcome("QUEUED", "AUTO_APPROVED",
                   f"{item.name} {item.version} is pre-approved. I'll install it now — you can keep working.", item=item, upgrade_from=have)


# ---- lifecycle ----------------------------------------------------------------------------------

def record(req, actor, kind, detail=""):
    # Also goes to the application log so operators can search/alert on it without querying the database. No names or emails: ids only.
    log.info("software.%s request=%s asset=%s actor=%s", kind, req.id, req.asset_id, str(actor).split(":")[0])
    SoftwareEvent.objects.create(request=req, actor=actor[:160], kind=kind, detail=str(detail or "")[:500])


def _asset_actor(asset, role="employee"):
    return f"{role}:{asset.asset_id}"


def _find_existing(asset, key):
    return SoftwareRequest.objects.filter(asset=asset, idempotency_key=key).first()


def _lock_asset(asset):
    """Serialises request creation for one laptop. Without it two simultaneous requests (a double click, a retry of a slow call)
    could both pass the duplicate checks, because there is no row yet to lock. Everything that follows re-checks inside the lock."""
    Asset.objects.select_for_update().filter(pk=asset.pk).values_list("pk", flat=True).first()


def create_request(asset, prompt, idempotency_key, reinstall=False):
    """Returns (request, replayed). Raises SoftwareError (input / rate limits) or Answered."""
    prompt = clean_prompt(prompt)
    key = re.sub(r"[^A-Za-z0-9._:-]", "", str(idempotency_key or ""))[:64]
    if not key:
        raise SoftwareError("This request is missing its id. Please try again.", code="missing_key")

    existing = _find_existing(asset, key)
    if existing:
        return existing, True

    open_now = SoftwareRequest.objects.filter(asset=asset, status__in=REQUEST_OPEN).count()
    if open_now >= settings.SOFTWARE_MAX_OPEN_PER_ASSET:
        raise SoftwareError(
            f"You already have {open_now} requests in progress. Wait for one to finish (or cancel it), then ask again.",
            status=429, code="too_many_open")

    result = evaluate(asset, prompt, reinstall)
    if result["status"] == "NOT_FOUND":
        # Nothing matched. It may be a typo, a loose description, or not about software at all ("how are you").
        from . import assistant  # imported here: the assistant itself uses this module
        name = (result["candidates"] or [None])[0]  # a close spelling needs no model
        chat = None
        if not name:
            chat = assistant.assist(asset, prompt)
            name = chat.get("suggestion") if chat.get("available") else None
        if name:
            # The employee must say yes before anything is requested.
            words = [t for t in tokens(prompt) if t not in FILLER]
            # Quote a short typo ("slakc"); for a loose description a quoted phrase would only read strangely.
            what = f"“{' '.join(words)}”" if 0 < len(words) <= 2 and len(" ".join(words)) <= 30 else "an exact match for that"
            raise Answered(outcome("NEEDS_CONFIRMATION", "DID_YOU_MEAN",
                                   f"I couldn't find {what}. Did you mean {name}? Reply “yes” and I’ll request it for you.",
                                   candidates=[name]))
        if chat and chat.get("available") and chat.get("intent") != "install":
            raise Answered(outcome("CHAT", "CHAT_REPLY", chat["reply"]))  # small talk / off-topic: answered, not recorded as a request
        if chat and chat.get("available"):
            # A real app that just isn't approved yet: look it up, and offer to send it to IT (the employee must say yes).
            proposal = assistant.propose(asset, prompt)
            if proposal:
                return _create_proposed(asset, prompt, key, proposal)
    if result["status"] in UNTRACKED:
        raise Answered(result)

    item = result["item"]
    try:
        return _record_request(asset, prompt, key, result, item)
    except IntegrityError:
        # The database's own guarantee (one request per laptop and id) caught what the lock could not: answer with the winner.
        existing = _find_existing(asset, key)
        if existing:
            return existing, True
        raise


def _record_request(asset, prompt, key, result, item):
    with transaction.atomic():
        _lock_asset(asset)
        existing = _find_existing(asset, key)  # re-checked inside the lock: the same request may have just been recorded
        if existing:
            return existing, True
        if result["status"] == "NOT_FOUND":
            # The same missing app asked about again soon: show the earlier answer instead of piling up rows.
            again = (SoftwareRequest.objects.filter(asset=asset, status="NOT_FOUND", raw_request__iexact=prompt,
                                                    created_at__gt=timezone.now() - timedelta(hours=1)).first())
            if again:
                return again, True
        if item and result["status"] in ("AWAITING_APPROVAL", "QUEUED"):
            # Same software already being handled for this laptop: show that one instead of queueing a twin.
            twin = (SoftwareRequest.objects.select_for_update()
                    .filter(asset=asset, software=item, status__in=REQUEST_OPEN).first())
            if twin:
                return twin, True
        req = SoftwareRequest.objects.create(
            asset=asset, idempotency_key=key, raw_request=prompt, software=item,
            software_name=item.name if item else "", version=item.version if item else "",
            spec=item.spec() if item and result["status"] in ("AWAITING_APPROVAL", "QUEUED") else {},
            status=result["status"], decision_reason=result["reason"], explanation=result["explanation"][:600],
        )
        record(req, _asset_actor(asset), "requested", prompt)
        record(req, "system", result["status"].lower(), result["reason"])
        if result["status"] == "QUEUED":
            record(req, "system", "auto_approved", "Catalogue entry does not require approval")
    return req, False


def _create_proposed(asset, prompt, key, proposal):
    """-> (request, replayed): an existing suggestion / request for the same app is returned instead of a second one."""
    name, vendor = proposal["name"], proposal.get("vendor") or ""
    same = compact(name)
    with transaction.atomic():
        _lock_asset(asset)
        existing = _find_existing(asset, key)
        if existing:
            return existing, True
        for twin in SoftwareRequest.objects.select_for_update().filter(asset=asset, kind="new", status__in=("PROPOSED", "AWAITING_APPROVAL")):
            if compact(twin.software_name) == same:
                return twin, True  # already suggested / already with IT
        req = SoftwareRequest.objects.create(
            asset=asset, idempotency_key=key, raw_request=prompt, kind="new", proposal=proposal, software_name=name, status="PROPOSED",
            decision_reason="NEW_SOFTWARE",
            explanation=(f"“{name}” isn't on your approved software list yet. I found {vendor + ' ' if vendor else ''}{name} at {proposal['host']} "
                         f"({proposal['licenseLabel'].lower()}). Would you like me to ask IT to review it and add it? Reply “yes” to send it to IT."),
        )
        record(req, _asset_actor(asset), "requested", prompt)
        record(req, "system", "proposed", f"{name} at {proposal['host']}; licence: {proposal['licenseKind']}")
    return req, False


def confirm_proposal(asset, request_id):
    """The employee said yes: the suggestion goes to the admins."""
    with transaction.atomic():
        req = SoftwareRequest.objects.select_for_update().filter(pk=request_id, asset=asset, kind="new").first()
        if not req:
            raise SoftwareError("I couldn't find that suggestion.", status=404, code="not_found")
        if req.status in REQUEST_OPEN:
            return req  # already sent: confirming twice is harmless
        if req.status != "PROPOSED":
            raise SoftwareError("That suggestion has expired. Ask me for the app again and I'll look it up.", status=409, code="expired")
        if SoftwareRequest.objects.filter(asset=asset, status__in=REQUEST_OPEN).count() >= settings.SOFTWARE_MAX_OPEN_PER_ASSET:
            raise SoftwareError("You already have several requests in progress. Wait for one to finish (or cancel it), then ask again.", status=429, code="too_many_open")
        req.status, req.decision_reason = "AWAITING_APPROVAL", "NEW_SOFTWARE_REVIEW"
        req.explanation = f"I've asked IT to review {req.software_name} and add it to the approved list. I'll install it as soon as it's approved."
        req.request_version += 1
        req.save()
        record(req, _asset_actor(asset), "confirmed", "Employee asked IT to review it")
    return req


def cancel_request(asset, request_id):
    with transaction.atomic():
        req = SoftwareRequest.objects.select_for_update().filter(pk=request_id, asset=asset).first()
        if not req:
            raise SoftwareError("I couldn't find that request.", status=404, code="not_found")
        if req.status not in ("AWAITING_APPROVAL", "QUEUED", "PROPOSED"):
            if req.status in REQUEST_ACTIVE:
                raise SoftwareError("The install has already started, so it can't be cancelled now.", status=409, code="already_running")
            raise SoftwareError("That request has already finished.", status=409, code="already_finished")
        req.status = "CANCELLED"
        req.decision_reason = "CANCELLED_BY_EMPLOYEE"
        req.explanation = "You cancelled this request."
        req.request_version += 1
        req.finished_at = timezone.now()
        req.save()
        record(req, _asset_actor(asset), "cancelled")
    return req


def decide(request_id, user, decision, expected_version, comment=""):
    """Admin approves or rejects a request that is waiting for approval."""
    if decision not in ("APPROVED", "REJECTED"):
        raise SoftwareError("Decision must be APPROVED or REJECTED.", code="bad_decision")
    comment = str(comment or "").strip()[:500]
    who = user.get_full_name() or user.get_username()
    with transaction.atomic():
        req = (SoftwareRequest.objects.select_for_update(of=("self",)).select_related("asset", "software", "decided_by")
               .filter(pk=request_id).first())
        if not req:
            raise SoftwareError("That request no longer exists.", status=404, code="not_found")
        if req.status != "AWAITING_APPROVAL":
            by = f", decided by {req.decided_by.get_full_name() or req.decided_by.get_username()}" if req.decided_by_id else ""
            raise SoftwareError(f"This request is no longer waiting for approval (status: {req.status.replace('_', ' ').lower()}{by}).",
                                status=409, code="not_pending")
        if expected_version is not None and int(expected_version) != req.request_version:
            raise SoftwareError("This request was changed while you were looking at it. The list has been refreshed — please review it again.",
                                status=409, code="stale")
        now = timezone.now()
        if decision == "APPROVED" and req.kind == "new" and not req.software_id:
            # Software that wasn't in the catalogue: an admin must review the real file first. (If someone added it to the
            # catalogue in the meantime, this request simply attaches to that entry.)
            found = evaluate(req.asset, req.software_name)
            if found["item"] and found["status"] in ("AWAITING_APPROVAL", "QUEUED"):
                req.software, req.spec = found["item"], found["item"].spec()
            else:
                raise SoftwareError("This is new software, so it has to be checked before it can be approved. Use “Review & approve” to check the file and add it to the catalogue.",
                                    status=409, code="needs_review")
        if decision == "APPROVED":
            item = req.software
            spec = req.spec or {}
            if not item or not item.enabled or item.sha256.lower() != str(spec.get("sha256", "")).lower():
                raise SoftwareError("The catalogue entry was changed or removed after this request was made. Reject it and ask the employee to request again.",
                                    status=409, code="catalogue_changed")
            req.status = "QUEUED"
            req.decision_reason = "APPROVED"
            req.explanation = f"{who} approved {req.software_name}. Installing on your computer now."
        else:
            req.status = "REJECTED"
            req.decision_reason = "REJECTED"
            req.explanation = f"{who} declined this request." + (f" Reason: {comment}" if comment else "")
            req.finished_at = now
        req.decided_by, req.decided_at, req.decision_comment = user, now, comment
        req.request_version += 1
        req.save()
        record(req, f"admin:{user.get_username()}", decision.lower(), comment)
    return req


def _fail_catalogue(req, reason, message):
    req.status = "BLOCKED"
    req.decision_reason = reason
    req.explanation = message
    req.finished_at = timezone.now()
    req.request_version += 1
    req.save()
    record(req, "system", "blocked", reason)


def claim_next_job(asset):
    """The laptop asks for work. At most one install per laptop runs at a time."""
    reap_stale(asset)
    with transaction.atomic():
        if SoftwareRequest.objects.filter(asset=asset, status__in=("DISPATCHED", "RUNNING", "VERIFYING")).exists():
            return None
        while True:
            req = (SoftwareRequest.objects.select_for_update(of=("self",)).select_related("software")
                   .filter(asset=asset, status="QUEUED").order_by("created_at").first())
            if not req:
                return None
            item, spec = req.software, req.spec or {}
            if not item or not item.enabled:
                _fail_catalogue(req, "ITEM_WITHDRAWN", "This software was removed from the approved list before it could be installed.")
                continue
            if item.sha256.lower() != str(spec.get("sha256", "")).lower() or item.download_url != spec.get("downloadUrl"):
                _fail_catalogue(req, "CATALOGUE_CHANGED", "The approved installer changed after this request was made, so I stopped. Please ask again.")
                continue
            now = timezone.now()
            req.status = "DISPATCHED"
            req.job_claimed_at = now
            req.progress_phase = "DOWNLOADING"
            req.progress_bytes_done, req.progress_bytes_total = 0, spec.get("sizeBytes")
            req.progress_updated_at = now
            req.request_version += 1
            req.save()
            record(req, _asset_actor(asset, "agent"), "claimed")
            return req


_PHASE_STATUS = {"DOWNLOADING": "DISPATCHED", "INSTALLING": "RUNNING", "VERIFYING": "VERIFYING"}
_STATUS_ORDER = {"DISPATCHED": 0, "RUNNING": 1, "VERIFYING": 2}


def report_progress(asset, request_id, phase, done=None, total=None):
    phase = str(phase or "").upper()
    if phase not in _PHASE_STATUS:
        raise SoftwareError("Unknown progress phase.", code="bad_phase")
    with transaction.atomic():
        req = SoftwareRequest.objects.select_for_update().filter(pk=request_id, asset=asset).first()
        if not req:
            raise SoftwareError("Unknown request.", status=404, code="not_found")
        if req.status not in _STATUS_ORDER:
            return req  # finished, cancelled or reaped meanwhile: nothing to update
        new_status = _PHASE_STATUS[phase]
        if _STATUS_ORDER[new_status] >= _STATUS_ORDER[req.status]:
            if new_status != req.status:
                record(req, _asset_actor(asset, "agent"), phase.lower())
            req.status = new_status
            req.progress_phase = phase
        for field, value in (("progress_bytes_done", done), ("progress_bytes_total", total)):
            if isinstance(value, (int, float)) and value >= 0:
                setattr(req, field, int(value))
        req.progress_updated_at = timezone.now()
        req.save()
    return req


def report_result(asset, request_id, ok, code="", detail=""):
    code = re.sub(r"[^A-Z0-9_]", "", str(code or "").upper())[:40]
    with transaction.atomic():
        req = SoftwareRequest.objects.select_for_update().filter(pk=request_id, asset=asset).first()
        if not req:
            raise SoftwareError("Unknown request.", status=404, code="not_found")
        recoverable = req.status == "FAILED" and req.decision_reason == "INSTALL_INTERRUPTED"
        if req.status not in _STATUS_ORDER and not (ok and recoverable):
            return req  # already finished: reports are idempotent
        now = timezone.now()
        if ok:
            req.status, req.decision_reason = "SUCCEEDED", "INSTALLED"
            req.explanation = f"{req.software_name} {req.version} is installed and verified on your computer."
        else:
            req.status, req.decision_reason = "FAILED", code or "INSTALL_FAILED"
            req.explanation = "The install didn't complete."
        req.finished_at, req.progress_updated_at = now, now
        req.request_version += 1
        req.save()
        record(req, _asset_actor(asset, "agent"), "succeeded" if ok else "failed", code or detail)
    return req


def reap_stale(asset=None):
    """Closes requests nobody is working on any more, so nothing stays 'in progress' forever."""
    now = timezone.now()
    qs = SoftwareRequest.objects.all()
    if asset is not None:
        qs = qs.filter(asset=asset)
    rules = (
        (qs.filter(status="AWAITING_APPROVAL", created_at__lt=now - timedelta(hours=settings.SOFTWARE_APPROVAL_TTL_HOURS)),
         "EXPIRED", "APPROVAL_TIMEOUT", "Nobody reviewed this request in time, so it expired. Ask again if you still need it."),
        (qs.filter(status="PROPOSED", created_at__lt=now - timedelta(hours=1)),
         "EXPIRED", "PROPOSAL_EXPIRED", "That suggestion expired because nobody confirmed it. Ask me again if you still need it."),
        (qs.filter(status="QUEUED", updated_at__lt=now - timedelta(hours=settings.SOFTWARE_QUEUE_TTL_HOURS)),
         "EXPIRED", "DEVICE_OFFLINE", "Your computer didn't pick this up in time, so it expired. Keep the app running and ask again."),
        (qs.filter(status__in=("DISPATCHED", "RUNNING", "VERIFYING"), progress_updated_at__lt=now - timedelta(minutes=settings.SOFTWARE_STALE_MINUTES)),
         "FAILED", "INSTALL_INTERRUPTED", "The install was interrupted (the app or computer stopped). Say “try again”."),
    )
    for rows, status, reason, text in rules:
        for req in list(rows[:200]):
            with transaction.atomic():
                fresh = SoftwareRequest.objects.select_for_update().get(pk=req.pk)
                if fresh.status != req.status:
                    continue
                fresh.status, fresh.decision_reason, fresh.explanation = status, reason, text
                fresh.finished_at = now
                fresh.request_version += 1
                fresh.save()
                record(fresh, "system", status.lower(), reason)


def queued_job_count(asset):
    return SoftwareRequest.objects.filter(asset=asset, status="QUEUED").count()


def close_open_requests(asset, actor):
    """The laptop was disconnected (new enrollment code): nothing may later install on whoever enrolls next.
    Waiting requests are cancelled; one already running is marked interrupted."""
    now = timezone.now()
    with transaction.atomic():
        for req in SoftwareRequest.objects.select_for_update().filter(asset=asset, status__in=REQUEST_OPEN):
            if req.status in REQUEST_ACTIVE and req.status != "QUEUED":
                req.status, req.decision_reason = "FAILED", "INSTALL_INTERRUPTED"
                req.explanation = "This laptop was disconnected from Proglintor, so the install was stopped."
            else:
                req.status, req.decision_reason = "CANCELLED", "DEVICE_RESET"
                req.explanation = "This request was cancelled because the laptop was disconnected from Proglintor."
            req.finished_at = now
            req.request_version += 1
            req.save()
            record(req, actor, "cancelled" if req.status == "CANCELLED" else "failed", "Laptop disconnected")


def approve_new(request_id, user, expected_version, create_item, licence_confirmed=False):
    """Admin approval of a new-software request: the checked item is added to the catalogue and the install is queued, in one step.
    `create_item()` builds the catalogue entry (validation errors propagate and nothing is changed)."""
    who = user.get_full_name() or user.get_username()
    with transaction.atomic():
        req = (SoftwareRequest.objects.select_for_update(of=("self",)).select_related("asset", "decided_by").filter(pk=request_id).first())
        if not req or req.kind != "new":
            raise SoftwareError("That request no longer exists.", status=404, code="not_found")
        if req.status != "AWAITING_APPROVAL":
            raise SoftwareError(f"This request is no longer waiting for approval (status: {req.status.replace('_', ' ').lower()}).", status=409, code="not_pending")
        if expected_version is not None and int(expected_version) != req.request_version:
            raise SoftwareError("This request was changed while you were looking at it. The list has been refreshed — please review it again.", status=409, code="stale")
        proposal = req.proposal or {}
        if not proposal.get("licenseClear") and not licence_confirmed:
            raise SoftwareError("Please confirm that the company is licensed to use this software before approving it.", status=409, code="licence_unconfirmed")
        item = create_item()
        if not compatible(item, req.asset):
            names = dict(SoftwareItem.PLATFORMS)
            raise SoftwareError(f"That build is for {names.get(item.platform, item.platform)}, but {req.asset.employee_name}'s computer runs "
                                f"{names.get(req.asset.platform, req.asset.platform or 'a different system')}. Check a build for their computer.", status=400, code="incompatible")
        now = timezone.now()
        req.software, req.software_name, req.version, req.spec = item, item.name, item.version, item.spec()
        req.status, req.decision_reason = "QUEUED", "APPROVED_NEW"
        req.explanation = f"{who} approved {item.name} and added it to the approved list. Installing on your computer now."
        req.decided_by, req.decided_at = user, now
        req.request_version += 1
        req.save()
        record(req, f"admin:{user.get_username()}", "approved_new",
               f"added to the catalogue as {item.slug}; licence: {proposal.get('licenseKind', 'unknown')}{'' if proposal.get('licenseClear') else ' (confirmed by the admin)'}")
    return req, item

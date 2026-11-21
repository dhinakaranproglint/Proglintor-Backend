"""Software install API.

Employee (enrolled laptop, `Authorization: Bearer <agent token>`):
    GET  /api/agent/software/catalogue          what can be requested on this laptop
    POST /api/agent/software/preview            {prompt}                    "what would happen?" (nothing is recorded)
    GET  /api/agent/software/requests           this laptop's recent requests
    POST /api/agent/software/requests           {prompt, idempotencyKey}    ask for an install
    POST /api/agent/software/requests/<id>/cancel
    POST /api/agent/software/jobs/next          claim the next approved install      -> {job|null}
    POST /api/agent/software/jobs/<id>/progress {phase, bytesDone, bytesTotal}
    POST /api/agent/software/jobs/<id>/result   {ok, code, detail}

Admin (staff user, `Authorization: Token <key>`):
    GET/POST        /api/admin/software/items              catalogue
    GET/PATCH/DELETE /api/admin/software/items/<slug>
    GET             /api/admin/software/approvals          waiting for a decision
    POST            /api/admin/software/requests/<id>/decision {decision, comment, expectedVersion}
    GET             /api/admin/software/requests[?status=&asset=]   history
    GET             /api/admin/software/requests/<id>      detail + audit trail
Employees can never reach the admin endpoints (they have no staff token), and admins have no agent token, so each
role can only do its own part of the flow.
"""
import re
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from . import assistant
from . import software as sw
from .agent_api import AgentView, _rate_limited
from .models import REQUEST_OPEN, Asset, SoftwareItem, SoftwareRequest

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,59}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TEAM_RE = re.compile(r"^[A-Z0-9]{10}$")
ICON_RE = re.compile(r"^data:image/png;base64,[A-Za-z0-9+/=]+$")
ICON_MAX_CHARS = 200 * 1024


def fail(err):
    return Response({"error": err.message, "code": err.code}, status=err.status)


# ---- employee side ---------------------------------------------------------------------------------

class CatalogueView(AgentView):
    def get(self, request):
        asset: Asset = request.auth
        items = [i for i in SoftwareItem.objects.filter(enabled=True) if sw.compatible(i, asset)]
        # `assistant` tells the app whether AI is in use, so it can show employees a notice before they type.
        return Response({"items": [i.to_public() for i in items], "assistant": {"chat": assistant.available(), "webLookup": assistant.web_lookup_enabled()}})


class PreviewView(AgentView):
    def post(self, request):
        asset: Asset = request.auth
        try:
            prompt = sw.clean_prompt(request.data.get("prompt"))
        except sw.SoftwareError as err:
            return fail(err)
        r = sw.evaluate(asset, prompt)
        item = r["item"]
        return Response({
            "status": r["status"], "reasonCode": r["reason"], "explanation": r["explanation"],
            "match": item.to_public() if item else None, "candidates": r["candidates"], "upgradeFrom": r["upgradeFrom"],
        })


class AssistView(AgentView):
    """Chat that isn't a request: small talk, "what can you do", off-topic redirects. Needs no model to function (see assistant.py)."""

    def post(self, request):
        try:
            return Response(assistant.assist(request.auth, request.data.get("message"), request.data.get("history")))
        except sw.SoftwareError as err:
            return fail(err)


class RequestListView(AgentView):
    def get(self, request):
        asset: Asset = request.auth
        sw.reap_stale(asset)
        rows = SoftwareRequest.objects.filter(asset=asset).select_related("decided_by")[:50]
        return Response({"items": [r.to_dict() for r in rows]})

    def post(self, request):
        asset: Asset = request.auth
        if _rate_limited(f"sw-submit:{asset.asset_id}", settings.SOFTWARE_SUBMITS_PER_10MIN, 600):
            return Response({"error": "You're sending requests very quickly. Please wait a few minutes and try again.", "code": "rate_limited"}, status=429)
        try:
            req, replayed = sw.create_request(asset, request.data.get("prompt"), request.data.get("idempotencyKey"), request.data.get("reinstall") is True)
        except sw.Answered as ask:
            r = ask.result
            return Response({"status": r["status"], "id": None, "decisionReason": r["reason"], "explanation": r["explanation"],
                             "candidates": r["candidates"], "rawRequest": str(request.data.get("prompt") or "")[:500]})
        except sw.SoftwareError as err:
            return fail(err)
        return Response(req.to_dict(replayed=replayed), status=200 if replayed else 201)


class RequestConfirmView(AgentView):
    """The employee said yes to "ask IT to review this app?"."""

    def post(self, request, request_id):
        try:
            req = sw.confirm_proposal(request.auth, request_id)
        except sw.SoftwareError as err:
            return fail(err)
        return Response(req.to_dict())


class RequestCancelView(AgentView):
    def post(self, request, request_id):
        try:
            req = sw.cancel_request(request.auth, request_id)
        except sw.SoftwareError as err:
            return fail(err)
        return Response(req.to_dict())


class JobNextView(AgentView):
    def post(self, request):
        req = sw.claim_next_job(request.auth)
        if not req:
            return Response({"job": None})
        return Response({"job": {"requestId": str(req.id), "spec": req.spec}})


class JobProgressView(AgentView):
    def post(self, request, request_id):
        d = request.data
        try:
            req = sw.report_progress(request.auth, request_id, d.get("phase"), d.get("bytesDone"), d.get("bytesTotal"))
        except sw.SoftwareError as err:
            return fail(err)
        # Tells the agent to stop if the request was closed on the server (e.g. reaped as interrupted).
        return Response({"status": req.status, "continue": req.status in ("DISPATCHED", "RUNNING", "VERIFYING")})


class JobResultView(AgentView):
    def post(self, request, request_id):
        d = request.data
        try:
            req = sw.report_result(request.auth, request_id, d.get("ok") is True, d.get("code"), d.get("detail"))
        except sw.SoftwareError as err:
            return fail(err)
        return Response(req.to_dict())


# ---- admin side ------------------------------------------------------------------------------------

def clean_item(data, partial=False, existing=None):
    """Validates catalogue input from an admin; returns model field values or raises SoftwareError."""
    out = {}

    def need(key):
        return not (partial and key not in data)

    def text(key, field, n, required=False, label=None):
        if not need(key):
            return
        value = str(data.get(key) if data.get(key) is not None else "").strip()
        if required and not value:
            raise sw.SoftwareError(f"{label or key} is required", code=key)
        out[field] = value[:n]

    text("name", "name", 120, True, "Name")
    text("version", "version", 40, True, "Version")
    text("publisher", "publisher", 120)
    text("description", "description", 300)
    text("appId", "app_id", 255)
    text("appName", "app_name", 255)
    if out.get("app_name") and (re.search(r"[\\/]", out["app_name"]) or out["app_name"].startswith(".")):
        raise sw.SoftwareError("App bundle name must be a file name like “Foo.app”, not a path", code="appName")

    if need("platform"):
        platform = str(data.get("platform") or "")
        if platform not in dict(SoftwareItem.PLATFORMS):
            raise sw.SoftwareError("Choose macOS, Windows or Linux", code="platform")
        out["platform"] = platform
    if need("arch"):
        arch = str(data.get("arch") or "any")
        if arch not in dict(SoftwareItem.ARCHS):
            raise sw.SoftwareError("Choose Any, ARM64 or x64", code="arch")
        out["arch"] = arch
    if need("installerType"):
        kind = str(data.get("installerType") or "")
        if kind not in dict(SoftwareItem.INSTALLERS):
            raise sw.SoftwareError("Choose an installer type (dmg, zip, pkg, msi, exe or AppImage)", code="installerType")
        out["installer_type"] = kind
    if need("downloadUrl"):
        url = str(data.get("downloadUrl") or "").strip()
        parts = urlparse(url)
        https_ok = parts.scheme == "https" and bool(parts.hostname)
        dev_ok = settings.DEBUG and parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")
        if not (https_ok or dev_ok):
            raise sw.SoftwareError("The download link must be a secure https:// address", code="downloadUrl")
        out["download_url"] = url[:1000]
    if need("sha256"):
        digest = str(data.get("sha256") or "").strip()
        if not SHA_RE.match(digest):
            raise sw.SoftwareError("Enter the installer's SHA-256 checksum (64 hexadecimal characters). Laptops refuse any file that doesn't match it.", code="sha256")
        out["sha256"] = digest.lower()
    if need("sizeBytes"):
        size = data.get("sizeBytes")
        if size in (None, ""):
            out["size_bytes"] = None
        else:
            try:
                size = int(size)
            except (TypeError, ValueError):
                raise sw.SoftwareError("Size must be a number of bytes", code="sizeBytes")
            if size <= 0:
                raise sw.SoftwareError("Size must be a positive number of bytes", code="sizeBytes")
            out["size_bytes"] = size
    if need("teamId"):
        team = str(data.get("teamId") or "").strip().upper()
        if team and not TEAM_RE.match(team):
            raise sw.SoftwareError("An Apple Team ID is 10 letters/numbers, e.g. UBF8T346G9", code="teamId")
        out["team_id"] = team
    if need("aliases"):
        aliases = data.get("aliases")
        if isinstance(aliases, str):
            aliases = [a for a in re.split(r"[,\n]", aliases)]
        if aliases is None:
            aliases = []
        if not isinstance(aliases, list):
            raise sw.SoftwareError("Aliases must be a list of names", code="aliases")
        out["aliases"] = [str(a).strip()[:80] for a in aliases if str(a).strip()][:20]
    if need("silentArgs"):
        args = data.get("silentArgs")
        if isinstance(args, str):
            args = args.split()
        if args is None:
            args = []
        if not isinstance(args, list) or any(not isinstance(a, str) or len(a) > 200 for a in args) or len(args) > 20:
            raise sw.SoftwareError("Silent install options must be a short list of text arguments", code="silentArgs")
        out["silent_args"] = args
    if "icon" in data:
        icon = data.get("icon") or ""
        if icon and (len(icon) > ICON_MAX_CHARS or not ICON_RE.match(icon)):
            raise sw.SoftwareError("The app icon must be a small PNG image", code="icon")
        out["icon"] = icon
    for key, field in (("requiresApproval", "requires_approval"), ("enabled", "enabled")):
        if key in data:
            out[field] = data.get(key) is True
    return out


def make_item(data):
    """Validates admin input and creates a catalogue entry (used by 'Add software' and by approving a new-software request)."""
    slug = str(data.get("slug") or "").strip().lower()
    if not slug:  # derive from the name, e.g. "Visual Studio Code" + macOS -> visual-studio-code-darwin
        base = re.sub(r"[^a-z0-9]+", "-", f"{data.get('name') or ''} {data.get('platform') or ''}".lower()).strip("-")
        slug = base[:60]
    if not SLUG_RE.match(slug):
        raise sw.SoftwareError("Give the software a name (letters and numbers) so it can get an id", code="name")
    if SoftwareItem.objects.filter(slug=slug).exists():
        raise sw.SoftwareError(f"“{slug}” is already in the catalogue. Edit that entry instead.", status=409, code="duplicate")
    fields = clean_item(data)
    fields.setdefault("requires_approval", True)
    return SoftwareItem.objects.create(slug=slug, **fields)


class ItemListView(APIView):
    def get(self, request):
        counts = dict(SoftwareRequest.objects.values_list("software_id").annotate(n=Count("id")))
        return Response({"items": [{**i.to_dict(), "requests": counts.get(i.slug, 0)} for i in SoftwareItem.objects.all()]})

    def post(self, request):
        try:
            item = make_item(request.data)
        except sw.SoftwareError as err:
            return fail(err)
        return Response(item.to_dict(with_icon=True), status=201)


class ItemIconsView(APIView):
    """GET ?slugs=a,b,c -> {icons: {slug: data-url}} for the entries that have one (at most 50 per call)."""

    def get(self, request):
        slugs = [x for x in str(request.query_params.get("slugs", "")).split(",") if x][:50]
        rows = SoftwareItem.objects.filter(slug__in=slugs).exclude(icon="").values_list("slug", "icon")
        return Response({"icons": dict(rows)})


class ItemDetailView(APIView):
    def get(self, request, slug):
        return Response(get_object_or_404(SoftwareItem, slug=slug).to_dict(with_icon=True))

    def patch(self, request, slug):
        item = get_object_or_404(SoftwareItem, slug=slug)
        try:
            fields = clean_item(request.data, partial=True, existing=item)
        except sw.SoftwareError as err:
            return fail(err)
        for k, v in fields.items():
            setattr(item, k, v)
        item.save()
        return Response(item.to_dict(with_icon=True))

    def delete(self, request, slug):
        item = get_object_or_404(SoftwareItem, slug=slug)
        busy = item.requests.filter(status__in=REQUEST_OPEN).count()
        if busy:
            return Response({"error": f"{busy} request{'s are' if busy != 1 else ' is'} still in progress for this software. "
                                      "Disable it instead, or wait until they finish.", "code": "in_use"}, status=409)
        item.delete()  # finished requests keep their name/version snapshot
        return Response(status=204)


class ApprovalListView(APIView):
    def get(self, request):
        sw.reap_stale()
        rows = (SoftwareRequest.objects.filter(status="AWAITING_APPROVAL")
                .select_related("asset", "decided_by", "software").order_by("created_at")[:200])
        return Response({"items": [r.to_admin(icon=True) for r in rows]})


class DecisionView(APIView):
    def post(self, request, request_id):
        d = request.data
        try:
            req = sw.decide(request_id, request.user, str(d.get("decision") or "").upper(), d.get("expectedVersion"), d.get("comment"))
        except (sw.SoftwareError, ValueError) as err:
            if isinstance(err, ValueError):
                return Response({"error": "Invalid request version.", "code": "bad_version"}, status=400)
            return fail(err)
        return Response(req.to_admin())


class ApproveNewView(APIView):
    """Approve software that wasn't in the catalogue: the admin has checked the real file; this adds it and queues the install."""

    def post(self, request, request_id):
        d = request.data
        item = d.get("item")
        if not isinstance(item, dict):
            return Response({"error": "Check the download first so I know what to add.", "code": "item"}, status=400)
        try:
            req, entry = sw.approve_new(request_id, request.user, d.get("expectedVersion"), lambda: make_item(item), d.get("licenceConfirmed") is True)
        except sw.SoftwareError as err:
            return fail(err)
        except ValueError:
            return Response({"error": "Invalid request version.", "code": "bad_version"}, status=400)
        return Response({"request": req.to_admin(), "item": entry.to_dict()})


class RequestAdminListView(APIView):
    def get(self, request):
        sw.reap_stale()
        qs = SoftwareRequest.objects.select_related("asset", "decided_by")
        status = request.query_params.get("status")
        if status == "open":
            qs = qs.filter(status__in=REQUEST_OPEN)
        elif status == "done":
            qs = qs.exclude(status__in=REQUEST_OPEN)
        elif status:
            qs = qs.filter(status=status.upper())
        asset = request.query_params.get("asset")
        if asset:
            qs = qs.filter(asset_id=asset)
        return Response({"items": [r.to_admin() for r in qs[:300]]})


class RequestAdminDetailView(APIView):
    def get(self, request, request_id):
        req = get_object_or_404(SoftwareRequest.objects.select_related("asset", "decided_by"), pk=request_id)
        return Response({**req.to_admin(), "events": [e.to_dict() for e in req.events.all()]})

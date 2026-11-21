"""API used by Proglintor agents on employee laptops.

The endpoints and JSON payloads match what the agent (proglint-pulse/src/agent/sync.js) sends:
    POST /api/agent/enroll     {code, hostname, os, platform, agentVersion} -> {token, asset, policy}
    POST /api/agent/heartbeat  live state, current app, running apps        -> {policy, asset, serverTime}
    POST /api/agent/usage      {days: {"YYYY-MM-DD": [apps]}}  (whole days, replaced)
    POST /api/agent/installed  {apps: [...]}
    POST /api/agent/alerts     {alerts: [...]}  (idempotent by uid)
Agents authenticate with `Authorization: Bearer <token>` issued at enrollment.
"""
import re
import time
from datetime import date, datetime, timezone as dt_timezone

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import authentication, exceptions, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Asset, DailyUsage, InstalledApps, Setting, WeekendAlert, hash_token
from .policy import DEFAULT_POLICY, normalize_policy

DAY_RE = re.compile(r"^\d{4}-\d\d-\d\d$")


def from_ms(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return datetime.fromtimestamp(v / 1000, tz=dt_timezone.utc)


def current_policy():
    return normalize_policy(Setting.get("policy", DEFAULT_POLICY))


def clip(value, n):
    return str(value or "")[:n]


class AgentTokenAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer <token>` -> the enrolled Asset."""

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("bearer "):
            raise exceptions.AuthenticationFailed("This device is not registered")
        token = header[7:].strip()
        asset = Asset.objects.filter(token_hash=hash_token(token)).first() if token else None
        if not asset:
            raise exceptions.AuthenticationFailed("This device is not registered")
        return (None, asset)

    def authenticate_header(self, request):
        return "Bearer"  # makes DRF answer 401 (the agent treats 401 as "disconnected by admin")


class IsAgent(permissions.BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, Asset)


class AgentView(APIView):
    authentication_classes = [AgentTokenAuthentication]
    permission_classes = [IsAgent]


def _client_ip(request):
    return request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")


def _rate_limited(key, limit=10, window=600):
    now = time.time()
    hits = [t for t in cache.get(key, []) if now - t < window]
    hits.append(now)
    cache.set(key, hits, window)
    return len(hits) > limit


class EnrollView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        if _rate_limited(f"enroll:{_client_ip(request)}"):
            return Response({"error": "Too many attempts. Try again in a few minutes."}, status=429)
        raw = re.sub(r"[^A-Z0-9]", "", str(request.data.get("code", "")).upper())
        code = f"{raw[:4]}-{raw[4:]}"
        with transaction.atomic():
            asset = Asset.objects.select_for_update().filter(enroll_code=code).first() if len(raw) == 8 else None
            if not asset:
                return Response({"error": "Enrollment code not found or already used. Ask your admin for a new code."}, status=404)
            token = asset.enroll()
            asset.hostname = clip(request.data.get("hostname"), 255)
            asset.os = clip(request.data.get("os"), 120)
            asset.platform = clip(request.data.get("platform"), 20)
            asset.agent_version = clip(request.data.get("agentVersion"), 40)
            asset.save()
        return Response({"token": token, "asset": asset.public(), "policy": current_policy()})


class HeartbeatView(AgentView):
    def post(self, request):
        asset: Asset = request.auth
        d = request.data
        current = d.get("current") if isinstance(d.get("current"), dict) else None
        running = d.get("runningApps") if isinstance(d.get("runningApps"), list) else []
        asset.last_seen = timezone.now()
        asset.state = clip(d.get("state") or "tracking", 20)
        asset.current_app_id = clip(current.get("id"), 255) if current else ""
        asset.current_app_name = clip(current.get("name"), 255) if current else ""
        asset.session_start = from_ms(d.get("sessionStart"))
        asset.running_apps = [{"id": clip(a.get("id"), 255), "name": clip(a.get("name"), 255)} for a in running[:200] if isinstance(a, dict)]
        try:
            asset.today_seconds = max(0.0, float(d.get("todaySeconds") or 0))
        except (TypeError, ValueError):
            asset.today_seconds = 0
        for field, key, n in (("hostname", "hostname", 255), ("os", "os", 120), ("platform", "platform", 20), ("agent_version", "agentVersion", 40)):
            if d.get(key):
                setattr(asset, field, clip(d.get(key), n))
        asset.save()
        return Response({"policy": current_policy(), "asset": asset.public(), "serverTime": int(time.time() * 1000)})


class UsageView(AgentView):
    def post(self, request):
        asset: Asset = request.auth
        days = request.data.get("days")
        if not isinstance(days, dict):
            return Response({"error": "days must be an object"}, status=400)
        with transaction.atomic():
            for key, apps in days.items():
                if not DAY_RE.match(str(key)) or not isinstance(apps, list):
                    continue
                day = date.fromisoformat(key)
                DailyUsage.objects.filter(asset=asset, day=day).delete()
                rows = {}
                for a in apps[:2000]:
                    if not isinstance(a, dict) or not a.get("id"):
                        continue
                    try:
                        seconds = float(a.get("seconds") or 0)
                    except (TypeError, ValueError):
                        continue
                    if seconds <= 0:
                        continue
                    app_id = clip(a["id"], 255)
                    rows[app_id] = DailyUsage(
                        asset=asset, day=day, app_id=app_id, app_name=clip(a.get("name") or a["id"], 255),
                        seconds=seconds, sessions=max(0, int(a.get("sessions") or 0)),
                        activations=max(0, int(a.get("activations") or 0)), last_active=from_ms(a.get("lastActive")),
                    )
                DailyUsage.objects.bulk_create(rows.values())
        return Response({"ok": True})


class InstalledView(AgentView):
    def post(self, request):
        apps = request.data.get("apps") if isinstance(request.data.get("apps"), list) else []
        clean = [
            {
                "id": clip(a.get("id"), 255), "name": clip(a.get("name"), 255), "version": clip(a.get("version"), 60),
                "publisher": clip(a.get("publisher"), 120), "lastUsed": a.get("lastUsed") if isinstance(a.get("lastUsed"), (int, float)) else None,
                "useCount": a.get("useCount") if isinstance(a.get("useCount"), int) else None, "isSystem": bool(a.get("isSystem")),
            }
            for a in apps[:3000] if isinstance(a, dict)
        ]
        InstalledApps.objects.update_or_create(asset=request.auth, defaults={"apps": clean})
        return Response({"ok": True})


class AlertsView(AgentView):
    def post(self, request):
        asset: Asset = request.auth
        alerts = request.data.get("alerts") if isinstance(request.data.get("alerts"), list) else []
        received = []
        for a in alerts[:100]:
            if not isinstance(a, dict) or not a.get("uid"):
                continue
            uid = clip(a["uid"], 64)
            received.append(uid)
            WeekendAlert.objects.get_or_create(
                uid=uid,
                defaults={
                    "asset": asset,
                    "created_at": from_ms(a.get("createdAt")) or timezone.now(),
                    "day": clip(a.get("day"), 10),
                    "app_id": clip(a.get("appId"), 255),
                    "app_name": clip(a.get("appName"), 255),
                    "reason": clip(a.get("reason"), 200),
                    "details": clip(a.get("details"), 2000),
                    "approver": clip(a.get("approver"), 200),
                    "duration": clip(a.get("duration"), 50),
                    "dismissed": bool(a.get("dismissed")),
                },
            )
        return Response({"ok": True, "received": received})

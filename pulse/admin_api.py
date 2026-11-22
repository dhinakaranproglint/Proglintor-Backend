"""API used by the Proglintor Admin desktop app (staff users, `Authorization: Token <key>`)."""
import csv
import io
import re
from datetime import datetime, time as dtime, timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count, Max, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AppIcon, Asset, DailyUsage, InstalledApps, Setting, SoftwareRequest, WeekendAlert, ms
from .policy import DEFAULT_POLICY, normalize_policy
from .software import close_open_requests

ASSET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
DEVICE_TYPES = {"Laptop", "Desktop", "Workstation", "Other"}


def days_back(n):
    return timezone.localdate() - timedelta(days=n)


def day_ms(day):
    return ms(datetime.combine(day, dtime.min, tzinfo=timezone.get_current_timezone()))


def app_totals(start, end, asset=None):
    qs = DailyUsage.objects.filter(day__range=(start, end))
    if asset:
        qs = qs.filter(asset=asset)
    rows = (
        qs.values("app_id")
        .annotate(name=Max("app_name"), seconds=Sum("seconds"), sessions=Sum("sessions"), assets=Count("asset", distinct=True), last=Max("last_active"))
        .order_by("-seconds")
    )
    return [
        {"id": r["app_id"], "name": r["name"], "seconds": r["seconds"], "sessions": r["sessions"], "assets": r["assets"], "lastActive": ms(r["last"])}
        for r in rows
    ]


def daily_series(days, asset=None):
    start = days_back(days - 1)
    qs = DailyUsage.objects.filter(day__range=(start, timezone.localdate()))
    if asset:
        qs = qs.filter(asset=asset)
    totals = {r["day"]: r["s"] for r in qs.values("day").annotate(s=Sum("seconds"))}
    return [{"date": day_ms(start + timedelta(days=i)), "seconds": totals.get(start + timedelta(days=i), 0)} for i in range(days)]


def assets_with_today(qs):
    today = {r["asset_id"]: r["s"] for r in DailyUsage.objects.filter(day=timezone.localdate()).values("asset_id").annotate(s=Sum("seconds"))}
    return [a.to_dict(today.get(a.asset_id, 0)) for a in qs]


def clean_asset_fields(data, partial=False, existing=None):
    """Validates admin input; returns model field values."""
    out = {}

    def text(key, field, n, required=False, label=None):
        if partial and key not in data:
            return
        value = str(data.get(key) or "").strip()
        if required and not value:
            raise ValidationError(f"{label} is required")
        out[field] = value[:n]

    text("employeeName", "employee_name", 120, required=True, label="Employee name")
    if not (partial and "employeeEmail" not in data):
        email = str(data.get("employeeEmail") or "").strip()
        try:
            validate_email(email)
        except ValidationError:
            raise ValidationError("Enter a valid employee email address")
        out["employee_email"] = email
    text("department", "department", 120)
    text("managerName", "manager_name", 120)
    if not (partial and "managerEmail" not in data):
        mgr = str(data.get("managerEmail") or "").strip()
        if mgr:
            try:
                validate_email(mgr)
            except ValidationError:
                raise ValidationError("Enter a valid email address for the reporting person")
        out["manager_email"] = mgr
    text("location", "location", 255)
    text("notes", "notes", 5000)
    if not (partial and "deviceType" not in data):
        device = str(data.get("deviceType") or "Laptop")
        out["device_type"] = device if device in DEVICE_TYPES else "Other"
    return out


# ---- auth ------------------------------------------------------------------------------

class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response({"ok": True})


class MeView(APIView):
    def get(self, request):
        u = request.user
        return Response({"username": u.get_username(), "name": u.get_full_name() or u.get_username()})


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({"ok": True, "service": "proglintor-backend"})


# ---- dashboard -------------------------------------------------------------------------

class DashboardView(APIView):
    def get(self, request):
        assets = assets_with_today(Asset.objects.all())
        return Response({
            "kpis": {
                "assets": len(assets),
                "enrolled": sum(1 for a in assets if a["enrolled"]),
                "online": sum(1 for a in assets if a["online"]),
                "activeToday": sum(a["todaySeconds"] for a in assets),
                "newAlerts": WeekendAlert.objects.filter(status=WeekendAlert.STATUS_NEW).count(),
                "pendingApprovals": SoftwareRequest.objects.filter(status="AWAITING_APPROVAL").count(),
            },
            "assets": assets,
            "topApps": app_totals(timezone.localdate(), timezone.localdate())[:8],
            "daily": daily_series(7),
            "recentAlerts": [a.to_dict() for a in WeekendAlert.objects.select_related("asset", "reviewed_by")[:5]],
        })


# ---- assets ------------------------------------------------------------------------------

class AssetListView(APIView):
    def get(self, request):
        return Response(assets_with_today(Asset.objects.all()))

    def post(self, request):
        asset_id = str(request.data.get("assetId") or "").strip()
        try:
            if not asset_id:
                raise ValidationError("Asset ID is required")
            if not ASSET_ID_RE.match(asset_id):
                raise ValidationError("Asset ID can use letters, numbers, dot, dash and underscore (2–40 characters)")
            if Asset.objects.filter(asset_id__iexact=asset_id).exists():
                raise ValidationError(f"Asset {asset_id} already exists")
            fields = clean_asset_fields(request.data)
        except ValidationError as e:
            return Response({"error": e.messages[0]}, status=400)
        asset = Asset(asset_id=asset_id, **fields)
        asset.issue_code()
        asset.save()
        return Response(asset.to_dict(), status=201)


class AssetDetailView(APIView):
    def get(self, request, asset_id):
        asset = get_object_or_404(Asset, asset_id=asset_id)
        today = timezone.localdate()
        installed = InstalledApps.objects.filter(asset=asset).first()
        return Response({
            "asset": assets_with_today([asset])[0],
            "todayApps": app_totals(today, today, asset),
            "weekApps": app_totals(days_back(6), today, asset),
            "daily": daily_series(14, asset),
            "installed": {"apps": installed.apps if installed else [], "updatedAt": ms(installed.updated_at) if installed else None},
            "alerts": [a.to_dict() for a in asset.alerts.select_related("reviewed_by")[:50]],
        })

    def patch(self, request, asset_id):
        asset = get_object_or_404(Asset, asset_id=asset_id)
        try:
            fields = clean_asset_fields(request.data, partial=True)
        except ValidationError as e:
            return Response({"error": e.messages[0]}, status=400)
        for k, v in fields.items():
            setattr(asset, k, v)
        asset.save()
        return Response(asset.to_dict())

    def delete(self, request, asset_id):
        get_object_or_404(Asset, asset_id=asset_id).delete()
        return Response(status=204)


class AssetResetCodeView(APIView):
    def post(self, request, asset_id):
        asset = get_object_or_404(Asset, asset_id=asset_id)
        asset.issue_code()
        asset.save()
        close_open_requests(asset, f"admin:{request.user.get_username()}")  # nothing waiting may follow the asset to its next laptop
        return Response(asset.to_dict())


# ---- alerts ------------------------------------------------------------------------------

class AlertListView(APIView):
    def get(self, request):
        qs = WeekendAlert.objects.select_related("asset", "reviewed_by")
        status = request.query_params.get("status", "all")
        if status in (WeekendAlert.STATUS_NEW, WeekendAlert.STATUS_REVIEWED):
            qs = qs.filter(status=status)
        return Response([a.to_dict() for a in qs[:500]])


class AlertDetailView(APIView):
    def patch(self, request, uid):
        alert = get_object_or_404(WeekendAlert.objects.select_related("asset"), uid=uid)
        status = request.data.get("status")
        if status in (WeekendAlert.STATUS_NEW, WeekendAlert.STATUS_REVIEWED):
            alert.status = status
            alert.reviewed_at = timezone.now() if status == WeekendAlert.STATUS_REVIEWED else None
            alert.reviewed_by = request.user if status == WeekendAlert.STATUS_REVIEWED else None
        if isinstance(request.data.get("adminNote"), str):
            alert.admin_note = request.data["adminNote"][:2000]
        alert.save()
        return Response(alert.to_dict())


# ---- policy & export -------------------------------------------------------------------------

class PolicyView(APIView):
    def get(self, request):
        return Response({"policy": normalize_policy(Setting.get("policy", DEFAULT_POLICY)), "defaults": DEFAULT_POLICY})

    def put(self, request):
        policy = normalize_policy(request.data)
        Setting.put("policy", policy)
        return Response(policy)


class ExportView(APIView):
    def get(self, request):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "asset_id", "employee", "email", "app_id", "app_name", "active_seconds", "sessions"])
        for u in DailyUsage.objects.select_related("asset").order_by("day", "asset_id", "-seconds").iterator():
            w.writerow([u.day.isoformat(), u.asset_id, u.asset.employee_name, u.asset.employee_email, u.app_id, u.app_name, round(u.seconds), u.sessions])
        return HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")


class IconsView(APIView):
    """POST {ids: [...]} -> {icons: {id: data-url}} for the ids that have an icon."""

    def post(self, request):
        ids = [str(i)[:255] for i in (request.data.get("ids") or [])[:300] if i]
        return Response({"icons": {i.app_id: i.data_url() for i in AppIcon.objects.filter(app_id__in=ids)}})

"""Imports assets, usage and alerts from the earlier desktop-only admin (pulse-admin.db, SQLite).

    python manage.py import_desktop_admin "~/Library/Application Support/Proglintor Admin/pulse-admin.db"

Assets keep their enrollment codes (laptops that never connected can still use them). Laptops that were
already connected must enroll again with a new code, because agent tokens are not copied.
"""
import json
import sqlite3
from datetime import date, datetime, timezone as dt_timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pulse.models import Asset, DailyUsage, InstalledApps, Setting, WeekendAlert
from pulse.policy import normalize_policy


def dt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc) if ms else None


class Command(BaseCommand):
    help = "Import data from the desktop admin's SQLite database (pulse-admin.db)."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, path, **opts):
        file = Path(path).expanduser()
        if not file.exists():
            raise CommandError(f"Not found: {file}")
        con = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        counts = {"assets": 0, "usage": 0, "alerts": 0}
        with transaction.atomic():
            for r in con.execute("SELECT * FROM assets"):
                if Asset.objects.filter(asset_id__iexact=r["asset_id"]).exists():
                    self.stdout.write(f"  skip {r['asset_id']} (already exists)")
                    continue
                Asset.objects.create(
                    asset_id=r["asset_id"], employee_name=r["employee_name"], employee_email=r["employee_email"],
                    department=r["department"] or "", location=r["location"] or "", device_type=r["device_type"] or "Laptop",
                    notes=r["notes"] or "", enroll_code=r["enroll_code"] or None, hostname=r["hostname"] or "",
                    os=r["os"] or "", platform=r["platform"] or "", agent_version=r["agent_version"] or "",
                )
                counts["assets"] += 1
            for r in con.execute("SELECT * FROM usage"):
                DailyUsage.objects.update_or_create(
                    asset_id=r["asset_id"], day=date.fromisoformat(r["day"]), app_id=r["app_id"],
                    defaults={"app_name": r["app_name"], "seconds": r["seconds"], "sessions": r["sessions"] or 0,
                              "activations": r["activations"] or 0, "last_active": dt(r["last_active"])},
                )
                counts["usage"] += 1
            for r in con.execute("SELECT * FROM installed"):
                InstalledApps.objects.update_or_create(asset_id=r["asset_id"], defaults={"apps": json.loads(r["apps"])})
            for r in con.execute("SELECT * FROM alerts"):
                _, created = WeekendAlert.objects.get_or_create(uid=r["uid"], defaults={
                    "asset_id": r["asset_id"], "created_at": dt(r["created_at"]), "day": r["day"] or "",
                    "app_id": r["app_id"] or "", "app_name": r["app_name"] or "", "reason": r["reason"] or "",
                    "details": r["details"] or "", "approver": r["approver"] or "", "duration": r["duration"] or "",
                    "dismissed": bool(r["dismissed"]), "status": r["status"] or "new", "admin_note": r["admin_note"] or "",
                })
                counts["alerts"] += int(created)
            row = con.execute("SELECT value FROM settings WHERE key = 'policy'").fetchone()
            if row and not Setting.get("policy"):
                Setting.put("policy", normalize_policy(json.loads(row["value"])))
        self.stdout.write(self.style.SUCCESS(f"Imported {counts['assets']} assets, {counts['usage']} usage rows, {counts['alerts']} alerts."))

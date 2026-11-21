import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no look-alikes (0/O, 1/I/L)


def new_enroll_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ms(dt):
    """datetime -> epoch milliseconds (the format the desktop apps use)."""
    return int(dt.timestamp() * 1000) if dt else None


class Asset(models.Model):
    """A company laptop/desktop assigned to an employee."""

    asset_id = models.CharField(primary_key=True, max_length=40)
    employee_name = models.CharField(max_length=120)
    employee_email = models.EmailField()
    department = models.CharField(max_length=120, blank=True)
    location = models.CharField("office / address", max_length=255, blank=True)
    device_type = models.CharField(max_length=30, default="Laptop")
    notes = models.TextField(blank=True)

    enroll_code = models.CharField(max_length=9, unique=True, null=True, blank=True)
    token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    enrolled_at = models.DateTimeField(null=True, blank=True)

    hostname = models.CharField(max_length=255, blank=True)
    os = models.CharField(max_length=120, blank=True)
    platform = models.CharField(max_length=20, blank=True)
    agent_version = models.CharField(max_length=40, blank=True)

    last_seen = models.DateTimeField(null=True, blank=True, db_index=True)
    state = models.CharField(max_length=20, blank=True)
    current_app_id = models.CharField(max_length=255, blank=True)
    current_app_name = models.CharField(max_length=255, blank=True)
    session_start = models.DateTimeField(null=True, blank=True)
    running_apps = models.JSONField(default=list, blank=True)
    today_seconds = models.FloatField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["asset_id"]

    def __str__(self):
        return f"{self.asset_id} — {self.employee_name}"

    # ---- enrollment ----
    def issue_code(self):
        """New one-time code; any linked laptop is disconnected."""
        self.enroll_code = new_enroll_code()
        self.token_hash = None
        return self.enroll_code

    def enroll(self):
        token = secrets.token_hex(32)
        self.token_hash = hash_token(token)
        self.enroll_code = None  # single use
        self.enrolled_at = timezone.now()
        self.last_seen = timezone.now()
        self.state = "starting"
        return token

    @property
    def enrolled(self):
        return bool(self.token_hash)

    @property
    def online(self):
        return bool(self.last_seen and timezone.now() - self.last_seen < timedelta(seconds=settings.PULSE_ONLINE_SECONDS))

    def public(self):
        """Fields sent to the agent."""
        return {
            "assetId": self.asset_id,
            "employeeName": self.employee_name,
            "employeeEmail": self.employee_email,
            "department": self.department,
            "location": self.location,
            "deviceType": self.device_type,
            "enrolledAt": ms(self.enrolled_at),
        }

    def to_dict(self, today_seconds=None):
        """Shape used by the admin desktop app."""
        online = self.online
        if not self.enrolled:
            state = "not-enrolled"
        elif not online:
            state = "offline"
        else:
            state = self.state or "tracking"
        reported_today = (
            self.today_seconds
            if self.last_seen and timezone.localdate(self.last_seen) == timezone.localdate()
            else 0
        )
        return {
            "assetId": self.asset_id,
            "employeeName": self.employee_name,
            "employeeEmail": self.employee_email,
            "department": self.department,
            "location": self.location,
            "deviceType": self.device_type,
            "notes": self.notes,
            "enrollCode": self.enroll_code,
            "enrolled": self.enrolled,
            "enrolledAt": ms(self.enrolled_at),
            "hostname": self.hostname,
            "os": self.os,
            "platform": self.platform,
            "agentVersion": self.agent_version,
            "lastSeen": ms(self.last_seen),
            "online": online,
            "state": state,
            "current": (
                {"id": self.current_app_id, "name": self.current_app_name, "sessionStart": ms(self.session_start)}
                if online and self.current_app_id
                else None
            ),
            "runningApps": self.running_apps if online else [],
            "todaySeconds": max(reported_today, today_seconds or 0),
            "createdAt": ms(self.created_at),
        }


class DailyUsage(models.Model):
    """Foreground time per app per day, as reported by the agent (whole days are replaced)."""

    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="usage")
    day = models.DateField()
    app_id = models.CharField(max_length=255)
    app_name = models.CharField(max_length=255)
    seconds = models.FloatField()
    sessions = models.PositiveIntegerField(default=0)
    activations = models.PositiveIntegerField(default=0)
    last_active = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["asset", "day", "app_id"], name="usage_unique_asset_day_app")]
        indexes = [models.Index(fields=["day"]), models.Index(fields=["asset", "day"])]
        verbose_name_plural = "daily usage"

    def __str__(self):
        return f"{self.asset_id} {self.day} {self.app_name} {round(self.seconds)}s"


class InstalledApps(models.Model):
    asset = models.OneToOneField(Asset, on_delete=models.CASCADE, primary_key=True, related_name="installed")
    apps = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "installed apps"


class WeekendAlert(models.Model):
    STATUS_NEW = "new"
    STATUS_REVIEWED = "reviewed"
    STATUS_CHOICES = [(STATUS_NEW, "New"), (STATUS_REVIEWED, "Reviewed")]

    uid = models.CharField(primary_key=True, max_length=64)  # generated by the agent (idempotent uploads)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="alerts")
    created_at = models.DateTimeField(db_index=True)
    day = models.CharField(max_length=10, blank=True)
    app_id = models.CharField(max_length=255, blank=True)
    app_name = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=200, blank=True)
    details = models.TextField(blank=True)
    approver = models.CharField(max_length=200, blank=True)
    duration = models.CharField(max_length=50, blank=True)
    dismissed = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    admin_note = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.asset_id} {self.app_name} {self.created_at:%Y-%m-%d %H:%M}"

    def to_dict(self):
        return {
            "uid": self.uid,
            "assetId": self.asset_id,
            "employeeName": self.asset.employee_name if self.asset_id else "",
            "createdAt": ms(self.created_at),
            "day": self.day,
            "appId": self.app_id,
            "appName": self.app_name,
            "reason": self.reason,
            "details": self.details,
            "approver": self.approver,
            "duration": self.duration,
            "dismissed": self.dismissed,
            "status": self.status,
            "adminNote": self.admin_note,
            "reviewedAt": ms(self.reviewed_at),
            "reviewedBy": self.reviewed_by.get_username() if self.reviewed_by_id else None,
        }


class Setting(models.Model):
    """Key/value settings (e.g. the weekend policy)."""

    key = models.CharField(primary_key=True, max_length=60)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls, key, default=None):
        row = cls.objects.filter(key=key).first()
        return row.value if row else default

    @classmethod
    def put(cls, key, value):
        cls.objects.update_or_create(key=key, defaults={"value": value})


class PasswordResetCode(models.Model):
    """One-time 6-digit code e-mailed to an admin who forgot their password."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reset_codes")
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    @property
    def valid(self):
        return not self.used and self.attempts < 5 and timezone.now() < self.expires_at


class AppIcon(models.Model):
    """App icon uploaded by agents (shared by every asset that uses the same app id)."""

    app_id = models.CharField(primary_key=True, max_length=255)
    data = models.BinaryField()
    content_type = models.CharField(max_length=40, default="image/png")
    updated_at = models.DateTimeField(auto_now=True)

    def data_url(self):
        import base64
        return f"data:{self.content_type};base64,{base64.b64encode(bytes(self.data)).decode()}"

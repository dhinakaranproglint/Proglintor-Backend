import hashlib
import secrets
import uuid
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


class Employee(models.Model):
    """A person onboarded by the admin. Assets are assigned to an employee; the reporting person lives here."""

    employee_id = models.CharField("employee ID", max_length=40, unique=True)
    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    department = models.CharField(max_length=120, blank=True)
    designation = models.CharField(max_length=120, blank=True)
    manager = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reports", verbose_name="reporting person")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "employee_id"]

    def __str__(self):
        return f"{self.name} ({self.employee_id})"

    def sync_assets(self):
        """Assets keep a copy of these fields (Orbit and the agent read them); refresh the copies."""
        for a in self.assets.all():
            a.apply_employee(self)
            a.save()

    def to_dict(self):
        return {
            "id": self.pk,
            "employeeId": self.employee_id,
            "name": self.name,
            "email": self.email,
            "department": self.department,
            "designation": self.designation,
            "managerId": self.manager_id,
            "managerName": self.manager.name if self.manager else "",
            "managerEmployeeId": self.manager.employee_id if self.manager else "",
            "active": self.active,
            "assetIds": list(self.assets.values_list("asset_id", flat=True)),
            "reportsCount": self.reports.count(),
            "createdAt": ms(self.created_at),
        }


class Asset(models.Model):
    """A company laptop/desktop assigned to an employee."""

    asset_id = models.CharField(primary_key=True, max_length=40)
    employee = models.ForeignKey(Employee, null=True, blank=True, on_delete=models.SET_NULL, related_name="assets")
    employee_name = models.CharField(max_length=120)
    employee_email = models.EmailField()
    department = models.CharField(max_length=120, blank=True)
    manager_name = models.CharField("reporting person", max_length=120, blank=True)
    manager_email = models.EmailField("reporting person email", blank=True)
    location = models.CharField("office / address", max_length=255, blank=True)
    device_type = models.CharField(max_length=30, default="Laptop")
    notes = models.TextField(blank=True)

    enroll_code = models.CharField(max_length=9, unique=True, null=True, blank=True)
    token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    enrolled_at = models.DateTimeField(null=True, blank=True)

    hostname = models.CharField(max_length=255, blank=True)
    os = models.CharField(max_length=120, blank=True)
    platform = models.CharField(max_length=20, blank=True)
    arch = models.CharField(max_length=20, blank=True)
    local_admin = models.BooleanField(null=True, blank=True)  # is the laptop's signed-in user a local administrator? None = unknown
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

    def apply_employee(self, emp):
        """Copy the onboarded employee's details (including the reporting person) onto this asset."""
        self.employee = emp
        self.employee_name = emp.name
        self.employee_email = emp.email
        self.department = emp.department
        self.manager_name = emp.manager.name if emp.manager else ""
        self.manager_email = emp.manager.email if emp.manager else ""

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
            "managerName": self.manager_name,
            "managerEmail": self.manager_email,
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
            "employeeRef": self.employee_id,
            "employeeCode": self.employee.employee_id if self.employee else "",
            "employeeName": self.employee_name,
            "employeeEmail": self.employee_email,
            "department": self.department,
            "managerName": self.manager_name,
            "location": self.location,
            "deviceType": self.device_type,
            "notes": self.notes,
            "enrollCode": self.enroll_code,
            "enrolled": self.enrolled,
            "enrolledAt": ms(self.enrolled_at),
            "hostname": self.hostname,
            "os": self.os,
            "platform": self.platform,
            "arch": self.arch,
            "localAdmin": self.local_admin,
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


# ---- software catalogue & install requests ---------------------------------------------------

REQUEST_ACTIVE = ("QUEUED", "DISPATCHED", "RUNNING", "VERIFYING")
REQUEST_OPEN = ("AWAITING_APPROVAL",) + REQUEST_ACTIVE


class SoftwareItem(models.Model):
    """One approved, pinned installer in the company catalogue (one row per platform build)."""

    PLATFORMS = [("darwin", "macOS"), ("win32", "Windows"), ("linux", "Linux")]
    ARCHS = [("any", "Any"), ("arm64", "Apple Silicon / ARM64"), ("x64", "Intel / x64")]
    INSTALLERS = [("dmg", "dmg"), ("zip", "zip"), ("pkg", "pkg"), ("msi", "msi"), ("exe", "exe"), ("appimage", "AppImage")]

    slug = models.CharField(primary_key=True, max_length=60)
    name = models.CharField(max_length=120)
    publisher = models.CharField(max_length=120, blank=True)
    version = models.CharField(max_length=40)
    description = models.CharField(max_length=300, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    platform = models.CharField(max_length=10, choices=PLATFORMS)
    arch = models.CharField(max_length=10, choices=ARCHS, default="any")
    installer_type = models.CharField(max_length=10, choices=INSTALLERS)
    download_url = models.URLField(max_length=1000)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    app_id = models.CharField("bundle id / display name", max_length=255, blank=True)
    app_name = models.CharField("app bundle name (e.g. Foo.app)", max_length=255, blank=True)
    team_id = models.CharField("Apple Team ID", max_length=20, blank=True)
    silent_args = models.JSONField(default=list, blank=True)
    icon = models.TextField(blank=True)  # small PNG data URL read from the app bundle when the entry was checked
    requires_approval = models.BooleanField(default=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "platform"]

    def __str__(self):
        return f"{self.name} {self.version} ({self.platform})"

    def spec(self):
        """The exact, pinned install instructions copied into a request (what the approver reviewed)."""
        return {
            "slug": self.slug, "name": self.name, "version": self.version, "publisher": self.publisher,
            "platform": self.platform, "arch": self.arch, "installerType": self.installer_type,
            "downloadUrl": self.download_url, "sha256": self.sha256, "sizeBytes": self.size_bytes,
            "appId": self.app_id, "appName": self.app_name, "teamId": self.team_id, "silentArgs": list(self.silent_args or []),
        }

    def to_dict(self, with_icon=False):
        """Admin view. The icon (up to ~100 KB) is left out of lists: they say `hasIcon` and the client fetches icons it doesn't have
        yet from /api/admin/software/icons, keyed by slug + updatedAt."""
        return {
            "slug": self.slug, "name": self.name, "publisher": self.publisher, "version": self.version,
            "description": self.description, "aliases": self.aliases or [], "platform": self.platform, "arch": self.arch,
            "installerType": self.installer_type, "downloadUrl": self.download_url, "sha256": self.sha256,
            "sizeBytes": self.size_bytes, "appId": self.app_id, "appName": self.app_name, "teamId": self.team_id,
            "silentArgs": self.silent_args or [], "requiresApproval": self.requires_approval, "enabled": self.enabled,
            "hasIcon": bool(self.icon), **({"icon": self.icon or None} if with_icon else {}), "updatedAt": ms(self.updated_at),
        }

    def to_public(self):
        """What an employee's device may see (no download URL or digest)."""
        return {
            "slug": self.slug, "name": self.name, "publisher": self.publisher, "version": self.version,
            "description": self.description, "requiresApproval": self.requires_approval,
        }


class SoftwareRequest(models.Model):
    """An employee's request to install something on their asset, from chat to verified install."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="software_requests")
    idempotency_key = models.CharField(max_length=64)
    raw_request = models.CharField(max_length=500)
    software = models.ForeignKey(SoftwareItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="requests")
    software_name = models.CharField(max_length=120, blank=True)
    version = models.CharField(max_length=40, blank=True)
    spec = models.JSONField(default=dict, blank=True)  # pinned at request time
    kind = models.CharField(max_length=10, default="catalogue")  # "new" = software not in the catalogue yet (see proposals.py)
    proposal = models.JSONField(default=dict, blank=True)  # AI-suggested source + licence + pre-checks, for the admin to review

    status = models.CharField(max_length=30, db_index=True)
    decision_reason = models.CharField(max_length=60, blank=True)
    explanation = models.CharField(max_length=600, blank=True)
    request_version = models.PositiveIntegerField(default=1)  # optimistic concurrency for approvals / cancel

    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_comment = models.CharField(max_length=500, blank=True)

    progress_phase = models.CharField(max_length=20, blank=True)
    progress_bytes_done = models.BigIntegerField(null=True, blank=True)
    progress_bytes_total = models.BigIntegerField(null=True, blank=True)
    progress_updated_at = models.DateTimeField(null=True, blank=True)
    job_claimed_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["asset", "idempotency_key"], name="software_request_idem")]

    def __str__(self):
        return f"{self.asset_id} {self.software_name or self.raw_request} {self.status}"

    def to_dict(self, replayed=False):
        """Shape sent to the employee's agent."""
        return {
            "id": str(self.id), "status": self.status, "decisionReason": self.decision_reason or None,
            "rawRequest": self.raw_request, "softwareSlug": self.software_id, "softwareName": self.software_name or None,
            "version": self.version or None, "explanation": self.explanation or None, "requestVersion": self.request_version,
            "replayed": replayed, "createdAt": ms(self.created_at), "updatedAt": ms(self.updated_at),
            "decisionComment": self.decision_comment or None, "kind": self.kind,
            "proposal": self.proposal_public() if self.kind == "new" else None,
            "decidedBy": self.decided_by.get_full_name() or self.decided_by.get_username() if self.decided_by_id else None,
            "progressPhase": self.progress_phase or None, "progressBytesDone": self.progress_bytes_done,
            "progressBytesTotal": self.progress_bytes_total, "progressUpdatedAt": ms(self.progress_updated_at),
            "jobClaimedAt": ms(self.job_claimed_at), "finishedAt": ms(self.finished_at),
        }

    def proposal_public(self):
        """What the employee sees about a new-software proposal (no URLs: those are for the admin to check)."""
        p = self.proposal or {}
        return {"name": p.get("name"), "vendor": p.get("vendor"), "host": p.get("host"), "licenseKind": p.get("licenseKind"),
                "licenseLabel": p.get("licenseLabel")}

    def to_admin(self, icon=False):
        """Admin view. `icon=True` (the pending-approvals list only) adds the app's icon; it needs `software` to be loaded with the row."""
        a = self.asset
        spec = self.spec or {}
        return {
            **self.to_dict(),
            "assetId": a.asset_id, "employeeName": a.employee_name, "employeeEmail": a.employee_email,
            "department": a.department, "hostname": a.hostname, "deviceOnline": a.online,
            "publisher": spec.get("publisher", ""), "installerType": spec.get("installerType", ""),
            "sha256Prefix": (spec.get("sha256") or "")[:12], "sizeBytes": spec.get("sizeBytes"),
            **({"icon": (self.software.icon or None) if self.software_id else None} if icon else {}),
            "assetPlatform": a.platform, "assetArch": a.arch, "assetLocalAdmin": a.local_admin,
            "proposal": self.proposal if self.kind == "new" else None,
        }


class SoftwareEvent(models.Model):
    """Append-only audit trail: who asked, who approved, what ran."""

    request = models.ForeignKey(SoftwareRequest, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    actor = models.CharField(max_length=160)  # "employee:PAP080", "admin:alice", "agent:PAP080", "system"
    kind = models.CharField(max_length=40)
    detail = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["at", "id"]

    def to_dict(self):
        return {"at": ms(self.at), "actor": self.actor, "kind": self.kind, "detail": self.detail}


# ---- Orbit: tasks, recognitions ---------------------------------------------------------------------

class Task(models.Model):
    """A piece of work an admin assigned to one laptop's employee. Assigning to several people creates one row each (sharing `group`),
    so every query is naturally scoped to one employee."""
    PRIORITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent")]
    STATUSES = [("todo", "To do"), ("in_progress", "In progress"), ("blocked", "Blocked"), ("done", "Done")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.UUIDField(default=uuid.uuid4, db_index=True)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=1000, blank=True)
    priority = models.CharField(max_length=10, choices=PRIORITIES, default="medium")
    status = models.CharField(max_length=12, choices=STATUSES, default="todo", db_index=True)
    percent = models.PositiveSmallIntegerField(default=0)
    estimated_hours = models.FloatField(default=0)
    due_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_date", "-created_at"]

    def __str__(self):
        return f"{self.title} ({self.asset_id})"

    def to_dict(self, admin=False):
        out = {
            "id": str(self.id), "group": str(self.group), "title": self.title, "description": self.description,
            "priority": self.priority, "status": self.status, "percent": self.percent, "estimatedHours": self.estimated_hours,
            "dueDate": self.due_date.isoformat() if self.due_date else None, "createdAt": ms(self.created_at),
            "completedAt": ms(self.completed_at),
        }
        if admin:
            out.update({"assetId": self.asset_id, "employeeName": self.asset.employee_name,
                        "createdBy": self.created_by.get_username() if self.created_by_id else None})
        return out


class Recognition(models.Model):
    """Appreciation for good work. Orbit (or the employee, or an admin) proposes it; an admin approves it, gives a reward and credits;
    the employee then gets a certificate. Nothing is awarded without an admin's decision."""
    KINDS = [("delivery", "Delivery"), ("early", "Delivered early"), ("weekend", "Weekend / extra hours"), ("focus", "Focus"),
             ("extra", "Extra effort"), ("kudos", "Kudos"), ("month", "Employee of the Month")]
    SOURCES = [("orbit", "Orbit"), ("employee", "Employee"), ("admin", "Admin")]
    STATUSES = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Not this time")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="recognitions")
    kind = models.CharField(max_length=12, choices=KINDS)
    source = models.CharField(max_length=10, choices=SOURCES, default="orbit")
    title = models.CharField(max_length=120)
    citation = models.CharField(max_length=600, blank=True)
    evidence = models.JSONField(default=dict, blank=True)  # numbers computed by the server, shown to the admin as proof
    dedupe_key = models.CharField(max_length=80, blank=True)  # the same achievement can't be nominated twice
    status = models.CharField(max_length=10, choices=STATUSES, default="pending", db_index=True)
    credits = models.FloatField(default=0)
    reward = models.CharField(max_length=200, blank=True)
    admin_note = models.CharField(max_length=500, blank=True)
    certificate_id = models.CharField(max_length=24, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["asset", "dedupe_key"], condition=~models.Q(dedupe_key=""), name="recognition_once_per_achievement")]

    def to_dict(self, admin=False):
        out = {
            "id": str(self.id), "kind": self.kind, "source": self.source, "title": self.title, "citation": self.citation,
            "evidence": self.evidence, "status": self.status, "credits": self.credits, "reward": self.reward,
            "adminNote": self.admin_note, "certificateId": self.certificate_id,
            "createdAt": ms(self.created_at), "decidedAt": ms(self.decided_at),
        }
        if admin:
            out.update({"assetId": self.asset_id, "employeeName": self.asset.employee_name, "department": self.asset.department,
                        "decidedBy": self.decided_by.get_username() if self.decided_by_id else None})
        return out


# ---- Timesheets and monthly awards ------------------------------------------------------------------

class TaskEvent(models.Model):
    """A point in a task's life (created, progress, done). Timesheets are built from these plus the laptop's real tracked time."""
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="task_events")
    at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=12)
    percent = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["at"]
        indexes = [models.Index(fields=["asset", "at"])]


class TimesheetSummary(models.Model):
    """The AI (or template) summary of one person's week or month. Kept so the employee and the admin read exactly the same words."""
    PERIODS = [("week", "Week"), ("month", "Month")]
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="timesheet_summaries")
    period = models.CharField(max_length=5, choices=PERIODS)
    start = models.DateField()
    digest = models.CharField(max_length=40)   # of the facts it was written from; a change in the data means a fresh summary
    text = models.TextField()
    source = models.CharField(max_length=10, default="template")  # "llm" | "template"
    created_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["asset", "period", "start"], name="timesheet_summary_once")]


class MonthlyAward(models.Model):
    """The admin's choice of Employee of the Month. One per month; the certificate is a normal approved Recognition."""
    month = models.CharField(max_length=7, unique=True)  # YYYY-MM
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="monthly_awards")
    recognition = models.OneToOneField(Recognition, on_delete=models.CASCADE, related_name="monthly_award")
    scores = models.JSONField(default=dict, blank=True)   # what the admin saw when choosing
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-month"]

    def to_dict(self, admin=True):
        return {"month": self.month, "assetId": self.asset_id, "employeeName": self.asset.employee_name, "department": self.asset.department,
                "recognitionId": str(self.recognition_id), "reward": self.recognition.reward, "citation": self.recognition.citation,
                "credits": self.recognition.credits, "certificateId": self.recognition.certificate_id, "decidedAt": ms(self.created_at),
                **({"scores": self.scores} if admin else {})}


class ManagerMessage(models.Model):
    """A message Orbit sent on an employee's behalf to their reporting person (for now: "I have no task"). The text is written by the server from a
    fixed template, never by the model, so it can't be talked into saying something else."""
    KINDS = [("no_tasks", "No tasks assigned")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="manager_messages")
    kind = models.CharField(max_length=12, choices=KINDS, default="no_tasks")
    body = models.CharField(max_length=800)
    to_name = models.CharField(max_length=120, blank=True)
    to_email = models.EmailField(blank=True)
    emailed = models.BooleanField(default=False)  # False when there was no address or the mail server refused it; the admin inbox still has it
    resolved = models.BooleanField(default=False, db_index=True)  # set when the admin gives them work or closes it
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def to_dict(self):
        return {"id": str(self.id), "assetId": self.asset_id, "employeeName": self.asset.employee_name, "department": self.asset.department,
                "kind": self.kind, "body": self.body, "toName": self.to_name, "toEmail": self.to_email, "emailed": self.emailed,
                "resolved": self.resolved, "createdAt": ms(self.created_at)}

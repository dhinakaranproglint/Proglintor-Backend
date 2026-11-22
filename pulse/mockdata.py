"""A month of realistic mock data for development and demos.

Everything is relative to "now", so a re-run keeps the data fresh: history ends yesterday and *today* is only as full as the working day
so far (a laptop at 11am has about 1.5 hours, not a full day). Generation is seeded per (person, day), so the same day always looks the
same. Nothing here talks to the network. Mock assets are marked (id prefix MOCK, note "mock-data") so they can be removed again.
"""
import random
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .models import Asset, DailyUsage, InstalledApps, Recognition, Task, TaskEvent, WeekendAlert

MARK = "mock-data"
WORK_START = 9.5   # hours; the working day starts around half past nine
WORK_LEN = 8.5

APPS = {
    "vscode": ("com.microsoft.VSCode", "Visual Studio Code"), "terminal": ("com.apple.Terminal", "Terminal"),
    "chrome": ("com.google.Chrome", "Google Chrome"), "slack": ("com.tinyspeck.slackmacgap", "Slack"),
    "teams": ("com.microsoft.teams2", "Microsoft Teams"), "outlook": ("com.microsoft.Outlook", "Microsoft Outlook"),
    "figma": ("com.figma.Desktop", "Figma"), "postman": ("com.postmanlabs.mac", "Postman"), "docker": ("com.docker.docker", "Docker Desktop"),
    "excel": ("com.microsoft.Excel", "Microsoft Excel"), "word": ("com.microsoft.Word", "Microsoft Word"), "notion": ("notion.id", "Notion"),
    "zoom": ("us.zoom.xos", "zoom.us"), "intellij": ("com.jetbrains.intellij", "IntelliJ IDEA"), "xcode": ("com.apple.dt.Xcode", "Xcode"),
    "sublime": ("com.sublimetext.4", "Sublime Text"), "tableplus": ("com.tinyapp.TablePlus", "TablePlus"), "jira": ("com.atlassian.jira", "Jira"),
}

# weights add to roughly 1; "meet" is time in meeting apps
PERSONAS = [
    dict(key="asha", name="Asha Rao", title="Backend Engineer", dept="Engineering", hours=6.4, weekend=0.10,
         apps={"vscode": .36, "terminal": .12, "chrome": .18, "slack": .08, "teams": .10, "postman": .06, "docker": .05, "outlook": .05},
         tasks=["Fix login redirect bug", "Add rate limiting to the public API", "Write migration for invoice table", "Speed up the orders query",
                "Upgrade Django to 5.2", "Add audit log for admin actions", "Fix flaky payment webhook test", "Document the auth flow",
                "Cache the pricing lookup", "Handle timeouts in the sync job", "Add pagination to the activity feed", "Clean up unused endpoints",
                "Review the notifications design", "Improve error messages in checkout", "Set up Sentry alerts"]),
    dict(key="rahul", name="Rahul Sharma", title="Senior Backend Engineer", dept="Engineering", hours=6.9, weekend=0.14, meeting=0.30,
         apps={"vscode": .26, "terminal": .08, "chrome": .14, "slack": .08, "teams": .26, "outlook": .08, "intellij": .06, "postman": .04},
         tasks=["Migrate payment webhook to Service Bus", "Design the billing event schema", "Mentor session: code review basics", "Reduce p95 latency on search",
                "Plan the Q1 platform roadmap", "Investigate memory leak in the worker", "Write the incident postmortem", "Interview two backend candidates",
                "Refactor the retry layer", "Define SLOs for the orders service", "Review the infra cost report", "Upgrade Postgres to 16",
                "Harden the token refresh flow", "Spike: event sourcing for audit", "Pair with new joiners on onboarding tasks"]),
    dict(key="priya", name="Priya Patel", title="Core Systems Engineer", dept="Engineering", hours=7.2, weekend=0.08,
         apps={"vscode": .34, "terminal": .18, "chrome": .14, "slack": .06, "teams": .08, "docker": .08, "outlook": .04, "tableplus": .06},
         tasks=["Deploy PostgreSQL cluster upgrade", "Patch the TLS configuration", "Optimise the batch importer", "Add health checks to every service",
                "Rewrite the cron scheduler", "Fix the deadlock in the ledger job", "Benchmark the new cache layer", "Automate certificate renewal",
                "Add tracing to the gateway", "Reduce docker image size", "Write runbook for failover", "Review the security scan results",
                "Move secrets to the vault", "Load test the checkout path", "Tidy up the CI pipeline"]),
    dict(key="amit", name="Amit Verma", title="Tech Lead and Architect", dept="Engineering", hours=6.6, weekend=0.06, meeting=0.34,
         apps={"teams": .28, "vscode": .14, "chrome": .16, "slack": .10, "outlook": .12, "notion": .10, "terminal": .04, "word": .06},
         tasks=["Architecture review for the new ledger", "Approve the API versioning proposal", "Quarterly tech-debt plan", "Review Priya's deployment design",
                "Draft the DR readiness checklist", "Sync with product on the Q1 scope", "Write the engineering hiring plan", "Run the design review",
                "Estimate the mobile rewrite", "Review the incident action items", "Update the architecture decision records", "1:1 prep for the team",
                "Evaluate the observability vendors", "Align on the data retention policy", "Prepare the board tech update"]),
    dict(key="meera", name="Meera Nair", title="UI/UX Designer", dept="Design", hours=6.1, weekend=0.04,
         apps={"figma": .44, "chrome": .16, "slack": .08, "teams": .12, "notion": .08, "outlook": .05, "zoom": .07},
         tasks=["Redesign the onboarding flow", "Create the empty states", "Design the notifications centre", "Accessibility audit of checkout",
                "Update the design system buttons", "Prototype the dashboard filters", "Usability test synthesis", "Icon set for the mobile app",
                "Design the admin approvals screen", "Dark mode colour review", "Write the UX copy for errors", "Handoff specs for the settings page",
                "Competitor review: pricing pages", "Motion guidelines", "Design the invite teammates flow"]),
    dict(key="karthik", name="Karthik Iyer", title="QA Engineer", dept="Quality", hours=6.3, weekend=0.07,
         apps={"chrome": .30, "postman": .14, "vscode": .14, "terminal": .08, "slack": .08, "teams": .10, "excel": .08, "jira": .08},
         tasks=["Regression test for the release", "Automate the login smoke tests", "Verify the invoice export fixes", "Test the payment retries",
                "Write test plan for notifications", "Triage the new bug reports", "Performance test the search", "Cross-browser check of the dashboard",
                "Update the test data scripts", "Reproduce the flaky checkout bug", "Review test coverage gaps", "Exploratory test of the admin app",
                "Mobile device matrix testing", "Security test: session handling", "Close out the sprint bugs"]),
    dict(key="divya", name="Divya Menon", title="Product Manager", dept="Product", hours=6.0, weekend=0.05, meeting=0.36,
         apps={"teams": .30, "notion": .16, "chrome": .14, "outlook": .12, "slack": .10, "excel": .06, "word": .06, "zoom": .06},
         tasks=["Write the PRD for team workspaces", "Prioritise the Q1 backlog", "Customer interviews: churned accounts", "Review the analytics dashboard",
                "Define success metrics for onboarding", "Sprint planning prep", "Stakeholder update for leadership", "Pricing experiment proposal",
                "Triage support escalations", "Roadmap review with sales", "Draft the release notes", "Competitive teardown",
                "Refine the acceptance criteria", "Plan the beta programme", "Retro summary and actions"]),
    dict(key="sanjay", name="Sanjay Kumar", title="DevOps Engineer", dept="Platform", hours=6.8, weekend=0.26,
         apps={"terminal": .30, "chrome": .24, "vscode": .14, "slack": .08, "teams": .08, "docker": .08, "outlook": .04, "tableplus": .04},
         tasks=["Set up the staging environment", "Rotate the production credentials", "Fix the failing deploy pipeline", "Add autoscaling to the workers",
                "Reduce cloud spend on idle resources", "Migrate logs to the new stack", "Patch the base images", "Write Terraform for the new VPC",
                "Investigate the 3am alert", "Backup restore drill", "Upgrade Kubernetes nodes", "Add dashboards for queue depth",
                "Harden the bastion host", "Automate the release checklist", "On-call handover notes"]),
]

WEEKEND_REASONS = [("Production incident", "Resolved the database connection pool timeout"), ("Release support", "Supported the planned release window"),
                   ("Deadline", "Finished the migration before Monday's demo"), ("On-call", "Handled the overnight alert and verified the fix"),
                   ("Customer escalation", "Investigated a customer-facing outage")]
REWARDS = ["Comp-off day", "Gift card", "Learning budget", "Shout-out at the team meeting", "Bonus credits"]


def _rng(*parts):
    return random.Random("|".join(str(p) for p in parts))


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _day_seconds(persona, day, now, rng, leave_days):
    """Total active seconds for a person on a day (0 on weekends mostly, on leave, or before work starts today)."""
    weekend = day.weekday() >= 5
    if day in leave_days:
        return 0.0
    if weekend and rng.random() > persona["weekend"]:
        return 0.0
    hours = _clip(rng.gauss(persona["hours"] * (0.55 if weekend else 1.0) * (0.92 if day.weekday() == 4 else 1.0), 0.9), 1.0 if weekend else 3.2, 9.4)
    if day == now.date():
        elapsed = (now.hour + now.minute / 60) - WORK_START
        if elapsed <= 0:
            return 0.0
        hours *= _clip(elapsed / WORK_LEN, 0, 1) * rng.uniform(0.85, 1.0)
    return hours * 3600


def _split(persona, total, rng):
    weights = {k: max(0.005, w * rng.uniform(0.6, 1.5)) for k, w in persona["apps"].items()}
    s = sum(weights.values())
    return {k: total * w / s for k, w in weights.items()}


def seed_usage(asset, persona, days, now):
    today = now.date()
    start = today - timedelta(days=days - 1)
    rng0 = _rng("leave", asset.asset_id)
    workdays = [start + timedelta(days=i) for i in range(days - 1) if (start + timedelta(days=i)).weekday() < 5]
    leave = set(rng0.sample(workdays, k=min(2, len(workdays)))) if workdays else set()
    written = 0
    for i in range(days):
        day = start + timedelta(days=i)
        rng = _rng("day", asset.asset_id, day)
        total = _day_seconds(persona, day, now, rng, leave)
        if total <= 0:
            DailyUsage.objects.filter(asset=asset, day=day).delete()
            continue
        for key, secs in _split(persona, total, rng).items():
            app_id, name = APPS[key]
            sessions = max(1, int(secs / rng.uniform(700, 1700)))
            end = datetime.combine(day, time(int(WORK_START + min(WORK_LEN, total / 3600 + 1.2)), rng.randint(0, 59)))
            DailyUsage.objects.update_or_create(
                asset=asset, day=day, app_id=app_id,
                defaults=dict(app_name=name, seconds=round(secs, 1), sessions=sessions, activations=sessions * rng.randint(2, 5),
                              last_active=min(timezone.make_aware(end), now)))
            written += 1
    return written


def _seed_events(task, asset, created, completed, status, percent, today, now, rng):
    """A believable trail for a task: created, a few days of progress, then done (or still going). Timesheets are built from this."""
    def at(d, hour):
        return min(timezone.make_aware(datetime.combine(d, time(hour, rng.randint(0, 59)))), now)
    TaskEvent.objects.create(task=task, asset=asset, at=at(created, 10), status="todo", percent=0)
    end = (completed.date() if completed else today)
    span = max(0, (end - created).days)
    work_days = [created + timedelta(days=i) for i in range(1, span + 1) if (created + timedelta(days=i)).weekday() < 5][-4:]
    steps = len(work_days)
    for i, d in enumerate(work_days):
        if completed and d == completed.date():
            continue
        pct = int(100 * (i + 1) / (steps + 1)) if completed else min(percent, int(100 * (i + 1) / (steps + 1)))
        TaskEvent.objects.create(task=task, asset=asset, at=at(d, rng.randint(11, 17)), status="in_progress", percent=max(10, pct))
    if completed:
        TaskEvent.objects.create(task=task, asset=asset, at=completed, status="done", percent=100)
    elif status in ("blocked", "in_progress") and not work_days:
        TaskEvent.objects.create(task=task, asset=asset, at=at(today, 11), status=status, percent=percent)


def seed_tasks(asset, persona, days, now, few=False):
    """~15 tasks over the window: most finished (some early, some late), a few in flight, a few overdue, a few coming up."""
    today = now.date()
    rng = _rng("tasks", asset.asset_id)
    titles = list(persona["tasks"])
    rng.shuffle(titles)
    if few:
        titles = titles[:6]
    made = 0
    for i, title in enumerate(titles):
        created = today - timedelta(days=days - 1) + timedelta(days=int(i * (days - 1) / len(titles)))
        due = created + timedelta(days=rng.randint(3, 10))
        est = rng.choice([1, 2, 2, 3, 4, 4, 6, 8])   # most real tasks are a few hours, not days
        priority = rng.choices(["low", "medium", "high", "urgent"], [2, 5, 4, 1])[0]
        if due < today - timedelta(days=1):
            fate = rng.choices(["done_early", "done_late", "overdue", "blocked"], [66, 24, 6, 4])[0]
        elif due <= today + timedelta(days=2):
            fate = rng.choices(["in_progress", "todo", "done_early"], [40, 20, 40])[0]
        else:
            fate = rng.choices(["todo", "in_progress", "done_early"], [45, 30, 25])[0]
        status, percent, completed = "todo", 0, None
        if fate.startswith("done"):
            offset = rng.randint(1, 3) if fate == "done_early" else -rng.randint(1, 3)
            done_day = min(due - timedelta(days=offset), today)
            done_day = max(done_day, created)
            status, percent = "done", 100
            completed = timezone.make_aware(datetime.combine(done_day, time(rng.randint(11, 18), rng.randint(0, 59))))
            completed = min(completed, now)
        elif fate == "in_progress":
            status, percent = "in_progress", rng.choice([20, 35, 50, 65, 80])
        elif fate == "blocked":
            status, percent = "blocked", rng.choice([25, 40, 60])
        elif fate == "overdue":
            status, percent = "in_progress", rng.choice([30, 60, 85])
        desc = ""
        t, was_new = Task.objects.get_or_create(asset=asset, title=title, defaults=dict(
            description=desc, priority=priority, status=status, percent=percent, estimated_hours=est, due_date=due, completed_at=completed))
        if was_new:
            made += 1
            Task.objects.filter(pk=t.pk).update(created_at=timezone.make_aware(datetime.combine(created, time(10, 0))))
            _seed_events(t, asset, created, completed, status, percent, today, now, rng)
    return made


def seed_recognitions(asset, persona, now, rng_key, admin=None, few=False):
    """A few approved (with certificates), one waiting, maybe one declined. Real-looking, never duplicated on a re-run."""
    rng = _rng("rec", asset.asset_id, rng_key)
    today = now.date()
    plan = []
    if few:   # a small, predictable set for demos: one approved (with certificate), one waiting, one the employee wrote themselves
        plan = [("approved", "weekend", "Weekend contribution (Saturday)", "Worked about 5.5 hours on a Saturday to support the release.", 1.0, 9),
                ("pending", "delivery", "Delivered 4 tasks this week", "Completed 4 assigned tasks in the last 7 days.", 0.5, 1),
                ("pending", "extra", "Covered the production incident", "Stayed on after hours to restore the payment service.", 1.0, 2)]
    elif rng.random() < 0.8:
        plan.append(("approved", "weekend", f"Weekend contribution (Saturday)", "Worked about 5.5 hours on a Saturday to support the release.", 1.0, 17))
    if not few and rng.random() < 0.6:
        plan.append(("approved", "early", "Delivered ahead of schedule", "Finished a key task three days before the due date.", 0.5, 9))
    if not few and rng.random() < 0.5:
        plan.append(("pending", "delivery", "Delivered 4 tasks this week", "Completed 4 assigned tasks in the last 7 days.", 0.5, 2))
    if not few and rng.random() < 0.25:
        plan.append(("rejected", "extra", "Helped a teammate with a bug", "Spent an afternoon pairing on a hard bug.", 0.0, 12))
    made = 0
    for status, kind, title, citation, credits, ago in plan:
        key = f"mock:{kind}:{status}"
        if Recognition.objects.filter(asset=asset, dedupe_key=key).exists():
            continue
        when = timezone.make_aware(datetime.combine(today - timedelta(days=ago), time(15, 20)))
        rec = Recognition.objects.create(
            asset=asset, kind=kind, source=rng.choice(["orbit", "orbit", "employee"]), title=title, citation=citation, dedupe_key=key, status=status,
            credits=credits, evidence={"mock": True})
        upd = {"created_at": when}
        if status != "pending":
            upd.update(decided_at=when + timedelta(hours=rng.randint(2, 20)), decided_by=admin)
            if status == "approved":
                upd.update(reward=rng.choice(REWARDS), certificate_id=f"CERT-{when:%Y}-{rng.randrange(16**6):06X}", admin_note="Thank you!")
            else:
                upd.update(admin_note="Great effort. Not eligible this time.")
        Recognition.objects.filter(pk=rec.pk).update(**upd)
        made += 1
    return made


def seed_weekend_alerts(asset, persona, days, now):
    today = now.date()
    made = 0
    for i in range(days):
        day = today - timedelta(days=i)
        if day.weekday() < 5 or day >= today:
            continue
        total = DailyUsage.objects.filter(asset=asset, day=day).values_list("seconds", flat=True)
        if sum(total) < 3600:
            continue
        rng = _rng("alert", asset.asset_id, day)
        reason, details = rng.choice(WEEKEND_REASONS)
        uid = f"mock-{asset.asset_id}-{day.isoformat()}"
        top = DailyUsage.objects.filter(asset=asset, day=day).order_by("-seconds").first()
        _, new = WeekendAlert.objects.get_or_create(uid=uid, defaults=dict(
            asset=asset, created_at=timezone.make_aware(datetime.combine(day, time(11, rng.randint(0, 59)))), day=day.isoformat(),
            app_id=top.app_id, app_name=top.app_name, reason=reason, details=details, approver="Reporting manager",
            duration=f"{sum(total) / 3600:.1f} hours", status="reviewed" if (today - day).days > 7 else "new"))
        made += int(new)
    return made


def make_asset(persona, idx, now):
    asset, new = Asset.objects.get_or_create(asset_id=f"MOCK{idx + 1:03d}", defaults=dict(
        employee_name=persona["name"], employee_email=f"{persona['key']}@example.com", department=persona["dept"],
        location="Chennai Office", device_type="Laptop", notes=MARK))
    if new or not asset.token_hash:
        asset.enroll()  # a random token nobody holds: these laptops are simulated
        asset.enrolled_at = now - timedelta(days=35)
        asset.save()
    return asset


def refresh_device_state(asset, persona, now):
    """Looks live: online with a current app during working hours, otherwise last seen at the end of the previous day."""
    today_secs = float(sum(DailyUsage.objects.filter(asset=asset, day=now.date()).values_list("seconds", flat=True)))
    working = now.weekday() < 5 and WORK_START <= now.hour + now.minute / 60 <= WORK_START + WORK_LEN and today_secs > 0
    rng = _rng("now", asset.asset_id, now.strftime("%Y%m%d%H%M"))
    keys = list(persona["apps"])
    current = APPS[rng.choice(keys)]
    Asset.objects.filter(pk=asset.pk).update(
        hostname=f"{persona['key']}-mbp", os="macOS 15.1", platform="darwin", arch="arm64", local_admin=persona["key"] in ("sanjay", "amit"), agent_version="1.4.0",
        last_seen=now if working else now - timedelta(hours=rng.randint(2, 14)), state="tracking" if working else "offline",
        current_app_id=current[0] if working else "", current_app_name=current[1] if working else "",
        session_start=now - timedelta(minutes=rng.randint(3, 40)) if working else None,
        running_apps=[{"id": APPS[k][0], "name": APPS[k][1]} for k in keys[:5]] if working else [], today_seconds=today_secs)
    InstalledApps.objects.update_or_create(asset=asset, defaults=dict(apps=[
        {"id": APPS[k][0], "name": APPS[k][1], "version": f"{rng.randint(1, 9)}.{rng.randint(0, 20)}.{rng.randint(0, 9)}"} for k in keys]))


def seed(days=30, now=None, personas=None, assets=None, few=False):
    """Seed mock people (and/or history for existing assets). Returns a summary dict."""
    now = now or timezone.localtime()
    from django.contrib.auth import get_user_model
    admin = get_user_model().objects.filter(is_staff=True).first()
    out = {"assets": 0, "usage": 0, "tasks": 0, "recognitions": 0, "alerts": 0}
    targets = []
    if assets:
        for i, a in enumerate(assets):
            targets.append((a, PERSONAS[i % len(PERSONAS)]))
    else:
        chosen = list(personas or PERSONAS)
        for i, p in enumerate(chosen[:3] if few else chosen):
            targets.append((make_asset(p, i, now), p))
    for asset, persona in targets:
        out["assets"] += 1
        if not assets:   # fake people get fake usage; a real laptop's usage is only ever what it really reported
            out["usage"] += seed_usage(asset, persona, days, now)
        out["tasks"] += seed_tasks(asset, persona, days, now, few)
        out["recognitions"] += seed_recognitions(asset, persona, now, "v1", admin, few)
        if not assets:
            out["alerts"] += seed_weekend_alerts(asset, persona, days, now)
        if not assets:
            refresh_device_state(asset, persona, now)
    return out


def remove_mock():
    """Deletes only assets marked as mock data (and, through cascade, their usage, tasks, recognitions and alerts)."""
    qs = Asset.objects.filter(notes=MARK, asset_id__startswith="MOCK")
    n = qs.count()
    qs.delete()
    return n

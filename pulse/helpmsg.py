"""When an employee tells Orbit they have no task, Orbit tells their reporting person.

Safety by construction: the message text comes from a fixed template (the model only supplies an optional short note, which is cleaned and
length-limited); the server checks that the employee really has no open task; and one message per employee per day is the most that goes out."""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.utils import timezone

from . import assistant
from .models import ManagerMessage, Task

log = logging.getLogger(__name__)
COOLDOWN = timedelta(hours=24)


def _recipient(asset):
    """The reporting person if one is set; otherwise the admins, so the message never goes nowhere."""
    if asset.manager_email:
        return asset.manager_name or "your reporting person", [asset.manager_email]
    emails = [u.email for u in get_user_model().objects.filter(is_active=True, is_staff=True).exclude(email="")[:20]]
    return "the admin", emails


def compose(asset, note):
    who = asset.employee_name
    body = f"{who} told Orbit they don't have any task assigned right now and are ready for new work."
    if note:
        body += f' They added: "{note}"'
    return body + " You can assign a task from the Tasks page."


def request_work(asset, note=""):
    open_n = Task.objects.filter(asset=asset).exclude(status="done").count()
    if open_n:
        return {"sent": False, "reason": "has_tasks", "openTasks": open_n,
                "text": f"You still have {open_n} open task{'s' if open_n != 1 else ''}, so I haven't messaged anyone."}
    recent = ManagerMessage.objects.filter(asset=asset, kind="no_tasks", created_at__gte=timezone.now() - COOLDOWN).first()
    if recent:
        return {"sent": False, "reason": "already_sent", "to": recent.to_name or "the admin",
                "text": f"I already let {recent.to_name or 'the admin'} know today. I'll make sure the request stays at the top of their list."}
    note = assistant.clean_reply(assistant.redact(str(note or "")), 200) if note else ""
    to_name, emails = _recipient(asset)
    msg = ManagerMessage.objects.create(asset=asset, body=compose(asset, note), to_name=to_name, to_email=emails[0] if emails else "")
    if emails:
        try:
            send_mail(f"{asset.employee_name} is ready for new work", f"{msg.body}\n\n- Orbit, on behalf of {asset.employee_name}", settings.DEFAULT_FROM_EMAIL, emails, fail_silently=False)
            msg.emailed = True
            msg.save(update_fields=["emailed"])
        except Exception:  # noqa: BLE001 - mail trouble must not break the chat; the admin inbox still has the message
            log.exception("could not email the no-task message")
    return {"sent": True, "to": to_name, "emailed": msg.emailed,
            "text": f"Done. I've let {to_name} know you're free for new work." + ("" if msg.emailed else " They'll see it in Proglint.")}


def resolve_for(asset):
    ManagerMessage.objects.filter(asset=asset, resolved=False).update(resolved=True)

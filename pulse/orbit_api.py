"""Orbit API.

Employee (enrolled laptop, `Authorization: Bearer <agent token>`), always scoped to that laptop's employee:
    GET   /api/agent/orbit/briefing?localTime=<ISO with offset>   greeting + the day at a glance
    POST  /api/agent/orbit/chat            {messages, agentName, localTime?}
    GET   /api/agent/orbit/tasks           PATCH /api/agent/orbit/tasks/<id>  {status, percent}
    GET   /api/agent/orbit/achievements    milestones, credits, things worth celebrating, recognitions
    POST  /api/agent/orbit/recognitions    {dedupeKey} or {title, details}   send an achievement to the admin
    GET   /api/agent/orbit/recognitions/<id>/certificate
    GET   /api/agent/orbit/pulse           small counters for notifications

Admin (staff token):
    GET/POST /api/admin/orbit/tasks, PATCH/DELETE /api/admin/orbit/tasks/<id>
    GET/POST /api/admin/orbit/recognitions, POST .../<id>/decision, GET .../<id>/certificate
    GET /api/admin/orbit/team, POST /api/admin/orbit/chat
"""
import uuid
from datetime import date

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from . import helpmsg, orbit, timesheet
from .agent_api import AgentView, _rate_limited
from .models import Asset, ManagerMessage, Recognition, Task


def fail(err):
    return Response({"error": err.message, "code": err.code}, status=err.status)


def _off():
    return Response({"error": "Orbit is switched off by your administrator.", "code": "orbit_disabled"}, status=404)


def _now(request, raw=None):
    raw = raw if raw is not None else request.query_params.get("localTime")
    return orbit.parse_local_time(raw) if raw else timezone.localtime()


# ---- employee ---------------------------------------------------------------------------------------

class BriefingView(AgentView):
    def get(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        if _rate_limited(f"orbit-brief:{request.auth.asset_id}", 60, 3600):
            return Response({"error": "Let's slow down a little. Try again in a few minutes.", "code": "rate_limited"}, status=429)
        try:
            return Response(orbit.briefing(request.auth, orbit.parse_local_time(request.query_params.get("localTime"))))
        except orbit.OrbitError as err:
            return fail(err)


class WrapupView(AgentView):
    def get(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        if _rate_limited(f"orbit-wrap:{request.auth.asset_id}", 20, 3600):
            return Response({"error": "Let's slow down a little.", "code": "rate_limited"}, status=429)
        try:
            return Response(orbit.wrapup(request.auth, orbit.parse_local_time(request.query_params.get("localTime"))))
        except orbit.OrbitError as err:
            return fail(err)


class ChatView(AgentView):
    def post(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        if _rate_limited(f"orbit-chat:{request.auth.asset_id}", settings.ORBIT_CHAT_PER_10MIN, 600):
            return Response({"error": "You're chatting very quickly. Give me a few minutes and ask again.", "code": "rate_limited"}, status=429)
        try:
            now = _now(request, request.data.get("localTime"))
            return Response(orbit.chat(request.auth, request.data.get("messages"), now, request.data.get("agentName")))
        except orbit.OrbitError as err:
            return fail(err)


class TaskListView(AgentView):
    def get(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        try:
            today = _now(request).date()
        except orbit.OrbitError as err:
            return fail(err)
        s = orbit.task_sections(request.auth, today)
        return Response({**s, "capacity": orbit.capacity(request.auth, today)})


class TaskDetailView(AgentView):
    """The employee reports their own progress. (Chat can't do this; it's a button.)"""

    def patch(self, request, task_id):
        task = get_object_or_404(Task, pk=task_id, asset=request.auth)
        status, percent = request.data.get("status", task.status), request.data.get("percent", task.percent)
        if status not in dict(Task.STATUSES):
            return Response({"error": "Unknown status.", "code": "bad_status"}, status=422)
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            return Response({"error": "Progress must be between 0 and 100.", "code": "bad_percent"}, status=422)
        if status == "done":
            percent = 100
        task.status, task.percent = status, percent
        task.completed_at = (task.completed_at or timezone.now()) if status == "done" else None
        task.save()
        timesheet.record_event(task)
        return Response(task.to_dict())


class AchievementsView(AgentView):
    def get(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        try:
            today = _now(request).date()
        except orbit.OrbitError as err:
            return fail(err)
        a = request.auth
        return Response({"milestones": orbit.achievements(a, today), "credits": orbit.credits_total(a), "suggestions": orbit.candidates(a, today),
                         "recognitions": [r.to_dict() for r in Recognition.objects.filter(asset=a)[:30]]})


class RecognitionListView(AgentView):
    def post(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        if _rate_limited(f"orbit-nominate:{request.auth.asset_id}", 20, 3600):
            return Response({"error": "That's a lot at once. Try again later.", "code": "rate_limited"}, status=429)
        try:
            today = _now(request, request.data.get("localTime")).date()
            rec = orbit.nominate(request.auth, today, request.data.get("dedupeKey"), request.data.get("title"), request.data.get("details"))
        except orbit.OrbitError as err:
            return fail(err)
        return Response(rec.to_dict(), status=201)


class CertificateView(AgentView):
    def get(self, request, rec_id):
        rec = get_object_or_404(Recognition.objects.select_related("asset", "decided_by"), pk=rec_id, asset=request.auth, status="approved")
        return Response({"html": orbit.certificate_html(rec), "certificateId": rec.certificate_id})


class PulseView(AgentView):
    """Tiny, cheap: what changed that the employee would like to hear about."""

    def get(self, request):
        a = request.auth
        open_tasks = Task.objects.filter(asset=a).exclude(status="done")
        return Response({"enabled": settings.ORBIT_ENABLED,
                         "tasks": [{"id": str(t.id), "title": t.title, "priority": t.priority} for t in open_tasks[:30]],
                         "recognitions": [{"id": str(r.id), "status": r.status, "title": r.title, "reward": r.reward, "credits": r.credits} for r in Recognition.objects.filter(asset=a)[:30]]})


# ---- admin ------------------------------------------------------------------------------------------

def _parse_due(raw):
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as err:
        raise orbit.OrbitError("Use a due date like 2026-12-31.", 422, "bad_due") from err


def _task_fields(data, partial=False):
    out = {}
    if "title" in data or not partial:
        title = orbit.clean_text(data.get("title"), 160)
        if len(title) < 3:
            raise orbit.OrbitError("Give the task a title (at least 3 characters).", 422, "title_required")
        out["title"] = title
    if "description" in data:
        out["description"] = orbit.clean_text(data.get("description"), 1000)
    if "priority" in data:
        if data["priority"] not in dict(Task.PRIORITIES):
            raise orbit.OrbitError("Priority must be low, medium, high or urgent.", 422, "bad_priority")
        out["priority"] = data["priority"]
    if "estimatedHours" in data:
        try:
            hours = float(data["estimatedHours"])
        except (TypeError, ValueError) as err:
            raise orbit.OrbitError("Estimated hours must be a number.", 422, "bad_hours") from err
        if not 0 <= hours <= 400:
            raise orbit.OrbitError("Estimated hours must be between 0 and 400.", 422, "bad_hours")
        out["estimated_hours"] = round(hours, 1)
    if "dueDate" in data:
        out["due_date"] = _parse_due(data["dueDate"])
    return out


class TaskAdminListView(APIView):
    def get(self, request):
        qs = Task.objects.select_related("asset", "created_by")
        if request.query_params.get("asset"):
            qs = qs.filter(asset_id=request.query_params["asset"][:40])
        if request.query_params.get("status") in dict(Task.STATUSES):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"items": [t.to_dict(admin=True) for t in qs[:300]]})

    def post(self, request):
        try:
            fields = _task_fields(request.data)
            ids = request.data.get("assetIds")
            if not isinstance(ids, list) or not ids or len(ids) > 50:
                raise orbit.OrbitError("Choose who this task is for (up to 50 people).", 422, "assignees_required")
            assets = list(Asset.objects.filter(asset_id__in=[str(i)[:40] for i in ids]))
            if len(assets) != len(set(map(str, ids))):
                raise orbit.OrbitError("One of the people you chose no longer exists.", 422, "unknown_assignee")
        except orbit.OrbitError as err:
            return fail(err)
        today = timezone.localdate()
        # How would this land on each person's week? Computed before the task exists, so it isn't counted twice.
        check = [orbit.feasibility(a, fields.get("estimated_hours", 0), fields.get("due_date"), today) for a in assets]
        group = uuid.uuid4()
        with transaction.atomic():
            created = [Task.objects.create(asset=a, group=group, created_by=request.user, **fields) for a in assets]
            for t in created:
                timesheet.record_event(t)
                helpmsg.resolve_for(t.asset)  # they asked for work and now have some
        return Response({"items": [t.to_dict(admin=True) for t in created], "feasibility": check}, status=201)


class TaskAdminDetailView(APIView):
    def patch(self, request, task_id):
        task = get_object_or_404(Task, pk=task_id)
        try:
            fields = _task_fields(request.data, partial=True)
            if "status" in request.data:
                if request.data["status"] not in dict(Task.STATUSES):
                    raise orbit.OrbitError("Unknown status.", 422, "bad_status")
                fields["status"] = request.data["status"]
                fields["completed_at"] = (task.completed_at or timezone.now()) if fields["status"] == "done" else None
                if fields["status"] == "done":
                    fields["percent"] = 100
        except orbit.OrbitError as err:
            return fail(err)
        changed = any(k in fields for k in ("status", "percent"))
        for k, v in fields.items():
            setattr(task, k, v)
        task.save()
        if changed:
            timesheet.record_event(task)
        return Response(task.to_dict(admin=True))

    def delete(self, request, task_id):
        get_object_or_404(Task, pk=task_id).delete()
        return Response({"deleted": True})


class RecognitionAdminListView(APIView):
    def get(self, request):
        qs = Recognition.objects.select_related("asset", "decided_by")
        if request.query_params.get("status") in dict(Recognition.STATUSES):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"items": [r.to_dict(admin=True) for r in qs[:200]],
                         "pending": Recognition.objects.filter(status="pending").count()})

    def post(self, request):
        """An admin recognises someone directly."""
        asset = get_object_or_404(Asset, asset_id=str(request.data.get("assetId") or "")[:40])
        try:
            rec = orbit.award_direct(asset, request.user, request.data.get("title"), request.data.get("citation"),
                                     request.data.get("reward"), request.data.get("credits"), request.data.get("kind", "kudos"))
        except orbit.OrbitError as err:
            return fail(err)
        return Response(rec.to_dict(admin=True), status=201)


class RecognitionDecisionView(APIView):
    def post(self, request, rec_id):
        with transaction.atomic():
            rec = get_object_or_404(Recognition.objects.select_for_update().select_related("asset"), pk=rec_id)
            try:
                orbit.decide(rec, request.user, request.data.get("decision"), request.data.get("reward"), request.data.get("credits"), request.data.get("note"))
            except orbit.OrbitError as err:
                return fail(err)
        return Response(rec.to_dict(admin=True))


class RecognitionCertificateView(APIView):
    def get(self, request, rec_id):
        rec = get_object_or_404(Recognition.objects.select_related("asset", "decided_by"), pk=rec_id, status="approved")
        return Response({"html": orbit.certificate_html(rec), "certificateId": rec.certificate_id})


class ManagerMessageListView(APIView):
    """The admin's inbox of "I have no task" messages (the reporting person also gets an email)."""

    def get(self, request):
        qs = ManagerMessage.objects.select_related("asset")
        if request.query_params.get("open") == "1":
            qs = qs.filter(resolved=False)
        return Response({"items": [m.to_dict() for m in qs[:100]], "open": ManagerMessage.objects.filter(resolved=False).count()})


class ManagerMessageResolveView(APIView):
    def post(self, request, msg_id):
        m = get_object_or_404(ManagerMessage, pk=msg_id)
        m.resolved = True
        m.save(update_fields=["resolved"])
        return Response(m.to_dict())


class TeamView(APIView):
    def get(self, request):
        return Response({"items": orbit.team_workload(timezone.localdate())})


class AdminChatView(APIView):
    def post(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        if _rate_limited(f"orbit-admin-chat:{request.user.pk}", 40, 600):
            return Response({"error": "Slow down a little and try again in a few minutes.", "code": "rate_limited"}, status=429)
        try:
            return Response(orbit.admin_chat(request.data.get("messages"), timezone.localdate()))
        except orbit.OrbitError as err:
            return fail(err)

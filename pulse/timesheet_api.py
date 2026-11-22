"""Timesheet, calendar, summaries and Employee of the Month.

Employee (enrolled laptop), always their own data:
    GET /api/agent/orbit/timesheet?month=YYYY-MM          month calendar, days, totals
    GET /api/agent/orbit/timesheet/summary?period=week|month&start=YYYY-MM-DD   the AI summary
Admin (staff token), any employee:
    GET /api/admin/orbit/timesheets/<asset_id>?month=     same payload
    GET /api/admin/orbit/timesheets/<asset_id>/summary?period=&start=
    GET /api/admin/orbit/awards/candidates?month=         ranked, with the score broken down
    POST /api/admin/orbit/awards/citation  {month, assetId}   a warmer citation written by the AI
    GET/POST /api/admin/orbit/awards       history / {month, assetId, reward, credits, citation, replace}
"""
from datetime import date

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from . import orbit, timesheet
from .agent_api import AgentView, _rate_limited
from .models import Asset, MonthlyAward
from .orbit_api import _off, fail


def _summary_args(request):
    period = request.query_params.get("period", "month")
    if period not in ("week", "month"):
        raise orbit.OrbitError("Period must be week or month.", 422, "bad_period")
    raw = request.query_params.get("start")
    try:
        start = date.fromisoformat(raw) if raw else timezone.localdate()
    except ValueError as err:
        raise orbit.OrbitError("Use a start date like 2026-11-16.", 422, "bad_start") from err
    return period, (timesheet.week_start(start) if period == "week" else start.replace(day=1))


class TimesheetView(AgentView):
    def get(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        try:
            return Response(timesheet.month_timesheet(request.auth, timesheet.parse_month(request.query_params.get("month"))))
        except orbit.OrbitError as err:
            return fail(err)


class TimesheetSummaryView(AgentView):
    def get(self, request):
        if not settings.ORBIT_ENABLED:
            return _off()
        if _rate_limited(f"ts-summary:{request.auth.asset_id}", 30, 3600):
            return Response({"error": "Let's slow down a little. Try again in a few minutes.", "code": "rate_limited"}, status=429)
        try:
            period, start = _summary_args(request)
            return Response(timesheet.summary(request.auth, period, start))
        except orbit.OrbitError as err:
            return fail(err)


class AdminTimesheetView(APIView):
    def get(self, request, asset_id):
        asset = get_object_or_404(Asset, asset_id=asset_id)
        try:
            return Response({**timesheet.month_timesheet(asset, timesheet.parse_month(request.query_params.get("month"))), "employeeName": asset.employee_name, "assetId": asset.asset_id})
        except orbit.OrbitError as err:
            return fail(err)


class AdminTimesheetSummaryView(APIView):
    def get(self, request, asset_id):
        asset = get_object_or_404(Asset, asset_id=asset_id)
        if _rate_limited(f"ts-summary-admin:{request.user.pk}", 60, 600):
            return Response({"error": "Slow down a little and try again in a few minutes.", "code": "rate_limited"}, status=429)
        try:
            period, start = _summary_args(request)
            return Response({**timesheet.summary(asset, period, start), "employeeName": asset.employee_name})
        except orbit.OrbitError as err:
            return fail(err)


class AwardCandidatesView(APIView):
    def get(self, request):
        try:
            first = timesheet.parse_month(request.query_params.get("month"))
        except orbit.OrbitError as err:
            return fail(err)
        award = MonthlyAward.objects.filter(month=first.strftime("%Y-%m")).select_related("asset", "recognition").first()
        return Response({"month": first.strftime("%Y-%m"), "label": timesheet.month_label(first), "candidates": timesheet.month_candidates(first),
                         "award": award.to_dict() if award else None,
                         "method": "Score = 35% delivery (tasks by priority) + 20% reliability (on time) + 20% consistency (healthy days) + 25% appreciation. "
                                   "Overwork is never rewarded, only flagged. This is a guide: you decide."})


class AwardCitationView(APIView):
    def post(self, request):
        if _rate_limited(f"award-citation:{request.user.pk}", 20, 600):
            return Response({"error": "Slow down a little.", "code": "rate_limited"}, status=429)
        try:
            first = timesheet.parse_month(request.data.get("month"))
        except orbit.OrbitError as err:
            return fail(err)
        cand = next((c for c in timesheet.month_candidates(first) if c["assetId"] == str(request.data.get("assetId") or "")), None)
        if not cand:
            return Response({"error": "That person has nothing recorded for this month.", "code": "no_data"}, status=404)
        text, source = timesheet.ai_citation(cand, timesheet.month_label(first))
        return Response({"citation": text, "source": source})


class AwardListView(APIView):
    def get(self, request):
        rows = MonthlyAward.objects.select_related("asset", "recognition")[:36]
        return Response({"items": [r.to_dict() for r in rows]})

    def post(self, request):
        try:
            first = timesheet.parse_month(request.data.get("month"))
            asset = get_object_or_404(Asset, asset_id=str(request.data.get("assetId") or "")[:40])
            try:
                credits = float(request.data.get("credits", 2))
            except (TypeError, ValueError) as err:
                raise orbit.OrbitError("Credits must be a number.", 422, "bad_credits") from err
            award = timesheet.award_month(request.user, asset, first, request.data.get("reward", ""), credits, orbit.clean_text(request.data.get("citation"), 600),
                                          request.data.get("replace") is True)
        except orbit.OrbitError as err:
            return fail(err)
        return Response(award.to_dict(), status=201)

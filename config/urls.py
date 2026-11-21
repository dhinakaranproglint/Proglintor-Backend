from django.contrib import admin
from django.urls import path

from pulse import admin_api, agent_api, auth_api, orbit_api, software_api

admin.site.site_header = "Proglintor"
admin.site.site_title = "Proglintor"
admin.site.index_title = "Backend administration"

urlpatterns = [
    path("api/health", admin_api.HealthView.as_view()),
    # Agents (employee laptops)
    path("api/agent/enroll", agent_api.EnrollView.as_view()),
    path("api/agent/heartbeat", agent_api.HeartbeatView.as_view()),
    path("api/agent/usage", agent_api.UsageView.as_view()),
    path("api/agent/installed", agent_api.InstalledView.as_view()),
    path("api/agent/alerts", agent_api.AlertsView.as_view()),
    path("api/agent/icons/missing", agent_api.IconsMissingView.as_view()),
    path("api/agent/icons", agent_api.IconsUploadView.as_view()),
    # Software install chat (employee side)
    path("api/agent/orbit/briefing", orbit_api.BriefingView.as_view()),
    path("api/agent/orbit/wrapup", orbit_api.WrapupView.as_view()),
    path("api/agent/orbit/chat", orbit_api.ChatView.as_view()),
    path("api/agent/orbit/tasks", orbit_api.TaskListView.as_view()),
    path("api/agent/orbit/tasks/<uuid:task_id>", orbit_api.TaskDetailView.as_view()),
    path("api/agent/orbit/achievements", orbit_api.AchievementsView.as_view()),
    path("api/agent/orbit/recognitions", orbit_api.RecognitionListView.as_view()),
    path("api/agent/orbit/recognitions/<uuid:rec_id>/certificate", orbit_api.CertificateView.as_view()),
    path("api/agent/orbit/pulse", orbit_api.PulseView.as_view()),
    path("api/agent/software/catalogue", software_api.CatalogueView.as_view()),
    path("api/agent/software/preview", software_api.PreviewView.as_view()),
    path("api/agent/software/assist", software_api.AssistView.as_view()),
    path("api/agent/software/requests", software_api.RequestListView.as_view()),
    path("api/agent/software/requests/<uuid:request_id>/cancel", software_api.RequestCancelView.as_view()),
    path("api/agent/software/requests/<uuid:request_id>/confirm", software_api.RequestConfirmView.as_view()),
    path("api/agent/software/jobs/next", software_api.JobNextView.as_view()),
    path("api/agent/software/jobs/<uuid:request_id>/progress", software_api.JobProgressView.as_view()),
    path("api/agent/software/jobs/<uuid:request_id>/result", software_api.JobResultView.as_view()),
    # Admin desktop app
    path("api/admin/login", auth_api.LoginView.as_view()),
    path("api/admin/setup", auth_api.SetupView.as_view()),
    path("api/admin/password/forgot", auth_api.ForgotPasswordView.as_view()),
    path("api/admin/password/reset", auth_api.ResetPasswordView.as_view()),
    path("api/admin/logout", admin_api.LogoutView.as_view()),
    path("api/admin/me", admin_api.MeView.as_view()),
    path("api/admin/dashboard", admin_api.DashboardView.as_view()),
    path("api/admin/assets", admin_api.AssetListView.as_view()),
    path("api/admin/assets/<str:asset_id>", admin_api.AssetDetailView.as_view()),
    path("api/admin/assets/<str:asset_id>/reset-code", admin_api.AssetResetCodeView.as_view()),
    path("api/admin/alerts", admin_api.AlertListView.as_view()),
    path("api/admin/alerts/<str:uid>", admin_api.AlertDetailView.as_view()),
    path("api/admin/software/items", software_api.ItemListView.as_view()),
    path("api/admin/software/icons", software_api.ItemIconsView.as_view()),
    path("api/admin/software/items/<str:slug>", software_api.ItemDetailView.as_view()),
    path("api/admin/software/approvals", software_api.ApprovalListView.as_view()),
    path("api/admin/software/requests", software_api.RequestAdminListView.as_view()),
    path("api/admin/software/requests/<uuid:request_id>", software_api.RequestAdminDetailView.as_view()),
    path("api/admin/software/requests/<uuid:request_id>/decision", software_api.DecisionView.as_view()),
    path("api/admin/software/requests/<uuid:request_id>/approve-new", software_api.ApproveNewView.as_view()),
    path("api/admin/orbit/tasks", orbit_api.TaskAdminListView.as_view()),
    path("api/admin/orbit/tasks/<uuid:task_id>", orbit_api.TaskAdminDetailView.as_view()),
    path("api/admin/orbit/recognitions", orbit_api.RecognitionAdminListView.as_view()),
    path("api/admin/orbit/recognitions/<uuid:rec_id>/decision", orbit_api.RecognitionDecisionView.as_view()),
    path("api/admin/orbit/recognitions/<uuid:rec_id>/certificate", orbit_api.RecognitionCertificateView.as_view()),
    path("api/admin/orbit/team", orbit_api.TeamView.as_view()),
    path("api/admin/orbit/chat", orbit_api.AdminChatView.as_view()),
    path("api/admin/policy", admin_api.PolicyView.as_view()),
    path("api/admin/icons", admin_api.IconsView.as_view()),
    path("api/admin/export.csv", admin_api.ExportView.as_view()),
    # Django's built-in admin site (superusers)
    path("django-admin/", admin.site.urls),
]

from django.contrib import admin
from django.urls import path

from pulse import admin_api, agent_api, auth_api

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
    path("api/admin/policy", admin_api.PolicyView.as_view()),
    path("api/admin/icons", admin_api.IconsView.as_view()),
    path("api/admin/export.csv", admin_api.ExportView.as_view()),
    # Django's built-in admin site (superusers)
    path("django-admin/", admin.site.urls),
]

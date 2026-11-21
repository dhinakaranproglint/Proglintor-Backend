from django.contrib import admin

from .models import AppIcon, Asset, DailyUsage, InstalledApps, PasswordResetCode, Setting, WeekendAlert


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("asset_id", "employee_name", "employee_email", "department", "device_type", "hostname", "last_seen", "state")
    search_fields = ("asset_id", "employee_name", "employee_email", "hostname")
    list_filter = ("device_type", "department", "platform")
    readonly_fields = ("enrolled_at", "last_seen", "hostname", "os", "platform", "agent_version", "state",
                       "current_app_name", "session_start", "running_apps", "today_seconds", "created_at", "updated_at")


@admin.register(DailyUsage)
class DailyUsageAdmin(admin.ModelAdmin):
    list_display = ("day", "asset", "app_name", "seconds", "sessions")
    list_filter = ("day",)
    search_fields = ("asset__asset_id", "app_name", "app_id")
    date_hierarchy = "day"


@admin.register(WeekendAlert)
class WeekendAlertAdmin(admin.ModelAdmin):
    list_display = ("created_at", "asset", "app_name", "reason", "status", "dismissed")
    list_filter = ("status", "dismissed", "reason")
    search_fields = ("asset__asset_id", "asset__employee_name", "app_name", "details")


admin.site.register(InstalledApps)
admin.site.register(Setting)


@admin.register(PasswordResetCode)
class PasswordResetCodeAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "expires_at", "attempts", "used")
    readonly_fields = ("code_hash",)


@admin.register(AppIcon)
class AppIconAdmin(admin.ModelAdmin):
    list_display = ("app_id", "content_type", "updated_at")
    search_fields = ("app_id",)
    exclude = ("data",)

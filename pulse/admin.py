from django.contrib import admin

from .models import (AppIcon, Asset, Employee, DailyUsage, InstalledApps, PasswordResetCode, Setting, SoftwareEvent, SoftwareItem,
                     SoftwareRequest, WeekendAlert)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_id", "name", "email", "department", "manager", "active")
    search_fields = ("employee_id", "name", "email")
    list_filter = ("active", "department")


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


@admin.register(SoftwareItem)
class SoftwareItemAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "platform", "arch", "installer_type", "requires_approval", "enabled")
    list_filter = ("platform", "enabled", "requires_approval")
    search_fields = ("name", "slug", "publisher")


class SoftwareEventInline(admin.TabularInline):
    model = SoftwareEvent
    extra = 0
    can_delete = False
    readonly_fields = ("at", "actor", "kind", "detail")


@admin.register(SoftwareRequest)
class SoftwareRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "asset", "software_name", "version", "status", "decision_reason", "decided_by")
    list_filter = ("status",)
    search_fields = ("asset__asset_id", "software_name", "raw_request")
    readonly_fields = ("id", "spec", "created_at", "updated_at")
    inlines = [SoftwareEventInline]

from django.contrib import admin

from core.admin import AuditedAdminMixin
from projects.models import Phase, Project, SiteActivity, SiteDiary, WorkPackage


class PhaseInline(admin.TabularInline):
    model = Phase
    extra = 0
    fields = ("sequence", "name", "kind", "budget", "percent_complete", "planned_start", "planned_end")


@admin.register(Project)
class ProjectAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "code", "city", "star_target", "status", "approved_budget")
    list_filter = ("status", "star_target")
    search_fields = ("name", "code")
    filter_horizontal = ("members",)
    inlines = [PhaseInline]


class WorkPackageInline(admin.TabularInline):
    model = WorkPackage
    extra = 0


@admin.register(Phase)
class PhaseAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "kind", "budget", "percent_complete", "planned_end")
    list_filter = ("project", "kind")
    search_fields = ("name",)
    inlines = [WorkPackageInline]


class SiteActivityInline(admin.TabularInline):
    model = SiteActivity
    extra = 1


@admin.register(SiteDiary)
class SiteDiaryAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("project", "date", "weather", "workers_present", "supervisor")
    list_filter = ("project", "weather")
    date_hierarchy = "date"
    inlines = [SiteActivityInline]

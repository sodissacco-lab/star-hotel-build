from django.contrib import admin, messages

from core.admin import AuditedAdminMixin
from core.starter import load_starter_data
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
    actions = ["load_starter_list"]

    @admin.action(description="Load the starter list (units, categories, materials, building components)")
    def load_starter_list(self, request, queryset):
        for project in queryset:
            try:
                load_starter_data(project, request.user)
            except Exception as exc:  # WorkflowError / PermissionDenied: shown, not a crash
                messages.error(request, f"{project}: {exc}")
                continue
            a = project._starter_added
            messages.success(request, f"{project}: added {a['units']} units, {a['categories']} categories, "
                                      f"{a['materials']} materials and {a['components']} building components.")


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
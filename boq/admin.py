from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from accounts.permissions import can
from boq import importer, services
from boq.models import BOQItem, BOQMeasurement, Variation
from core.admin import AuditedAdminMixin, run_workflow_action
from projects.models import Project


class BOQImportForm(forms.Form):
    project = forms.ModelChoiceField(queryset=Project.objects.none())
    file = forms.FileField(label="BOQ file (.xlsx or .csv)")
    create_units = forms.BooleanField(required=False, label="Create missing units of measure automatically")
    dry_run = forms.BooleanField(required=False, initial=True,
                                 label="Check only - do not save anything yet")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = user.accessible_projects()


class MeasurementInline(admin.TabularInline):
    model = BOQMeasurement
    extra = 0
    fields = ("date", "quantity", "notes")

    def has_change_permission(self, request, obj=None):
        return False  # append-only; correct with a negative entry

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BOQItem)
class BOQItemAdmin(AuditedAdminMixin, admin.ModelAdmin):
    change_list_template = "admin/boq/boqitem/change_list.html"
    list_display = ("code", "description", "project", "phase", "unit", "original_quantity",
                    "revised_quantity", "rate")
    list_filter = ("project", "phase")
    search_fields = ("code", "description")
    inlines = [MeasurementInline]

    def get_readonly_fields(self, request, obj=None):
        base = ("revised_quantity",)
        if obj is not None:  # quantity changes only through variation orders
            return base + ("original_quantity",)
        return base

    def save_formset(self, request, form, formset, change):
        for inst in formset.save(commit=False):
            if isinstance(inst, BOQMeasurement) and inst.created_by_id is None:
                inst.created_by = request.user
            inst.save()
        formset.save_m2m()

    # ---- BOQ import ---------------------------------------------------------
    def get_urls(self):
        custom = [
            path("import/", self.admin_site.admin_view(self.import_view), name="boq_boqitem_import"),
            path("import/sample/", self.admin_site.admin_view(self.sample_view), name="boq_boqitem_sample"),
        ]
        return custom + super().get_urls()

    def sample_view(self, request):
        response = HttpResponse(importer.SAMPLE_CSV, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="boq_import_sample.csv"'
        return response

    def import_view(self, request):
        if not can(request.user, "manage_boq"):
            raise PermissionDenied
        form = BOQImportForm(request.POST or None, request.FILES or None, user=request.user)
        result = None
        if request.method == "POST" and form.is_valid():
            upload = form.cleaned_data["file"]
            try:
                result = importer.import_boq(
                    upload.read(), upload.name, form.cleaned_data["project"], request.user,
                    create_units=form.cleaned_data["create_units"], dry_run=form.cleaned_data["dry_run"],
                )
            except importer.ImportFileError as exc:
                form.add_error("file", str(exc))
            else:
                if result.errors:
                    messages.error(request, "Nothing was imported. Fix the problems listed below and upload again.")
                elif result.dry_run:
                    messages.success(
                        request, f"The file is valid: {result.valid_rows} item(s) are ready. "
                                 "Untick 'Check only' and upload again to import them.")
                else:
                    messages.success(request, f"Imported {result.created} BOQ item(s)"
                                              f"{f' and created {result.units_created} unit(s)' if result.units_created else ''}.")
                    return redirect(reverse("admin:boq_boqitem_changelist"))
        context = {
            **self.admin_site.each_context(request), "opts": self.model._meta, "title": "Import BOQ",
            "form": form, "result": result,
        }
        return TemplateResponse(request, "admin/boq/boqitem/import.html", context)


@admin.register(Variation)
class VariationAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("number", "item", "date", "quantity_change", "status", "approved_by")
    list_filter = ("status", "item__project")
    readonly_fields = ("number", "status", "approved_by", "approved_at")
    actions = ["approve_selected", "reject_selected"]

    def get_readonly_fields(self, request, obj=None):
        base = tuple(super().get_readonly_fields(request, obj))
        if obj is not None and obj.status != Variation.Status.DRAFT:
            return base + ("item", "date", "description", "reason", "quantity_change")
        return base

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != Variation.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Approve selected variations")
    def approve_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.approve_variation, "approved")

    @admin.action(description="Reject selected variations")
    def reject_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.reject_variation, "rejected")
from django.contrib import admin

from core.admin import AuditedAdminMixin, run_workflow_action
from procurement import services
from procurement.models import Quotation, Requisition, RequisitionLine

S = Requisition.Status
STILL_EDITABLE = {"preferred_supplier", "verification_notes"}


class LineInline(admin.TabularInline):
    model = RequisitionLine
    extra = 1

    def _locked(self, obj):
        return obj is not None and obj.status != S.DRAFT

    def has_add_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_delete_permission(request, obj)


class QuotationInline(admin.TabularInline):
    model = Quotation
    extra = 0

    def _closed(self, obj):
        return obj is not None and obj.status in (S.ORDERED, S.REJECTED)

    def has_add_permission(self, request, obj=None):
        return not self._closed(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return not self._closed(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self._closed(obj) and super().has_delete_permission(request, obj)


@admin.register(Requisition)
class RequisitionAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("number", "project", "status", "emergency", "needed_by", "estimate", "created_by")
    list_filter = ("project", "status", "emergency")
    search_fields = ("number",)
    inlines = [LineInline, QuotationInline]
    actions = ["submit_selected", "verify_selected", "approve_selected", "reject_selected", "make_po_selected"]

    @admin.display(description="Estimate (UGX)")
    def estimate(self, obj):
        return f"{obj.estimated_total:,.0f}"

    def get_readonly_fields(self, request, obj=None):
        base = ("number", "status", "verified_by", "verified_at", "approved_by", "approved_at", "purchase_order")
        if obj is None or obj.status == S.DRAFT:
            return base
        locked = [f.name for f in Requisition._meta.fields if f.editable and f.name not in STILL_EDITABLE]
        if obj.status in (S.ORDERED, S.REJECTED):
            locked += list(STILL_EDITABLE)
        return base + tuple(locked)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != S.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="1. Submit selected requisitions")
    def submit_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.submit_requisition, "submitted")

    @admin.action(description="2. Verify against BOQ and budget (Quantity Surveyor)")
    def verify_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.verify_requisition, "verified")

    @admin.action(description="3. Approve selected requisitions")
    def approve_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.approve_requisition, "approved")

    @admin.action(description="Reject selected requisitions")
    def reject_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.reject_requisition, "rejected")

    @admin.action(description="4. Create draft purchase order from approved requisitions")
    def make_po_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.create_po_from_requisition, "turned into draft purchase orders")

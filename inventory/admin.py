from django.contrib import admin

from core.admin import AuditedAdminMixin, run_workflow_action
from inventory import services
from inventory.models import (
    GoodsReceipt, GoodsReceiptLine, Material, MaterialCategory, MaterialIssue, MaterialIssueLine,
    MaterialReturn, PurchaseOrder, PurchaseOrderLine, StockAdjustment, StockLedger, Supplier, Unit,
    WastageRecord,
)


class PostedLockMixin:
    """Posted documents are read-only: corrections are made by new documents."""

    def get_readonly_fields(self, request, obj=None):
        base = tuple(super().get_readonly_fields(request, obj))
        if obj is not None and getattr(obj, "posted", False):
            return base + tuple(f.name for f in self.model._meta.fields)
        return base

    def has_delete_permission(self, request, obj=None):
        if obj is not None and getattr(obj, "posted", False):
            return False
        return super().has_delete_permission(request, obj)


class LockedInline:
    """Inline lines become read-only once the parent document is posted/approved."""

    locked_attr = "posted"

    def _locked(self, obj):
        if obj is None:
            return False
        if self.locked_attr == "status":
            return obj.status != PurchaseOrder.Status.DRAFT
        return bool(getattr(obj, self.locked_attr, False))

    def has_add_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_delete_permission(request, obj)


admin.site.register(MaterialCategory)
admin.site.register(Unit)


@admin.register(Supplier)
class SupplierAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "phone", "tin", "rating", "is_active")
    search_fields = ("name", "tin", "phone")
    list_filter = ("is_active", "rating")


@admin.register(Material)
class MaterialAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("code", "name", "category", "unit", "standard_price", "reorder_level", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("code", "name")


# ---- purchase orders
class POLineInline(LockedInline, admin.TabularInline):
    model = PurchaseOrderLine
    extra = 1
    locked_attr = "status"


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("number", "project", "supplier", "order_date", "status", "total_amount")
    list_filter = ("project", "status")
    search_fields = ("number", "supplier__name")
    readonly_fields = ("number", "status", "approved_by", "approved_at")
    inlines = [POLineInline]
    actions = ["submit_selected", "approve_selected"]

    @admin.display(description="Total (UGX)")
    def total_amount(self, obj):
        return f"{obj.total:,.0f}"

    @admin.action(description="Submit selected purchase orders for approval")
    def submit_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.submit_po, "submitted")

    @admin.action(description="Approve selected purchase orders")
    def approve_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.approve_po, "approved")


# ---- goods received
class GRNLineInline(LockedInline, admin.TabularInline):
    model = GoodsReceiptLine
    extra = 1


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(PostedLockMixin, AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "po", "receipt_date", "delivery_note_no", "posted")
    list_filter = ("posted",)
    inlines = [GRNLineInline]
    actions = ["post_selected"]

    @admin.action(description="Post selected receipts to stock")
    def post_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.post_goods_receipt, "posted to stock")


# ---- issues
class IssueLineInline(LockedInline, admin.TabularInline):
    model = MaterialIssueLine
    extra = 1


@admin.register(MaterialIssue)
class MaterialIssueAdmin(PostedLockMixin, AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "project", "issue_date", "activity", "requested_by", "posted")
    list_filter = ("project", "posted")
    inlines = [IssueLineInline]
    actions = ["post_selected"]

    @admin.action(description="Post selected issues (deduct from stock)")
    def post_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.post_issue, "posted")


@admin.register(MaterialReturn)
class MaterialReturnAdmin(PostedLockMixin, AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "project", "material", "quantity", "return_date", "posted")
    list_filter = ("project", "posted")
    actions = ["post_selected"]

    @admin.action(description="Post selected returns (add back to stock)")
    def post_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.post_return, "posted")


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(PostedLockMixin, AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "project", "material", "date", "counted_quantity", "system_quantity", "difference", "posted")
    list_filter = ("project", "posted")
    readonly_fields = ("system_quantity", "difference")
    actions = ["post_selected"]

    @admin.action(description="Post selected stocktakes (book differences)")
    def post_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.post_adjustment, "posted")


@admin.register(WastageRecord)
class WastageRecordAdmin(PostedLockMixin, AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("id", "project", "material", "date", "quantity", "reason", "value", "posted")
    list_filter = ("project", "reason", "posted")
    readonly_fields = ("value",)
    actions = ["post_selected"]

    @admin.action(description="Post selected wastage records (deduct from stock)")
    def post_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.post_wastage, "posted")


@admin.register(StockLedger)
class StockLedgerAdmin(admin.ModelAdmin):
    """Read-only view of the append-only stock ledger."""

    list_display = ("entry_date", "project", "material", "entry_type", "quantity", "unit_cost", "reference")
    list_filter = ("project", "entry_type", "material__category")
    search_fields = ("material__name", "material__code", "reference")
    date_hierarchy = "entry_date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

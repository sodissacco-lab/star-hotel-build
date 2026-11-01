from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html

from accounts.permissions import can
from core.admin import AuditedAdminMixin, run_workflow_action
from accounts.permissions import WorkflowError
from finance import receipts, services
from finance.models import Budget, Expense, Payment, Receipt


@admin.register(Budget)
class BudgetAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("project", "category", "phase", "amount")
    list_filter = ("project", "category")


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("date", "amount", "method", "reference")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False  # payments are recorded through the Payment screen


@admin.register(Expense)
class ExpenseAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("date", "project", "category", "description", "amount", "status", "paid", "outstanding_amount")
    list_filter = ("project", "category", "status")
    search_fields = ("description", "reference")
    date_hierarchy = "date"
    inlines = [PaymentInline]
    actions = ["approve_selected", "reject_selected"]

    @admin.display(description="Paid")
    def paid(self, obj):
        return f"{obj.paid_amount:,.0f}"

    @admin.display(description="Outstanding")
    def outstanding_amount(self, obj):
        return f"{obj.outstanding:,.0f}"

    def get_readonly_fields(self, request, obj=None):
        # Approved / source-generated expenses are never edited; correct by reversal.
        if obj and (obj.status == Expense.Status.APPROVED or obj.source_type):
            return [f.name for f in Expense._meta.fields]
        return ("status",)

    @admin.action(description="Approve selected expenses")
    def approve_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.approve_expense, "approved")

    @admin.action(description="Reject selected expenses")
    def reject_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.reject_expense, "rejected")


@admin.register(Payment)
class PaymentAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("date", "expense", "amount", "method", "reference")
    list_filter = ("method",)
    date_hierarchy = "date"

    def has_add_permission(self, request):
        return can(request.user, "record_payment") and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return False  # payments are immutable once recorded

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            rc = obj.receipts.filter(voided=False).first()
            if rc:
                messages.success(request, f"Payment recorded. Receipt {rc.number} issued - print it from Receipts.")


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    """Receipts are issued by the system. The only change allowed is voiding, with a reason."""

    list_display = ("number", "date", "payee_type", "payee_name", "amount", "method", "status", "print_link")
    list_filter = ("project", "payee_type", "voided", "method")
    search_fields = ("number", "payee_name", "reference", "purpose")
    date_hierarchy = "date"
    actions = ["print_selected"]
    fields = ("number", "project", "date", "payee_type", "payee_name", "amount", "method", "reference", "purpose",
              "payment", "payroll", "voided", "void_reason", "voided_by", "voided_at")

    def get_readonly_fields(self, request, obj=None):
        locked = [f for f in self.fields if f != "void_reason"]
        if obj is not None and obj.voided:
            locked.append("void_reason")
        return locked

    def has_add_permission(self, request):
        return False  # issued automatically

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        reason = form.cleaned_data.get("void_reason", "")
        if not obj.voided and reason.strip():
            try:
                receipts.void_receipt(obj, request.user, reason)
            except (WorkflowError, PermissionDenied) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"{obj.number} has been voided.")

    @admin.display(description="Status")
    def status(self, obj):
        return "VOID" if obj.voided else "Valid"

    @admin.display(description="Print")
    def print_link(self, obj):
        return format_html('<a href="{}" target="_blank">Print</a>', reverse("receipt_print", args=[obj.pk]))

    @admin.action(description="Print selected receipts")
    def print_selected(self, request, queryset):
        ids = ",".join(str(pk) for pk in queryset.values_list("pk", flat=True)[:100])
        return redirect(f"{reverse('receipt_batch')}?ids={ids}")
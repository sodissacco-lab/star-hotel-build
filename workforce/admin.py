from django.contrib import admin, messages

from accounts.permissions import WorkflowError, can
from core.admin import AuditedAdminMixin, run_workflow_action
from workforce import services
from workforce.models import (
    Attendance, Contract, Contractor, PaymentCertificate, Payroll, PayrollLine, Worker,
)


@admin.register(Contractor)
class ContractorAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("name", "contact_person", "phone", "insurance_expiry", "is_active")
    search_fields = ("name", "tin")
    list_filter = ("is_active",)


@admin.register(Contract)
class ContractAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("reference", "contractor", "project", "amount", "certified", "status")
    list_filter = ("project", "status")
    search_fields = ("reference", "contractor__name")

    @admin.display(description="Certified (UGX)")
    def certified(self, obj):
        return f"{obj.certified_gross:,.0f}"

    def get_readonly_fields(self, request, obj=None):
        # The amount cannot be edited once certificates exist; amend by a new contract/variation.
        if obj is not None and obj.certificates.exists():
            return ("project", "contractor", "amount", "retention_percent")
        return ()


@admin.register(PaymentCertificate)
class PaymentCertificateAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("number", "contract", "date", "gross_amount", "retention_amount", "net_amount", "status")
    list_filter = ("status", "contract__project")
    readonly_fields = ("number", "status", "retention_amount", "net_amount", "approved_by", "approved_at")
    actions = ["approve_selected", "reject_selected"]

    def get_readonly_fields(self, request, obj=None):
        base = tuple(super().get_readonly_fields(request, obj))
        if obj is not None and obj.status != PaymentCertificate.Status.DRAFT:
            return base + ("contract", "date", "description", "gross_amount", "cumulative_percent")
        return base

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != PaymentCertificate.Status.DRAFT:
            return False
        return super().has_delete_permission(request, obj)

    @admin.action(description="Approve selected certificates (books the net amount as an expense)")
    def approve_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.approve_certificate, "approved")

    @admin.action(description="Reject selected certificates")
    def reject_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.reject_certificate, "rejected")


@admin.register(Worker)
class WorkerAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("full_name", "trade", "project", "contractor", "daily_wage", "is_active")
    list_filter = ("project", "trade", "contractor", "is_active")
    search_fields = ("full_name", "phone")

    def has_add_permission(self, request):
        return can(request.user, "manage_workers") and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return can(request.user, "manage_workers") and super().has_change_permission(request, obj)

    def has_view_permission(self, request, obj=None):
        return can(request.user, "view_labour") or can(request.user, "record_attendance")

    def has_delete_permission(self, request, obj=None):
        return False  # deactivate instead of deleting


@admin.register(Attendance)
class AttendanceAdmin(AuditedAdminMixin, admin.ModelAdmin):
    list_display = ("date", "worker", "status", "hours_worked", "overtime_hours", "phase", "paid_in")
    list_filter = ("status", "worker__project", "worker__trade", "date")
    date_hierarchy = "date"
    search_fields = ("worker__full_name",)

    @admin.display(description="Paid in payroll")
    def paid_in(self, obj):
        return obj.payroll.number if obj.payroll_id else "–"

    def has_add_permission(self, request):
        return can(request.user, "record_attendance") and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.payroll_id:
            return False  # already paid
        return can(request.user, "record_attendance") and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.payroll_id:
            return False
        return can(request.user, "record_attendance") and super().has_delete_permission(request, obj)


class PayrollLineInline(admin.TabularInline):
    model = PayrollLine
    extra = 0
    fields = ("worker", "days_present", "regular_pay", "overtime_pay")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Payroll)
class PayrollAdmin(AuditedAdminMixin, admin.ModelAdmin):
    """Add a payroll by choosing the project and period; the lines are built automatically."""

    list_display = ("number", "project", "period_start", "period_end", "status", "total_amount")
    list_filter = ("project", "status")
    inlines = [PayrollLineInline]
    actions = ["approve_selected", "issue_receipts_selected"]

    @admin.display(description="Total (UGX)")
    def total_amount(self, obj):
        return f"{obj.total:,.0f}"

    def get_readonly_fields(self, request, obj=None):
        base = ("number", "status", "approved_by", "approved_at")
        if obj is not None:
            return base + ("project", "period_start", "period_end")
        return base

    def has_add_permission(self, request):
        return can(request.user, "build_payroll") and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.status != Payroll.Status.DRAFT:
            return False  # deleting a draft frees its attendance days for the next payroll
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            try:
                services.fill_payroll(obj, request.user)
            except WorkflowError as exc:
                messages.error(request, f"{obj.number}: {exc} The draft is empty; delete it and try again.")

    @admin.action(description="Approve selected payrolls (books wages as an expense)")
    def approve_selected(self, request, queryset):
        run_workflow_action(request, queryset, services.approve_payroll, "approved")

    @admin.action(description="Issue worker receipts (after wages are fully paid)")
    def issue_receipts_selected(self, request, queryset):
        from finance.receipts import issue_worker_receipts
        run_workflow_action(request, queryset, issue_worker_receipts, "receipted")
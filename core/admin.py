from django.contrib import messages
from django.core.exceptions import PermissionDenied

from accounts.permissions import WorkflowError


class AuditedAdminMixin:
    """Stamp created_by / updated_by from the logged-in admin user."""

    def save_model(self, request, obj, form, change):
        if not change and getattr(obj, "created_by_id", None) is None:
            obj.created_by = request.user
        if hasattr(obj, "updated_by_id"):
            obj.updated_by = request.user
        super().save_model(request, obj, form, change)


def run_workflow_action(request, queryset, func, success_label):
    """Run a service function over selected rows, reporting errors per row."""
    done = 0
    for obj in queryset:
        try:
            func(obj, request.user)
            done += 1
        except (WorkflowError, PermissionDenied) as exc:
            messages.error(request, f"{obj}: {exc}")
    if done:
        messages.success(request, f"{done} record(s) {success_label}.")

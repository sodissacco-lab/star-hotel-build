from django.db import transaction
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from finance.models import Expense


@transaction.atomic
def approve_expense(expense, user):
    """Approve a pending expense. The creator cannot approve their own entry."""
    require(user, "approve_expense")
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.status != Expense.Status.PENDING:
        raise WorkflowError("Only pending expenses can be approved.")
    if expense.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve an expense you recorded.")
    expense.status = Expense.Status.APPROVED
    expense.approved_by = user
    expense.approved_at = timezone.now()
    expense.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return expense


@transaction.atomic
def reject_expense(expense, user):
    require(user, "approve_expense")
    expense = Expense.objects.select_for_update().get(pk=expense.pk)
    if expense.status != Expense.Status.PENDING:
        raise WorkflowError("Only pending expenses can be rejected.")
    expense.status = Expense.Status.REJECTED
    expense.save(update_fields=["status", "updated_at"])
    return expense

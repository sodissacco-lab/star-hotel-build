from collections import Counter
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from boq.models import BOQItem, Variation
from inventory.models import MaterialIssueLine

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@transaction.atomic
def approve_variation(var, user):
    require(user, "approve_variation")
    var = Variation.objects.select_for_update().get(pk=var.pk)
    if var.status != Variation.Status.DRAFT:
        raise WorkflowError("Only draft variations can be approved.")
    if var.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a variation you raised.")
    item = BOQItem.objects.select_for_update().get(pk=var.item_id)
    new_qty = item.revised_quantity + var.quantity_change
    if new_qty < 0:
        raise WorkflowError(f"{item.code}: the variation would make the quantity negative.")
    if new_qty < item.quantity_done:
        raise WorkflowError(
            f"{item.code}: the new quantity ({new_qty}) is below the work already measured ({item.quantity_done})."
        )
    item.revised_quantity = new_qty
    item.save(update_fields=["revised_quantity", "updated_at"])
    var.status = Variation.Status.APPROVED
    var.approved_by = user
    var.approved_at = timezone.now()
    var.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return var


@transaction.atomic
def reject_variation(var, user):
    require(user, "approve_variation")
    var = Variation.objects.select_for_update().get(pk=var.pk)
    if var.status != Variation.Status.DRAFT:
        raise WorkflowError("Only draft variations can be rejected.")
    var.status = Variation.Status.REJECTED
    var.save(update_fields=["status", "updated_at"])
    return var


def item_report(project):
    """BOQ progress per item, plus estimate-vs-phase-budget comparison.

    Material usage (issued to site for the item's phase) is shown only when
    exactly one BOQ item uses that material in that phase, so the comparison
    is unambiguous.
    """
    dec = DecimalField(max_digits=16, decimal_places=3)
    items = list(
        BOQItem.objects.filter(project=project)
        .select_related("unit", "phase", "material")
        .annotate(done=Coalesce(Sum("measurements__quantity"), Value(ZERO, output_field=dec), output_field=dec))
    )
    issued = {
        (r["issue__phase_id"], r["material_id"]): r["q"]
        for r in MaterialIssueLine.objects.filter(
            issue__project=project, issue__posted=True, issue__phase__isnull=False
        ).values("issue__phase_id", "material_id").annotate(q=Sum("quantity_issued"))
    }
    pairs = Counter((i.phase_id, i.material_id) for i in items if i.phase_id and i.material_id)

    rows = []
    total_est = total_earned = ZERO
    est_by_phase = {}
    for it in items:
        est = it.revised_quantity * it.rate
        earned = it.done * it.rate
        used = usage_pct = None
        if it.phase_id and it.material_id and pairs[(it.phase_id, it.material_id)] == 1:
            used = issued.get((it.phase_id, it.material_id), ZERO)
            usage_pct = (used * HUNDRED / it.revised_quantity) if it.revised_quantity else None
        rows.append({
            "item": it, "done": it.done, "remaining": it.revised_quantity - it.done,
            "percent": (it.done * HUNDRED / it.revised_quantity) if it.revised_quantity else ZERO,
            "estimated": est, "earned": earned, "issued": used, "usage_pct": usage_pct,
            "overrun": usage_pct is not None and usage_pct > HUNDRED,
        })
        total_est += est
        total_earned += earned
        if it.phase_id:
            est_by_phase[it.phase_id] = est_by_phase.get(it.phase_id, ZERO) + est

    phase_rows = []
    for ph in project.phases.all():
        if ph.pk in est_by_phase or ph.budget:
            phase_rows.append({
                "phase": ph, "budget": ph.budget, "boq_estimate": est_by_phase.get(ph.pk, ZERO),
                "difference": ph.budget - est_by_phase.get(ph.pk, ZERO),
            })
    totals = {
        "estimated": total_est, "earned": total_earned,
        "percent": (total_earned * HUNDRED / total_est) if total_est else ZERO,
    }
    return rows, phase_rows, totals

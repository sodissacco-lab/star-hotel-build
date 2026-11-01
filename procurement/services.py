"""Requisition workflow: submit -> QS verifies (BOQ + budget) -> approve -> purchase order."""
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from finance.models import Budget, Category, Expense
from inventory.models import PurchaseOrder, PurchaseOrderLine
from procurement.models import Requisition, RequisitionLine

ZERO = Decimal("0")
S = Requisition.Status
COMMITTED = [S.VERIFIED, S.APPROVED, S.ORDERED]


def _lock(req):
    return Requisition.objects.select_for_update().get(pk=req.pk)


@transaction.atomic
def submit_requisition(req, user):
    req = _lock(req)
    if req.status != S.DRAFT:
        raise WorkflowError("Only draft requisitions can be submitted.")
    if not req.lines.exists():
        raise WorkflowError("Add at least one line before submitting.")
    if req.emergency and not req.justification.strip():
        raise WorkflowError("Emergency requisitions need a justification.")
    req.status = S.SUBMITTED
    req.save(update_fields=["status", "updated_at"])
    return req


@transaction.atomic
def verify_requisition(req, user):
    """Quantity surveyor check: quantities within the BOQ and the materials budget."""
    require(user, "verify_requisition")
    req = _lock(req)
    if req.status != S.SUBMITTED:
        raise WorkflowError("Only submitted requisitions can be verified.")
    if req.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot verify a requisition you raised.")

    problems = []
    for line in req.lines.select_related("boq_item", "material"):
        item = line.boq_item
        if item is None:
            continue
        if item.project_id != req.project_id:
            problems.append(f"{item.code} belongs to another project.")
            continue
        already = RequisitionLine.objects.filter(
            boq_item=item, requisition__status__in=COMMITTED
        ).exclude(requisition=req).aggregate(t=Sum("quantity"))["t"] or ZERO
        allowed = item.revised_quantity - already
        if line.quantity > allowed:
            problems.append(
                f"{item.code}: requesting {line.quantity} but only {max(allowed, ZERO)} remains of the BOQ quantity "
                f"({item.revised_quantity}; {already} already requisitioned). Raise a variation first."
            )

    material_budget = Budget.objects.filter(project=req.project, category=Category.MATERIALS) \
        .aggregate(t=Sum("amount"))["t"] or ZERO
    if material_budget > 0:
        spent = Expense.objects.filter(project=req.project, category=Category.MATERIALS,
                                       status=Expense.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or ZERO
        if spent + req.estimated_total > material_budget:
            problems.append(
                f"Estimated UGX {req.estimated_total:,.0f} would take materials spending "
                f"(UGX {spent:,.0f} so far) above the approved materials budget of UGX {material_budget:,.0f}."
            )
    if problems:
        raise WorkflowError(" | ".join(problems))

    req.status = S.VERIFIED
    req.verified_by = user
    req.verified_at = timezone.now()
    req.save(update_fields=["status", "verified_by", "verified_at", "updated_at"])
    return req


@transaction.atomic
def approve_requisition(req, user):
    require(user, "approve_requisition")
    req = _lock(req)
    allowed_from = (S.VERIFIED, S.SUBMITTED) if req.emergency else (S.VERIFIED,)
    if req.status not in allowed_from:
        raise WorkflowError(
            "Emergency requisitions can be approved once submitted; others must be verified by the quantity surveyor first."
            if req.emergency else "Only verified requisitions can be approved."
        )
    if req.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a requisition you raised.")
    req.status = S.APPROVED
    req.approved_by = user
    req.approved_at = timezone.now()
    req.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return req


@transaction.atomic
def reject_requisition(req, user):
    if not (user.is_superuser or _can_any(user, "verify_requisition", "approve_requisition")):
        raise WorkflowError("Your role is not permitted to reject requisitions.")
    req = _lock(req)
    if req.status not in (S.SUBMITTED, S.VERIFIED):
        raise WorkflowError("Only submitted or verified requisitions can be rejected.")
    req.status = S.REJECTED
    req.save(update_fields=["status", "updated_at"])
    return req


def _can_any(user, *caps):
    from accounts.permissions import can
    return any(can(user, c) for c in caps)


def _choose_supplier(req):
    quotes = list(req.quotations.select_related("supplier"))
    selected = [q for q in quotes if q.is_selected]
    if len(selected) > 1:
        raise WorkflowError("More than one quotation is marked as selected.")
    if selected:
        return selected[0].supplier
    if quotes:
        return min(quotes, key=lambda q: q.amount).supplier  # lowest quotation
    if req.preferred_supplier_id:
        return req.preferred_supplier
    raise WorkflowError("No quotation or preferred supplier to order from.")


@transaction.atomic
def create_po_from_requisition(req, user):
    """Create a DRAFT purchase order from an approved requisition. The PO then follows
    the normal submit -> approve route, with its own approval limits."""
    require(user, "create_po")
    req = _lock(req)
    if req.status != S.APPROVED:
        raise WorkflowError("Only approved requisitions can be turned into purchase orders.")
    minimum = getattr(settings, "MIN_QUOTATIONS", 1)
    if not req.emergency and req.quotations.count() < minimum:
        raise WorkflowError(f"At least {minimum} supplier quotation(s) must be recorded first.")
    supplier = _choose_supplier(req)

    po = PurchaseOrder.objects.create(
        project=req.project, supplier=supplier, requisition_ref=req.number,
        notes=f"Raised from {req.number}", created_by=user, updated_by=user,
    )
    for line in req.lines.select_related("material"):
        PurchaseOrderLine.objects.create(
            po=po, material=line.material, work_package=req.work_package,
            quantity=line.quantity, unit_price=line.estimated_unit_price,
        )
    req.status = S.ORDERED
    req.purchase_order = po
    req.save(update_fields=["status", "purchase_order", "updated_at"])
    return po

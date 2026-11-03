"""Requisition workflow: submit -> QS verifies (BOQ + budget) -> approve -> purchase order."""
from decimal import Decimal

from decimal import InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from accounts.permissions import WorkflowError, can, require
from finance.models import Budget, Category, Expense, Payment
from inventory.models import (
    GoodsReceipt, GoodsReceiptLine, Material, PurchaseOrder, PurchaseOrderLine, Supplier,
)
from procurement.models import DirectDelivery, DirectDeliveryLine, Requisition, RequisitionLine

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


# ---------------------------------------------------------------- deliveries without a purchase order
DD = DirectDelivery.Status


def _number(value, label, default=ZERO):
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise WorkflowError(f"{label}: enter a number.")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_direct_delivery(project, user, data):
    """Record goods that arrived without a purchase order. Nothing is booked into stock or charged to the
    supplier yet: the delivery waits as PENDING until approve_direct_delivery. `data` is a plain dict (see
    the mobile API for the keys)."""
    require(user, "receive_goods")
    with transaction.atomic():
        reason = " ".join(str(data.get("reason", "")).split())
        if not reason:
            raise WorkflowError("Say why this was received without a purchase order.")

        supplier = None
        if data.get("supplier"):
            supplier = Supplier.objects.filter(pk=_int(data["supplier"]), is_active=True).first()
            if supplier is None:
                raise WorkflowError("Choose a supplier from the list.")
        else:
            name = " ".join(str(data.get("supplier_name", "")).split())
            if not name:
                raise WorkflowError("Choose a supplier, or type the new supplier's name.")
            supplier = Supplier.objects.filter(name__iexact=name).first()
            if supplier is None:
                supplier = Supplier.objects.create(
                    name=name[:200], phone=str(data.get("supplier_phone", ""))[:30], created_by=user, updated_by=user)
            elif not supplier.is_active:
                raise WorkflowError(f"{supplier.name} is marked inactive. Ask the office to reactivate the supplier.")

        raw_day = data.get("date")
        day = timezone.localdate() if not raw_day else parse_date(str(raw_day))
        if day is None:
            raise WorkflowError("Date: use the format YYYY-MM-DD.")

        phase = None
        if data.get("phase"):
            phase = project.phases.filter(pk=_int(data["phase"])).first()
            if phase is None:
                raise WorkflowError("That work area is not part of this project.")

        invoice_no = str(data.get("invoice_no", "")).strip()[:60]
        duplicate = DirectDelivery.find_duplicate_invoice(supplier, invoice_no)
        if duplicate:
            raise WorkflowError(f"Invoice {invoice_no} from {supplier.name} is already recorded ({duplicate}). "
                                "Check for a duplicate before recording it again.")

        rows = data.get("lines")
        if not isinstance(rows, list) or not rows:
            raise WorkflowError("Add at least one material.")
        entries = []
        for n, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise WorkflowError(f"Line {n}: invalid.")
            material = Material.objects.filter(pk=_int(row.get("material")), is_active=True).first()
            delivered = _number(row.get("delivered"), f"Line {n} delivered")
            accepted = delivered if row.get("accepted") in (None, "") else _number(row.get("accepted"), f"Line {n} accepted")
            price = _number(row.get("unit_price"), f"Line {n} unit price")
            if material is None:
                raise WorkflowError(f"Line {n}: choose a material.")
            if delivered <= 0:
                raise WorkflowError(f"Line {n}: the quantity delivered must be above zero.")
            if accepted < 0 or accepted > delivered:
                raise WorkflowError(f"Line {n} ({material.name}): accepted must be between 0 and the quantity delivered.")
            if price <= 0:
                raise WorkflowError(f"Line {n} ({material.name}): enter the price paid per unit.")
            entries.append((material, delivered, accepted, price, str(row.get("condition", ""))[:200]))

        transport = _number(data.get("transport_charges"), "Transport charges")
        other = _number(data.get("other_charges"), "Other charges")
        paid = _number(data.get("amount_paid"), "Amount paid")
        if min(transport, other, paid) < 0:
            raise WorkflowError("Charges and the amount paid cannot be negative.")
        accepted_value = sum((a * p for _, _, a, p, _ in entries), ZERO)
        if paid > accepted_value:
            raise WorkflowError(
                f"The amount paid (UGX {paid:,.0f}) is more than the value of the goods accepted "
                f"(UGX {accepted_value:,.0f}). Enter transport and other charges separately.")
        method = str(data.get("payment_method") or "cash")
        if method not in DirectDelivery.PayMethod.values:
            raise WorkflowError("Choose a valid payment method.")

        dd = DirectDelivery.objects.create(
            project=project, supplier=supplier, delivery_date=day,
            delivery_note_no=str(data.get("delivery_note", ""))[:60], invoice_no=invoice_no,
            vehicle_reg=str(data.get("vehicle_reg", ""))[:30], driver_name=str(data.get("driver_name", ""))[:120],
            driver_phone=str(data.get("driver_phone", ""))[:30], phase=phase, reason=reason[:300],
            notes=str(data.get("notes", "")), transport_charges=transport, other_charges=other,
            amount_paid=paid, payment_method=method, payment_reference=str(data.get("payment_reference", ""))[:60],
            created_by=user, updated_by=user)
        for material, delivered, accepted, price, condition in entries:
            DirectDeliveryLine.objects.create(
                delivery=dd, material=material, quantity_delivered=delivered, quantity_accepted=accepted,
                quantity_rejected=delivered - accepted, unit_price=price, condition=condition)
        return dd


@transaction.atomic
def approve_direct_delivery(dd, user):
    """Approve a delivery without an order: it becomes an approved purchase order, a posted goods receipt
    (stock + material expense), the delivery-charge expenses and, if paid on the spot, the supplier payment."""
    from documents.models import Attachment
    from inventory import services as stock

    require(user, "approve_direct_delivery")
    dd = DirectDelivery.objects.select_for_update().get(pk=dd.pk)
    if dd.status != DD.PENDING:
        raise WorkflowError("Only deliveries waiting for approval can be approved.")
    if dd.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a delivery you recorded.")
    lines = [l for l in dd.lines.select_related("material") if l.quantity_accepted > 0]
    if not lines:
        raise WorkflowError("All the goods were rejected, so there is nothing to book into stock. Reject this record instead.")
    if dd.grand_total > settings.PO_APPROVAL_THRESHOLD_UGX:
        require(user, "approve_large_po")  # big unordered buys need the Owner or Admin
    receiver = dd.created_by
    if receiver is None or not can(receiver, "receive_goods"):
        raise WorkflowError("The person who recorded this delivery no longer has permission to receive goods.")

    now = timezone.now()
    po = PurchaseOrder.objects.create(
        project=dd.project, supplier=dd.supplier, order_date=dd.delivery_date, requisition_ref=dd.number,
        status=PurchaseOrder.Status.APPROVED, approved_by=user, approved_at=now,
        notes=f"Raised from delivery without an order {dd.number}. Reason: {dd.reason}",
        created_by=dd.created_by, updated_by=user)
    po_lines = {
        l.pk: PurchaseOrderLine.objects.create(po=po, material=l.material, quantity=l.quantity_accepted, unit_price=l.unit_price)
        for l in lines
    }
    grn = GoodsReceipt.objects.create(
        po=po, receipt_date=dd.delivery_date, delivery_note_no=dd.delivery_note_no, inspected_by=str(dd.created_by)[:120],
        notes=f"Delivery without an order {dd.number}", created_by=dd.created_by, updated_by=user)
    for l in lines:
        GoodsReceiptLine.objects.create(
            receipt=grn, po_line=po_lines[l.pk], quantity_delivered=l.quantity_delivered,
            quantity_accepted=l.quantity_accepted, quantity_rejected=l.quantity_rejected, condition=l.condition)
    try:
        stock.post_goods_receipt(grn, receiver)  # stock ledger + approved material expense
    except WorkflowError as exc:
        raise WorkflowError(f"Could not book the goods in: {exc}")

    if dd.phase_id:
        Expense.objects.filter(source_type="inventory.goodsreceipt", source_id=grn.pk).update(phase=dd.phase)
    for amount, category, label in ((dd.transport_charges, Category.TRANSPORT, "Transport and loading"),
                                    (dd.other_charges, Category.MISC, "Other delivery charges")):
        if amount > 0:
            Expense.objects.create(
                project=dd.project, phase=dd.phase, date=dd.delivery_date, category=category,
                description=f"{label} for delivery {dd.number}", amount=amount, supplier=dd.supplier,
                reference=dd.delivery_note_no or dd.number, status=Expense.Status.APPROVED,
                approved_by=user, approved_at=now, created_by=dd.created_by,
                source_type="procurement.directdelivery", source_id=dd.pk)
    if dd.amount_paid > 0:
        try:
            Payment.objects.create(
                purchase_order=po, date=dd.delivery_date, amount=dd.amount_paid, method=dd.payment_method or "cash",
                reference=(dd.payment_reference or dd.invoice_no or dd.number)[:60], created_by=dd.created_by,
                updated_by=user)
        except ValidationError as exc:
            raise WorkflowError("Could not record the payment: " + " ".join(exc.messages))

    dd.status = DD.APPROVED
    dd.approved_by, dd.approved_at = user, now
    dd.purchase_order, dd.receipt = po, grn
    dd.updated_by = user
    dd.save(update_fields=["status", "approved_by", "approved_at", "purchase_order", "receipt", "updated_by", "updated_at"])
    Attachment.objects.filter(direct_delivery=dd).update(purchase_order=po)
    return dd


@transaction.atomic
def reject_direct_delivery(dd, user, note=""):
    require(user, "approve_direct_delivery")
    dd = DirectDelivery.objects.select_for_update().get(pk=dd.pk)
    if dd.status != DD.PENDING:
        raise WorkflowError("Only deliveries waiting for approval can be rejected.")
    dd.status = DD.REJECTED
    dd.rejection_note = str(note or "")[:300]
    dd.updated_by = user
    dd.save(update_fields=["status", "rejection_note", "updated_by", "updated_at"])
    return dd


def last_prices():
    """{material_id: unit price on the most recent receipt}, so the form can show what was paid last time."""
    from inventory.models import StockLedger
    out = {}
    rows = StockLedger.objects.filter(entry_type=StockLedger.Type.RECEIPT).order_by("-entry_date", "-id") \
        .values_list("material_id", "unit_cost")[:3000]
    for material_id, cost in rows:
        out.setdefault(material_id, cost)
    return out
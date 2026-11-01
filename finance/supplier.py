"""Supplier accounts: what was ordered, delivered, paid in advance, and what is still owed.

Nothing here is typed in by hand. The figures are computed from the purchase order lines,
the posted goods receipts, the supplier payments and the demand notes, so every screen,
receipt and report shows the same numbers.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from finance.models import DemandNote, Expense, Payment, PaymentAllocation
from inventory.models import PurchaseOrder

ZERO = Decimal("0")
LIVE_STATUSES = (PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL, PurchaseOrder.Status.RECEIVED)
GRN = "inventory.goodsreceipt"


# ------------------------------------------------------------------ the account
def po_account(po):
    """Ordered / delivered / paid / advance / balance for one purchase order, with item lines."""
    lines, ordered, delivered = [], ZERO, ZERO
    for pl in po.lines.select_related("material__unit"):
        got = pl.received_quantity
        lines.append({
            "material": pl.material.name, "code": pl.material.code, "unit": pl.material.unit.abbreviation,
            "ordered": pl.quantity, "delivered": got, "outstanding": max(pl.quantity - got, ZERO),
            "unit_price": pl.unit_price, "ordered_value": pl.line_total, "delivered_value": got * pl.unit_price,
        })
        ordered += pl.line_total
        delivered += got * pl.unit_price
    by_status = dict(po.payments.values_list("status").annotate(t=Sum("amount")).order_by())
    paid = by_status.get(Payment.Status.APPROVED, ZERO)
    pending = by_status.get(Payment.Status.PENDING, ZERO)
    demanded = DemandNote.objects.filter(purchase_order=po).exclude(status=DemandNote.Status.REJECTED) \
        .aggregate(t=Sum("amount"))["t"] or ZERO
    return {
        "po": po, "ordered": ordered, "delivered": delivered, "paid": paid, "pending": pending,
        "demanded": demanded, "advance": max(paid - delivered, ZERO), "balance_due": max(delivered - paid, ZERO),
        "undelivered_value": max(ordered - delivered, ZERO), "lines": lines,
        "fully_delivered": bool(lines) and all(l["outstanding"] <= 0 for l in lines),
    }


def position_text(acct):
    """(tone, sentence) describing where the account stands, used on receipts and statements."""
    if acct["advance"] > 0:
        if acct["delivered"] <= 0:
            return "advance", "ADVANCE PAYMENT - the goods have not yet been delivered"
        return "advance", "ADVANCE OUTSTANDING - part of the goods has not yet been delivered"
    if acct["balance_due"] > 0:
        return "due", "BALANCE DUE TO SUPPLIER for goods already delivered"
    if acct["fully_delivered"] and acct["paid"] >= acct["ordered"]:
        return "paid", "PAID IN FULL - all goods delivered"
    return "paid", "NO BALANCE on goods delivered to date"


# ------------------------------------------------------------------ paying a supplier
def validate_po_payment(payment, po=None):
    po = po or payment.purchase_order
    if po.status not in LIVE_STATUSES:
        raise ValidationError("Suppliers can only be paid against an approved purchase order.")
    if payment.amount is None:
        return
    acct = po_account(po)
    committed = acct["paid"] + acct["pending"]
    if committed + payment.amount > acct["ordered"]:
        raise ValidationError(
            f"{po.number} is for UGX {acct['ordered']:,.0f} and UGX {committed:,.0f} is already paid or awaiting "
            f"approval. The most you can pay now is UGX {max(acct['ordered'] - committed, ZERO):,.0f}.")
    dn = payment.demand_note
    if dn is not None:
        if dn.purchase_order_id != po.pk:
            raise ValidationError({"demand_note": "This demand note belongs to a different purchase order."})
        if dn.status != DemandNote.Status.APPROVED:
            raise ValidationError({"demand_note": "The demand note must be approved for payment first."})
        open_amount = dn.amount - dn.paid - dn.pending
        if payment.amount > open_amount:
            raise ValidationError(
                f"The demand note has only UGX {max(open_amount, ZERO):,.0f} left to pay.")


def prepare_po_payment(payment):
    """Called under a database transaction just before a PO payment is first saved."""
    po = PurchaseOrder.objects.select_for_update().get(pk=payment.purchase_order_id)
    try:
        validate_po_payment(payment, po)
    except ValidationError as exc:
        raise ValidationError(exc.messages)
    acct = po_account(po)
    unpaid_delivered = max(acct["delivered"] - acct["paid"] - acct["pending"], ZERO)
    if payment.amount > unpaid_delivered:  # the excess is an advance and needs approval
        payment.status = Payment.Status.PENDING
    else:
        payment.status = Payment.Status.APPROVED
        payment.approved_at = timezone.now()


def finalise_payment(payment):
    """A payment became real: apply it to deliveries and issue its receipt."""
    from finance import receipts
    if payment.purchase_order_id:
        reallocate(payment.purchase_order)
    receipts.issue_for_payment(payment)


def reallocate(po):
    """Rebuild how approved supplier payments are applied to the expenses of delivered goods (oldest first)."""
    receipt_ids = list(po.receipts.values_list("pk", flat=True))
    expenses = list(Expense.objects.filter(source_type=GRN, source_id__in=receipt_ids,
                                           status=Expense.Status.APPROVED).order_by("date", "id"))
    PaymentAllocation.objects.filter(payment__purchase_order=po).delete()
    pool = [[p, p.amount] for p in Payment.objects.filter(purchase_order=po, status=Payment.Status.APPROVED)
            .order_by("date", "id")]
    new = []
    for exp in expenses:
        need = exp.amount
        for item in pool:
            if need <= 0:
                break
            take = min(item[1], need)
            if take > 0:
                new.append(PaymentAllocation(payment=item[0], expense=exp, amount=take))
                item[1] -= take
                need -= take
    PaymentAllocation.objects.bulk_create(new)


@transaction.atomic
def approve_payment(payment, user):
    require(user, "approve_advance")
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        raise WorkflowError("Only payments waiting for approval can be approved.")
    if payment.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a payment you recorded.")
    po = PurchaseOrder.objects.select_for_update().get(pk=payment.purchase_order_id)
    acct = po_account(po)
    advance_after = max(acct["paid"] + payment.amount - acct["delivered"], ZERO)
    limit = acct["ordered"] * settings.ADVANCE_MAX_PERCENT / 100
    if advance_after > limit:
        require(user, "approve_large_advance")  # only the Owner/Admin may exceed the advance limit
    payment.status = Payment.Status.APPROVED
    payment.approved_by, payment.approved_at = user, timezone.now()
    payment.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    finalise_payment(payment)
    return payment


@transaction.atomic
def reject_payment(payment, user):
    require(user, "approve_advance")
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != Payment.Status.PENDING:
        raise WorkflowError("Only payments waiting for approval can be rejected.")
    payment.status = Payment.Status.REJECTED
    payment.approved_by, payment.approved_at = user, timezone.now()
    payment.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return payment


# ------------------------------------------------------------------ demand notes
@transaction.atomic
def verify_demand_note(dn, user):
    """Check a demand against what has actually been delivered."""
    require(user, "verify_demand")
    dn = DemandNote.objects.select_for_update().get(pk=dn.pk)
    if dn.status != DemandNote.Status.RECEIVED:
        raise WorkflowError("Only newly received demand notes can be verified.")
    acct = po_account(dn.purchase_order)
    already = DemandNote.objects.filter(purchase_order=dn.purchase_order, status__in=(
        DemandNote.Status.VERIFIED, DemandNote.Status.APPROVED)).exclude(pk=dn.pk).aggregate(t=Sum("amount"))["t"] or ZERO
    claimable = max(acct["delivered"] - already, ZERO)
    dn.is_advance_request = dn.amount > claimable
    dn.status = DemandNote.Status.VERIFIED
    dn.verified_by, dn.verified_at = user, timezone.now()
    dn.save(update_fields=["is_advance_request", "status", "verified_by", "verified_at", "updated_at"])
    return dn


@transaction.atomic
def approve_demand_note(dn, user):
    require(user, "approve_demand")
    dn = DemandNote.objects.select_for_update().get(pk=dn.pk)
    if dn.status != DemandNote.Status.VERIFIED:
        raise WorkflowError("A demand note must be verified before it is approved.")
    if dn.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a demand note you recorded.")
    dn.status = DemandNote.Status.APPROVED
    dn.approved_by, dn.approved_at = user, timezone.now()
    dn.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return dn


@transaction.atomic
def reject_demand_note(dn, user):
    if not (user.is_superuser or user.role in ("admin", "quantity_surveyor", "accountant", "owner", "project_manager")):
        raise WorkflowError("Your role is not permitted to reject demand notes.")
    dn = DemandNote.objects.select_for_update().get(pk=dn.pk)
    if dn.status not in (DemandNote.Status.RECEIVED, DemandNote.Status.VERIFIED):
        raise WorkflowError("Only demand notes not yet approved can be rejected.")
    if dn.payments.exclude(status=Payment.Status.REJECTED).exists():
        raise WorkflowError("This demand note already has payments.")
    dn.status = DemandNote.Status.REJECTED
    dn.save(update_fields=["status", "updated_at"])
    return dn


# ------------------------------------------------------------------ overviews
def paid_by_expense(expense_ids):
    """{expense_id: amount paid} - direct payments plus supplier payments applied to the expense."""
    ids = list(expense_ids)
    paid = defaultdict(lambda: ZERO)
    for eid, t in Payment.objects.filter(expense_id__in=ids, status=Payment.Status.APPROVED) \
            .order_by().values_list("expense_id").annotate(t=Sum("amount")):
        paid[eid] += t
    for eid, t in PaymentAllocation.objects.filter(expense_id__in=ids).order_by().values_list("expense_id").annotate(t=Sum("amount")):
        paid[eid] += t
    return paid


def live_orders(project):
    return PurchaseOrder.objects.filter(project=project, status__in=LIVE_STATUSES).select_related("supplier")


def supplier_summary(project):
    """One row per supplier across the project's live purchase orders."""
    rows = {}
    for po in live_orders(project):
        a = po_account(po)
        r = rows.setdefault(po.supplier_id, {"supplier": po.supplier, "orders": 0, "ordered": ZERO, "delivered": ZERO,
                                              "paid": ZERO, "pending": ZERO, "advance": ZERO, "balance_due": ZERO})
        r["orders"] += 1
        for k in ("ordered", "delivered", "paid", "pending", "advance", "balance_due"):
            r[k] += a[k]
    return sorted(rows.values(), key=lambda r: (-r["advance"], -r["balance_due"], r["supplier"].name))


def advances_overview(project, today=None):
    """Orders where money has been paid ahead of delivery, flagging the ones waiting too long."""
    today = today or timezone.localdate()
    limit = timedelta(days=settings.ADVANCE_ALERT_DAYS)
    out = []
    for po in live_orders(project):
        a = po_account(po)
        if a["advance"] <= 0:
            continue
        first = po.payments.filter(status=Payment.Status.APPROVED).order_by("date").values_list("date", flat=True).first()
        since = first or today
        out.append({"po": po, "supplier": po.supplier, "advance": a["advance"], "since": since,
                    "days": (today - since).days, "stale": (today - since) > limit,
                    "outstanding_lines": [l for l in a["lines"] if l["outstanding"] > 0]})
    return sorted(out, key=lambda r: -r["days"])


def statement(project, supplier):
    """Everything about one supplier on one project: orders, items supplied, payments, demands."""
    from inventory.models import GoodsReceiptLine

    orders = list(PurchaseOrder.objects.filter(project=project, supplier=supplier).exclude(
        status__in=(PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.CANCELLED)).order_by("order_date", "id"))
    accounts = [po_account(po) for po in orders]
    supplied = list(GoodsReceiptLine.objects.filter(receipt__po__in=orders, receipt__posted=True, quantity_accepted__gt=0)
                    .select_related("receipt__po", "po_line__material__unit").order_by("receipt__receipt_date", "id"))
    supplied_rows = [{
        "date": l.receipt.receipt_date, "po": l.receipt.po.number, "delivery_note": l.receipt.delivery_note_no,
        "material": l.po_line.material.name, "unit": l.po_line.material.unit.abbreviation,
        "quantity": l.quantity_accepted, "unit_price": l.po_line.unit_price,
        "value": l.quantity_accepted * l.po_line.unit_price,
    } for l in supplied]
    payments = list(Payment.objects.filter(purchase_order__in=orders).select_related("purchase_order", "demand_note")
                    .order_by("date", "id"))
    demands = list(DemandNote.objects.filter(purchase_order__in=orders).select_related("purchase_order").order_by("date", "id"))
    totals = {k: sum((a[k] for a in accounts), ZERO) for k in ("ordered", "delivered", "paid", "pending", "advance", "balance_due")}
    return {"orders": accounts, "supplied": supplied_rows, "payments": payments, "demands": demands, "totals": totals,
            "supplied_total": sum((r["value"] for r in supplied_rows), ZERO)}

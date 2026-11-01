"""Stock and procurement business logic.

All stock movements go through the functions below, which:
  * check the user's role capability,
  * validate the business rules (no negative stock, no over-receiving ...),
  * write append-only StockLedger rows, and
  * create the matching finance records (material receipts become approved expenses).
Everything runs inside a database transaction.
"""
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from finance.models import Category, Expense
from inventory.models import (
    GoodsReceipt, Material, MaterialIssue, MaterialReturn, PurchaseOrder,
    StockAdjustment, StockLedger, WastageRecord,
)

ZERO = Decimal("0")
T = StockLedger.Type


# ------------------------------------------------------------------ queries
def stock_balance(project, material):
    total = StockLedger.objects.filter(project=project, material=material).aggregate(t=Sum("quantity"))["t"]
    return total or ZERO


def average_cost(project, material):
    """Weighted-average unit cost of goods received (falls back to the standard price)."""
    agg = StockLedger.objects.filter(
        project=project, material=material, entry_type=T.RECEIPT
    ).aggregate(
        q=Sum("quantity"),
        v=Sum(ExpressionWrapper(F("quantity") * F("unit_cost"), output_field=DecimalField(max_digits=24, decimal_places=4))),
    )
    qty = agg["q"] or ZERO
    if qty <= 0:
        return material.standard_price
    return (agg["v"] / qty).quantize(Decimal("0.01"))


def stock_summary(project):
    """One row per material that has ledger activity, plus project-wide totals."""
    value = ExpressionWrapper(F("quantity") * F("unit_cost"), output_field=DecimalField(max_digits=24, decimal_places=4))
    rows = (
        StockLedger.objects.filter(project=project)
        .values("material_id")
        .annotate(
            balance=Sum("quantity"),
            received=Sum("quantity", filter=Q(entry_type=T.RECEIPT)),
            received_value=Sum(value, filter=Q(entry_type=T.RECEIPT)),
            issued=Sum("quantity", filter=Q(entry_type=T.ISSUE)),
            issued_value=Sum(value, filter=Q(entry_type=T.ISSUE)),
            returned=Sum("quantity", filter=Q(entry_type=T.RETURN)),
            returned_value=Sum(value, filter=Q(entry_type=T.RETURN)),
            wasted=Sum("quantity", filter=Q(entry_type=T.WASTAGE)),
            wasted_value=Sum(value, filter=Q(entry_type=T.WASTAGE)),
        )
    )
    materials = {m.pk: m for m in Material.objects.select_related("unit", "category").filter(pk__in=[r["material_id"] for r in rows])}
    out, totals = [], dict(stock_value=ZERO, purchased_value=ZERO, consumed_value=ZERO, wastage_value=ZERO)
    for r in rows:
        m = materials[r["material_id"]]
        received = r["received"] or ZERO
        avg = (r["received_value"] / received).quantize(Decimal("0.01")) if received > 0 else m.standard_price
        balance = r["balance"] or ZERO
        stock_value = balance * avg
        # issue values are negative and return values positive, so consumed = issued cost - returned cost
        consumed_value = -(r["issued_value"] or ZERO) - (r["returned_value"] or ZERO)
        wastage_value = -(r["wasted_value"] or ZERO)
        out.append({
            "material": m, "balance": balance, "avg_cost": avg, "stock_value": stock_value,
            "received": received, "issued": -(r["issued"] or ZERO), "returned": r["returned"] or ZERO,
            "wasted": -(r["wasted"] or ZERO),
            "below_reorder": balance <= m.reorder_level,
        })
        totals["stock_value"] += stock_value
        totals["purchased_value"] += r["received_value"] or ZERO
        totals["consumed_value"] += consumed_value
        totals["wastage_value"] += wastage_value
    out.sort(key=lambda x: (x["material"].category.name, x["material"].name))
    return out, totals


# ------------------------------------------------------------------ helpers
def _lock_materials(material_ids):
    list(Material.objects.select_for_update().filter(pk__in=set(material_ids)))


def _ledger(project, material, entry_type, quantity, unit_cost, ref, user, entry_date, source, work_package=None):
    return StockLedger.objects.create(
        project=project, material=material, entry_type=entry_type, quantity=quantity,
        unit_cost=unit_cost, reference=ref, created_by=user, entry_date=entry_date,
        source_type=source._meta.label_lower, source_id=source.pk, work_package=work_package,
    )


def _mark_posted(obj):
    obj.posted = True
    obj.posted_at = timezone.now()
    obj.save(update_fields=["posted", "posted_at"])


# ------------------------------------------------------------------ purchase orders
@transaction.atomic
def submit_po(po, user):
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if po.status != PurchaseOrder.Status.DRAFT:
        raise WorkflowError("Only draft purchase orders can be submitted.")
    if not po.lines.exists():
        raise WorkflowError("Add at least one line before submitting.")
    po.status = PurchaseOrder.Status.SUBMITTED
    po.save(update_fields=["status", "updated_at"])
    return po


@transaction.atomic
def approve_po(po, user):
    require(user, "approve_po")
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if po.status != PurchaseOrder.Status.SUBMITTED:
        raise WorkflowError("Only submitted purchase orders can be approved.")
    if po.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a purchase order you created.")
    if po.total > settings.PO_APPROVAL_THRESHOLD_UGX:
        require(user, "approve_large_po")
    po.status = PurchaseOrder.Status.APPROVED
    po.approved_by = user
    po.approved_at = timezone.now()
    po.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return po


# ------------------------------------------------------------------ goods receipt
@transaction.atomic
def post_goods_receipt(receipt, user):
    require(user, "receive_goods")
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.posted:
        raise WorkflowError("This receipt is already posted.")
    po = receipt.po
    if po.status not in (PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL):
        raise WorkflowError("Goods can only be received against an approved purchase order.")
    lines = list(receipt.lines.select_related("po_line__material", "po_line__work_package__phase"))
    if not lines:
        raise WorkflowError("The receipt has no lines.")

    pending = {}
    for line in lines:
        pl = line.po_line
        if pl.po_id != po.pk:
            raise WorkflowError(f"{pl.material.name} does not belong to {po.number}.")
        if line.quantity_accepted + line.quantity_rejected != line.quantity_delivered:
            raise WorkflowError(f"{pl.material.name}: accepted + rejected must equal delivered.")
        pending[pl.pk] = pending.get(pl.pk, ZERO) + line.quantity_accepted
        outstanding = pl.quantity - pl.received_quantity
        if pending[pl.pk] > outstanding:
            raise WorkflowError(
                f"{pl.material.name}: accepting {pending[pl.pk]} exceeds the outstanding {outstanding} on {po.number}."
            )

    _lock_materials(l.po_line.material_id for l in lines)
    ref = receipt.delivery_note_no or f"GRN-{receipt.pk}"
    for line in lines:
        pl = line.po_line
        if line.quantity_accepted > 0:
            _ledger(po.project, pl.material, T.RECEIPT, line.quantity_accepted, pl.unit_price,
                    ref, user, receipt.receipt_date, receipt, pl.work_package)
            phase = pl.work_package.phase if pl.work_package_id else None
            Expense.objects.create(
                project=po.project, phase=phase, date=receipt.receipt_date, category=Category.MATERIALS,
                description=f"{pl.material.name} x {line.quantity_accepted} ({po.number})",
                amount=line.quantity_accepted * pl.unit_price, supplier=po.supplier, reference=ref,
                status=Expense.Status.APPROVED, approved_by=po.approved_by, approved_at=timezone.now(),
                created_by=user, source_type=receipt._meta.label_lower, source_id=receipt.pk,
            )

    _mark_posted(receipt)
    fully = all(pl.received_quantity >= pl.quantity for pl in po.lines.all())
    po.status = PurchaseOrder.Status.RECEIVED if fully else PurchaseOrder.Status.PARTIAL
    po.save(update_fields=["status", "updated_at"])
    from finance.supplier import reallocate  # advances already paid are applied to this delivery
    reallocate(po)
    return receipt


# ------------------------------------------------------------------ issues, returns
@transaction.atomic
def post_issue(issue, user):
    require(user, "issue_materials")
    issue = MaterialIssue.objects.select_for_update().get(pk=issue.pk)
    if issue.posted:
        raise WorkflowError("This issue is already posted.")
    lines = list(issue.lines.select_related("material"))
    if not lines:
        raise WorkflowError("The issue has no lines.")
    _lock_materials(l.material_id for l in lines)
    for line in lines:
        available = stock_balance(issue.project, line.material)
        if line.quantity_issued > available:
            raise WorkflowError(
                f"{line.material.name}: cannot issue {line.quantity_issued}; only {available} in stock."
            )
        cost = average_cost(issue.project, line.material)
        _ledger(issue.project, line.material, T.ISSUE, -line.quantity_issued, cost,
                f"ISS-{issue.pk}", user, issue.issue_date, issue, issue.work_package)
    _mark_posted(issue)
    return issue


@transaction.atomic
def post_return(ret, user):
    require(user, "issue_materials")
    ret = MaterialReturn.objects.select_for_update().get(pk=ret.pk)
    if ret.posted:
        raise WorkflowError("This return is already posted.")
    _lock_materials([ret.material_id])
    cost = average_cost(ret.project, ret.material)
    _ledger(ret.project, ret.material, T.RETURN, ret.quantity, cost, f"RET-{ret.pk}", user, ret.return_date, ret)
    _mark_posted(ret)
    return ret


# ------------------------------------------------------------------ stocktake, wastage
@transaction.atomic
def post_adjustment(adj, user):
    require(user, "adjust_stock")
    adj = StockAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.posted:
        raise WorkflowError("This adjustment is already posted.")
    if adj.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: a different authorised user must post your stocktake.")
    _lock_materials([adj.material_id])
    system = stock_balance(adj.project, adj.material)
    diff = adj.counted_quantity - system
    if diff != 0:
        _ledger(adj.project, adj.material, T.ADJUSTMENT, diff, average_cost(adj.project, adj.material),
                f"ADJ-{adj.pk}", user, adj.date, adj)
    adj.system_quantity = system
    adj.difference = diff
    adj.posted = True
    adj.posted_at = timezone.now()
    adj.save(update_fields=["system_quantity", "difference", "posted", "posted_at"])
    return adj


@transaction.atomic
def post_wastage(w, user):
    require(user, "record_wastage")
    w = WastageRecord.objects.select_for_update().get(pk=w.pk)
    if w.posted:
        raise WorkflowError("This wastage record is already posted.")
    _lock_materials([w.material_id])
    available = stock_balance(w.project, w.material)
    if w.quantity > available:
        raise WorkflowError(f"{w.material.name}: wastage {w.quantity} exceeds stock on hand ({available}).")
    cost = average_cost(w.project, w.material)
    _ledger(w.project, w.material, T.WASTAGE, -w.quantity, cost, f"WST-{w.pk}", user, w.date, w)
    w.value = w.quantity * cost
    w.posted = True
    w.posted_at = timezone.now()
    w.save(update_fields=["value", "posted", "posted_at"])
    return w
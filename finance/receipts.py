"""Issuing and voiding receipts.

* Every Payment automatically gets a receipt for its payee (supplier, contractor or other).
* Wages are acknowledged per worker: once a payroll's wages are fully paid, one receipt per worker.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from finance import supplier
from finance.models import Expense, Payment, Receipt

PAYROLL = "workforce.payroll"
CERTIFICATE = "workforce.paymentcertificate"
CONTRACTOR_ADVANCE = "workforce.contractoradvance"
WORKER_ADVANCE = "workforce.workeradvance"


def _money(value):
    return str(Decimal(value).quantize(Decimal("0.01")))


def _qty(value):
    return str(Decimal(value).quantize(Decimal("0.001")))


def _supplier_snapshot(payment, acct, dn):
    """Items and account position as they stand when the receipt is issued."""
    tone, status = supplier.position_text(acct)
    return {
        "kind": "supplier", "po": acct["po"].number, "tone": tone, "status": status,
        "ordered": _money(acct["ordered"]), "delivered": _money(acct["delivered"]),
        "paid_before": _money(acct["paid"] - payment.amount), "this_payment": _money(payment.amount),
        "paid_total": _money(acct["paid"]), "advance": _money(acct["advance"]), "balance_due": _money(acct["balance_due"]),
        "lines": [{
            "material": l["material"], "unit": l["unit"], "ordered": _qty(l["ordered"]), "delivered": _qty(l["delivered"]),
            "outstanding": _qty(l["outstanding"]), "unit_price": _money(l["unit_price"]),
            "ordered_value": _money(l["ordered_value"]), "delivered_value": _money(l["delivered_value"]),
        } for l in acct["lines"]],
        "demand_note": None if dn is None else {
            "number": dn.number, "supplier_ref": dn.supplier_ref, "amount": _money(dn.amount),
            "paid": _money(dn.paid), "balance": _money(dn.balance),
        },
    }


def _issue_for_po_payment(payment):
    po = payment.purchase_order
    dn = payment.demand_note
    acct = supplier.po_account(po)
    snap = _supplier_snapshot(payment, acct, dn)
    purpose = f"Payment on purchase order {po.number}"
    if dn:
        purpose += f" against demand note {dn.supplier_ref or dn.number}"
    if snap["tone"] == "advance":
        purpose += " (advance - goods not yet fully delivered)"
    return Receipt.objects.create(
        project=po.project, date=payment.date, payment=payment, purchase_order=po, demand_note=dn,
        amount=payment.amount, method=payment.method, reference=payment.reference or (dn.supplier_ref if dn else ""),
        purpose=purpose[:300], payee_type=Receipt.PayeeType.SUPPLIER, payee_name=po.supplier.name, supplier=po.supplier,
        snapshot=snap, created_by=payment.created_by, updated_by=payment.created_by,
    )


def issue_for_payment(payment):
    if payment.status != Payment.Status.APPROVED:
        return None  # advances get their receipt when they are approved
    if payment.purchase_order_id:
        return _issue_for_po_payment(payment)
    expense = payment.expense
    if expense.source_type == PAYROLL:
        return None  # wages are receipted worker by worker, see issue_worker_receipts
    payee = dict(payee_type=Receipt.PayeeType.OTHER, payee_name=(expense.description or "Payee not stated")[:200])
    snap = {}
    if expense.source_type == CERTIFICATE:
        from workforce.models import PaymentCertificate
        cert = PaymentCertificate.objects.select_related("contract__contractor").filter(pk=expense.source_id).first()
        if cert:
            contractor = cert.contract.contractor
            payee = dict(payee_type=Receipt.PayeeType.CONTRACTOR, payee_name=contractor.name, contractor=contractor)
            c = cert.contract
            snap = {
                "kind": "contractor", "contract": c.reference, "certificate": cert.number,
                "contract_amount": _money(c.amount), "certified_gross": _money(c.certified_gross),
                "retention_held": _money(c.retention_held), "advance_recovered": _money(cert.advance_recovered),
                "advance_outstanding": _money(c.advance_outstanding), "certificate_net": _money(expense.amount),
                "paid_this_certificate": _money(expense.paid_amount),
                "balance_this_certificate": _money(expense.amount - expense.paid_amount),
            }
    elif expense.source_type == CONTRACTOR_ADVANCE:
        from workforce.models import ContractorAdvance
        adv = ContractorAdvance.objects.select_related("contract__contractor").filter(pk=expense.source_id).first()
        if adv:
            c = adv.contract
            payee = dict(payee_type=Receipt.PayeeType.CONTRACTOR, payee_name=c.contractor.name, contractor=c.contractor)
            snap = {
                "kind": "contractor_advance", "contract": c.reference, "advance": adv.number,
                "contract_amount": _money(c.amount), "this_advance": _money(adv.amount),
                "paid_on_advance": _money(expense.paid_amount),
                "balance_unpaid": _money(expense.amount - expense.paid_amount),
                "advances_total": _money(c.advances_approved), "recovered": _money(c.advances_recovered),
                "outstanding": _money(c.advance_outstanding),
            }
    elif expense.source_type == WORKER_ADVANCE:
        from workforce.models import WorkerAdvance
        adv = WorkerAdvance.objects.select_related("worker").filter(pk=expense.source_id).first()
        if adv:
            payee = dict(payee_type=Receipt.PayeeType.WORKER, payee_name=adv.worker.full_name, worker=adv.worker)
            snap = {
                "kind": "worker_advance", "advance": adv.number, "this_advance": _money(adv.amount),
                "paid_on_advance": _money(expense.paid_amount), "repaid": _money(adv.repaid),
                "outstanding": _money(adv.outstanding), "reason": adv.reason,
            }
    elif expense.supplier_id:
        payee = dict(payee_type=Receipt.PayeeType.SUPPLIER, payee_name=expense.supplier.name, supplier=expense.supplier)
    return Receipt.objects.create(
        project=expense.project, date=payment.date, payment=payment, amount=payment.amount,
        method=payment.method, reference=payment.reference or expense.reference,
        purpose=expense.description[:300], snapshot=snap, created_by=payment.created_by,
        updated_by=payment.created_by, **payee,
    )


@transaction.atomic
def issue_worker_receipts(payroll, user):
    """One receipt per worker on an approved payroll whose wages have been fully paid."""
    from workforce.models import Payroll

    require(user, "issue_receipts")
    payroll = Payroll.objects.select_for_update().get(pk=payroll.pk)
    if payroll.status != Payroll.Status.APPROVED:
        raise WorkflowError("Only approved payrolls can be receipted.")
    expense = Expense.objects.filter(source_type=PAYROLL, source_id=payroll.pk).first()
    if expense is None or expense.paid_amount < expense.amount:
        raise WorkflowError("Record the full wage payment first (Payments), then issue the worker receipts.")
    last = expense.payments.order_by("-date", "-id").first()
    have = set(Receipt.objects.filter(payroll=payroll, voided=False).values_list("worker_id", flat=True))
    created = 0
    for line in payroll.lines.select_related("worker"):
        if line.worker_id in have or line.net <= 0:
            continue
        advance_left = line.advance.outstanding if line.advance_id else Decimal("0")
        Receipt.objects.create(
            project=payroll.project, date=last.date, payee_type=Receipt.PayeeType.WORKER,
            payee_name=line.worker.full_name, worker=line.worker, payroll=payroll, amount=line.net,
            method=last.method, reference=payroll.number,
            purpose=f"Wages {payroll.period_start:%d/%m/%Y} to {payroll.period_end:%d/%m/%Y} ({line.days_present:g} days)",
            snapshot={"kind": "worker", "days": f"{line.days_present:g}", "regular": _money(line.regular_pay),
                      "overtime": _money(line.overtime_pay), "gross": _money(line.total),
                      "advance_deducted": _money(line.advance_deducted), "advance_balance": _money(advance_left),
                      "total": _money(line.net), "payroll": payroll.number},
            created_by=user, updated_by=user,
        )
        created += 1
    if not created:
        raise WorkflowError("Receipts have already been issued for every worker on this payroll.")
    return payroll


@transaction.atomic
def void_receipt(receipt, user, reason):
    require(user, "void_receipt")
    receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.voided:
        raise WorkflowError("This receipt is already void.")
    if not (reason or "").strip():
        raise WorkflowError("Give a reason for voiding the receipt.")
    receipt.voided, receipt.void_reason = True, reason.strip()[:200]
    receipt.voided_by, receipt.voided_at = user, timezone.now()
    receipt.save(update_fields=["voided", "void_reason", "voided_by", "voided_at", "updated_at"])
    return receipt
"""Contractor certificates, attendance pay and payroll.

Pay rules (change them in settings if your site works differently):
  * STANDARD_WORK_HOURS (default 8) = one full day. Fewer hours are paid pro rata, more than
    a day's hours never increases the regular pay.
  * Overtime hours are paid at daily_wage / STANDARD_WORK_HOURS x OVERTIME_MULTIPLIER (default 1.5).
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from finance.models import Category, Expense
from workforce.models import (
    Attendance, Contract, PaymentCertificate, Payroll, PayrollLine, money,
)

ZERO = Decimal("0")
ONE = Decimal("1")
PRESENT = Attendance.Status.PRESENT


def _std_hours():
    return Decimal(str(getattr(settings, "STANDARD_WORK_HOURS", 8)))


def _ot_multiplier():
    return Decimal(str(getattr(settings, "OVERTIME_MULTIPLIER", "1.5")))


def day_fraction(att):
    if att.status != PRESENT:
        return ZERO
    return min(att.hours_worked / _std_hours(), ONE)


def pay_for(att, daily_wage):
    """Returns (regular_pay, overtime_pay) for one attendance record."""
    if att.status != PRESENT:
        return ZERO, ZERO
    regular = money(daily_wage * day_fraction(att))
    overtime = money(att.overtime_hours * (daily_wage / _std_hours()) * _ot_multiplier())
    return regular, overtime


# ------------------------------------------------------------ certificates
@transaction.atomic
def approve_certificate(cert, user):
    require(user, "approve_certificate")
    cert = PaymentCertificate.objects.select_for_update().get(pk=cert.pk)
    if cert.status != PaymentCertificate.Status.DRAFT:
        raise WorkflowError("Only draft certificates can be approved.")
    if cert.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a certificate you raised.")
    contract = Contract.objects.select_for_update().get(pk=cert.contract_id)
    if contract.status != Contract.Status.ACTIVE:
        raise WorkflowError("The contract is not active.")
    already = contract.certified_gross
    if already + cert.gross_amount > contract.amount:
        raise WorkflowError(
            f"Certified total would be UGX {already + cert.gross_amount:,.0f}, above the contract amount of "
            f"UGX {contract.amount:,.0f}. Amend the contract first."
        )
    retention = money(cert.gross_amount * contract.retention_percent / 100)
    net = cert.gross_amount - retention
    Expense.objects.create(
        project=contract.project, phase=contract.phase, date=cert.date, category=Category.SUBCONTRACTOR,
        description=f"{contract.contractor.name}: {cert.description} ({contract.reference})",
        amount=net, reference=cert.number, status=Expense.Status.APPROVED,
        approved_by=user, approved_at=timezone.now(), created_by=user,
        source_type=cert._meta.label_lower, source_id=cert.pk,
    )
    cert.status = PaymentCertificate.Status.APPROVED
    cert.retention_amount = retention
    cert.net_amount = net
    cert.approved_by = user
    cert.approved_at = timezone.now()
    cert.save(update_fields=["status", "retention_amount", "net_amount", "approved_by", "approved_at", "updated_at"])
    return cert


@transaction.atomic
def reject_certificate(cert, user):
    require(user, "approve_certificate")
    cert = PaymentCertificate.objects.select_for_update().get(pk=cert.pk)
    if cert.status != PaymentCertificate.Status.DRAFT:
        raise WorkflowError("Only draft certificates can be rejected.")
    cert.status = PaymentCertificate.Status.REJECTED
    cert.save(update_fields=["status", "updated_at"])
    return cert


# ------------------------------------------------------------ payroll
@transaction.atomic
def fill_payroll(payroll, user):
    """Build the payroll lines from unpaid attendance and mark those days as included."""
    require(user, "build_payroll")
    payroll = Payroll.objects.select_for_update().get(pk=payroll.pk)
    if payroll.status != Payroll.Status.DRAFT:
        raise WorkflowError("Only draft payrolls can be built.")
    if payroll.lines.exists():
        raise WorkflowError("This payroll has already been built.")
    records = list(payroll.eligible_attendance().select_related("worker"))
    if not records:
        raise WorkflowError("There is no unpaid attendance for direct-hire workers in this period.")

    per_worker = {}
    for att in records:
        reg, ot = pay_for(att, att.worker.daily_wage)
        row = per_worker.setdefault(att.worker_id, {"worker": att.worker, "days": ZERO, "reg": ZERO, "ot": ZERO})
        row["days"] += day_fraction(att)
        row["reg"] += reg
        row["ot"] += ot
    for row in per_worker.values():
        PayrollLine.objects.create(
            payroll=payroll, worker=row["worker"], days_present=row["days"],
            regular_pay=row["reg"], overtime_pay=row["ot"],
        )
    Attendance.objects.filter(pk__in=[a.pk for a in records]).update(payroll=payroll)
    return payroll


@transaction.atomic
def approve_payroll(payroll, user):
    require(user, "approve_payroll")
    payroll = Payroll.objects.select_for_update().get(pk=payroll.pk)
    if payroll.status != Payroll.Status.DRAFT:
        raise WorkflowError("Only draft payrolls can be approved.")
    if payroll.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve a payroll you prepared.")
    total = payroll.total
    if total <= 0:
        raise WorkflowError("The payroll is empty. Build it first.")
    Expense.objects.create(
        project=payroll.project, date=payroll.period_end, category=Category.LABOUR,
        description=f"Wages {payroll.period_start:%d/%m/%Y} to {payroll.period_end:%d/%m/%Y} "
                    f"({payroll.lines.count()} workers)",
        amount=total, reference=payroll.number, status=Expense.Status.APPROVED,
        approved_by=user, approved_at=timezone.now(), created_by=user,
        source_type=payroll._meta.label_lower, source_id=payroll.pk,
    )
    payroll.status = Payroll.Status.APPROVED
    payroll.approved_by = user
    payroll.approved_at = timezone.now()
    payroll.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return payroll


# ------------------------------------------------------------ report
def labour_summary(project, today=None):
    today = today or timezone.localdate()
    month_start = today.replace(day=1)
    since = today - timedelta(days=13)

    counts = dict(
        Attendance.objects.filter(worker__project=project, status=PRESENT, date__gte=since, date__lte=today)
        .order_by().values_list("date").annotate(n=Count("id"))
    )
    headcount = [{"date": since + timedelta(days=i), "count": counts.get(since + timedelta(days=i), 0)}
                 for i in range(14)]

    month_records = Attendance.objects.filter(
        worker__project=project, worker__contractor__isnull=True, status=PRESENT,
        date__gte=month_start, date__lte=today,
    ).select_related("worker", "phase")
    by_trade, by_phase = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
    month_total = ZERO
    for att in month_records:
        reg, ot = pay_for(att, att.worker.daily_wage)
        cost = reg + ot
        by_trade[att.worker.get_trade_display()] += cost
        by_phase[att.phase.name if att.phase else "Not assigned to a component"] += cost
        month_total += cost

    contractor_present_today = Attendance.objects.filter(
        worker__project=project, worker__contractor__isnull=False, status=PRESENT, date=today).count()

    contracts = list(Contract.objects.filter(project=project).select_related("contractor"))
    cert_map = {pk: cid for pk, cid in PaymentCertificate.objects.filter(
        contract__project=project, status=PaymentCertificate.Status.APPROVED).values_list("pk", "contract_id")}
    dec = DecimalField(max_digits=16, decimal_places=2)
    paid_by_contract = defaultdict(lambda: ZERO)
    for exp in Expense.objects.filter(source_type="workforce.paymentcertificate", source_id__in=list(cert_map)).annotate(
        paid_total=Coalesce(Sum("payments__amount"), Value(ZERO, output_field=dec), output_field=dec)
    ):
        paid_by_contract[cert_map[exp.source_id]] += exp.paid_total

    contract_rows = []
    for c in contracts:
        certified, retention = c.certified_gross, c.retention_held
        net_due = certified - retention
        contract_rows.append({
            "contract": c, "certified": certified, "retention": retention, "balance": c.amount - certified,
            "percent": (certified * 100 / c.amount) if c.amount else ZERO,
            "paid": paid_by_contract.get(c.pk, ZERO), "unpaid": net_due - paid_by_contract.get(c.pk, ZERO),
        })

    return {
        "headcount": headcount, "month_total": month_total,
        "by_trade": sorted(by_trade.items(), key=lambda kv: -kv[1]),
        "by_phase": sorted(by_phase.items(), key=lambda kv: -kv[1]),
        "contractor_present_today": contractor_present_today, "contract_rows": contract_rows,
    }

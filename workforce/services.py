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
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.permissions import WorkflowError, require
from finance.models import Category, Expense
from workforce.models import (
    Attendance, Contract, ContractorAdvance, PaymentCertificate, Payroll, PayrollLine, WorkerAdvance, money,
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
    # Mobilisation advance is recovered in proportion to the work certified, so it is fully
    # recovered when the whole contract sum has been certified.
    recovery = ZERO
    outstanding = contract.advance_outstanding
    if outstanding > 0 and contract.amount > 0:
        recovery = money(cert.gross_amount * contract.advances_approved / contract.amount)
        recovery = max(min(recovery, outstanding, cert.gross_amount - retention), ZERO)
    net = cert.gross_amount - retention - recovery
    desc = f"{contract.contractor.name}: {cert.description} ({contract.reference})"
    if recovery:
        desc += f" [less advance recovered {recovery:,.0f}]"
    Expense.objects.create(
        project=contract.project, phase=contract.phase, date=cert.date, category=Category.SUBCONTRACTOR,
        description=desc[:300],
        amount=net, reference=cert.number, status=Expense.Status.APPROVED,
        approved_by=user, approved_at=timezone.now(), created_by=user,
        source_type=cert._meta.label_lower, source_id=cert.pk,
    )
    cert.status = PaymentCertificate.Status.APPROVED
    cert.retention_amount = retention
    cert.advance_recovered = recovery
    cert.net_amount = net
    cert.approved_by = user
    cert.approved_at = timezone.now()
    cert.save(update_fields=["status", "retention_amount", "advance_recovered", "net_amount", "approved_by", "approved_at",
                                "updated_at"])
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
    cap = Decimal(str(getattr(settings, "WORKER_ADVANCE_DEDUCTION_PERCENT", 50))) / 100
    for row in per_worker.values():
        gross = row["reg"] + row["ot"]
        advance, deduction = None, ZERO
        for adv in WorkerAdvance.objects.select_for_update().filter(
                worker=row["worker"], status=WorkerAdvance.Status.APPROVED).order_by("approved_at", "id"):
            if adv.outstanding > 0:
                advance, deduction = adv, min(adv.outstanding, money(gross * cap))
                break
        PayrollLine.objects.create(
            payroll=payroll, worker=row["worker"], days_present=row["days"],
            regular_pay=row["reg"], overtime_pay=row["ot"],
            advance=advance if deduction > 0 else None, advance_deducted=deduction,
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
    deductions = payroll.deductions
    note = f", less salary advances recovered {deductions:,.0f}" if deductions else ""
    Expense.objects.create(
        project=payroll.project, date=payroll.period_end, category=Category.LABOUR,
        description=f"Wages {payroll.period_start:%d/%m/%Y} to {payroll.period_end:%d/%m/%Y} "
                    f"({payroll.lines.count()} workers{note})",
        amount=total - deductions, reference=payroll.number, status=Expense.Status.APPROVED,
        approved_by=user, approved_at=timezone.now(), created_by=user,
        source_type=payroll._meta.label_lower, source_id=payroll.pk,
    )
    payroll.status = Payroll.Status.APPROVED
    payroll.approved_by = user
    payroll.approved_at = timezone.now()
    payroll.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return payroll


# ------------------------------------------------------------ advances
@transaction.atomic
def approve_contractor_advance(adv, user):
    require(user, "approve_mobilisation")
    adv = ContractorAdvance.objects.select_for_update().get(pk=adv.pk)
    if adv.status != ContractorAdvance.Status.DRAFT:
        raise WorkflowError("Only draft advances can be approved.")
    if adv.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve an advance you raised.")
    contract = Contract.objects.select_for_update().get(pk=adv.contract_id)
    if contract.status != Contract.Status.ACTIVE:
        raise WorkflowError("The contract is not active.")
    try:
        adv.clean()
    except DjangoValidationError as exc:
        raise WorkflowError("; ".join(exc.messages))
    Expense.objects.create(
        project=contract.project, phase=contract.phase, date=adv.date, category=Category.SUBCONTRACTOR,
        description=f"{contract.contractor.name}: {adv.purpose} ({contract.reference})"[:300],
        amount=adv.amount, reference=adv.number, status=Expense.Status.APPROVED,
        approved_by=user, approved_at=timezone.now(), created_by=user,
        source_type=adv._meta.label_lower, source_id=adv.pk,
    )
    adv.status = ContractorAdvance.Status.APPROVED
    adv.approved_by, adv.approved_at = user, timezone.now()
    adv.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return adv


@transaction.atomic
def reject_contractor_advance(adv, user):
    require(user, "approve_mobilisation")
    adv = ContractorAdvance.objects.select_for_update().get(pk=adv.pk)
    if adv.status != ContractorAdvance.Status.DRAFT:
        raise WorkflowError("Only draft advances can be rejected.")
    adv.status = ContractorAdvance.Status.REJECTED
    adv.save(update_fields=["status", "updated_at"])
    return adv


@transaction.atomic
def approve_worker_advance(adv, user):
    require(user, "approve_worker_advance")
    adv = WorkerAdvance.objects.select_for_update().get(pk=adv.pk)
    if adv.status != WorkerAdvance.Status.DRAFT:
        raise WorkflowError("Only draft advances can be approved.")
    if adv.created_by_id == user.pk and not user.is_superuser:
        raise WorkflowError("Segregation of duties: you cannot approve an advance you raised.")
    try:
        adv.clean()
    except DjangoValidationError as exc:
        raise WorkflowError("; ".join(exc.messages))
    Expense.objects.create(
        project=adv.worker.project, date=adv.date, category=Category.LABOUR,
        description=f"Salary advance: {adv.worker.full_name}"[:300],
        amount=adv.amount, reference=adv.number, status=Expense.Status.APPROVED,
        approved_by=user, approved_at=timezone.now(), created_by=user,
        source_type=adv._meta.label_lower, source_id=adv.pk,
    )
    adv.status = WorkerAdvance.Status.APPROVED
    adv.approved_by, adv.approved_at = user, timezone.now()
    adv.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return adv


@transaction.atomic
def reject_worker_advance(adv, user):
    require(user, "approve_worker_advance")
    adv = WorkerAdvance.objects.select_for_update().get(pk=adv.pk)
    if adv.status != WorkerAdvance.Status.DRAFT:
        raise WorkflowError("Only draft advances can be rejected.")
    adv.status = WorkerAdvance.Status.REJECTED
    adv.save(update_fields=["status", "updated_at"])
    return adv


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
            "advance": c.advances_approved, "advance_recovered": c.advances_recovered,
            "advance_outstanding": c.advance_outstanding,
            "paid": paid_by_contract.get(c.pk, ZERO),
            "unpaid": net_due - c.advances_recovered - paid_by_contract.get(c.pk, ZERO),
        })

    return {
        "headcount": headcount, "month_total": month_total,
        "by_trade": sorted(by_trade.items(), key=lambda kv: -kv[1]),
        "by_phase": sorted(by_phase.items(), key=lambda kv: -kv[1]),
        "contractor_present_today": contractor_present_today, "contract_rows": contract_rows,
    }


# ------------------------------------------------------------ daily attendance (web form and mobile API)
class AttendanceBatchError(WorkflowError):
    """One or more rows of a day's attendance were rejected; nothing was saved."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors))


def record_attendance_day(project, day, entries, user):
    """Record or correct a whole day's attendance, all or nothing.

    entries: dicts with worker, status, hours_worked, overtime_hours, phase (optional), notes (optional).
    Days already included in a payroll cannot be changed. Returns the number of rows saved.
    """
    require(user, "record_attendance")
    errors, saved = [], 0
    with transaction.atomic():
        for e in entries:
            worker = e["worker"]
            if worker.project_id != project.pk:
                errors.append(f"{worker.full_name}: not a worker of this project.")
                continue
            obj = Attendance.objects.filter(worker=worker, date=day).first()
            if obj is None:
                obj = Attendance(worker=worker, date=day, created_by=user)
            elif obj.payroll_id:
                errors.append(f"{worker.full_name}: already included in a payroll and cannot be changed.")
                continue
            obj.status, obj.hours_worked, obj.overtime_hours = e["status"], e["hours_worked"], e["overtime_hours"]
            obj.phase, obj.notes, obj.updated_by = e.get("phase"), e.get("notes", ""), user
            try:
                obj.full_clean()
            except DjangoValidationError as exc:
                errors.append(f"{worker.full_name}: {'; '.join(exc.messages)}")
                continue
            obj.save()
            saved += 1
        if errors:  # roll the whole day back
            raise AttendanceBatchError(errors)
    return saved

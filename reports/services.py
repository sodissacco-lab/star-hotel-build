"""Numbers behind the reports. Everything here only reads; nothing is changed."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

from finance.models import Budget, Category, Expense
from inventory.models import GoodsReceipt, StockLedger, WastageRecord
from procurement.models import DirectDelivery, Requisition
from projects.models import SiteDiary
from workforce import services as labour
from workforce.models import Attendance, ContractorAdvance, PaymentCertificate, Payroll, WorkerAdvance

ZERO = Decimal("0")
T = StockLedger.Type
MAX_DAYS = 366


def parse_period(params, default_days=7, today=None):
    """(start, end) from ?from=&to= (YYYY-MM-DD); defaults to the last `default_days` days up to today."""
    today = today or timezone.localdate()

    def read(key):
        try:
            return date.fromisoformat(params.get(key, ""))
        except ValueError:
            return None

    end = read("to") or today
    start = read("from") or end - timedelta(days=default_days - 1)
    if start > end:
        start, end = end, start
    if (end - start).days > MAX_DAYS:
        start = end - timedelta(days=MAX_DAYS)
    return start, end


def presets(today=None):
    """Ready-made periods for the quick links."""
    today = today or timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    return [
        ("This week", monday, monday + timedelta(days=6)),
        ("Last week", monday - timedelta(days=7), monday - timedelta(days=1)),
        ("This month", first, today),
        ("Last month", last_month_end.replace(day=1), last_month_end),
    ]


def _value():
    return ExpressionWrapper(F("quantity") * F("unit_cost"), output_field=DecimalField(max_digits=24, decimal_places=4))


# ------------------------------------------------------------------ weekly progress
def weekly_progress(project, start, end):
    diaries = list(SiteDiary.objects.filter(project=project, date__range=(start, end))
                   .select_related("supervisor").prefetch_related("activities__phase").order_by("date"))
    present_by_day = dict(Attendance.objects.filter(worker__project=project, status=Attendance.Status.PRESENT,
                                                    date__range=(start, end)).order_by().values_list("date").annotate(n=Count("id")))
    diary_by_day = {d.date: d for d in diaries}
    days = []
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i)
        diary = diary_by_day.get(day)
        days.append({"date": day, "diary": diary, "weather": diary.get_weather_display() if diary else "",
                     "diary_workers": diary.workers_present if diary else None,
                     "attendance": present_by_day.get(day, 0)})

    activities = [{"date": d.date, "phase": a.phase.name if a.phase else "", "location": a.location,
                   "description": a.description, "completed": a.work_completed}
                  for d in diaries for a in d.activities.all()]
    problems = [{"date": d.date, "challenges": d.challenges, "safety": d.safety_incidents, "instructions": d.instructions_issued}
                for d in diaries if d.challenges or d.safety_incidents or d.instructions_issued]

    att = Attendance.objects.filter(worker__project=project, status=Attendance.Status.PRESENT, date__range=(start, end))
    attendance = {"worker_days": att.count(), "workers": att.values("worker").distinct().count(),
                  "overtime_hours": att.aggregate(t=Sum("overtime_hours"))["t"] or ZERO}

    ledger = StockLedger.objects.filter(project=project, entry_date__range=(start, end))
    def moved(kind):
        return ledger.filter(entry_type=kind).aggregate(v=Sum(_value()))["v"] or ZERO

    stock = {"received": moved(T.RECEIPT), "issued": -moved(T.ISSUE), "wasted": -moved(T.WASTAGE)}
    stock["deliveries"] = GoodsReceipt.objects.filter(po__project=project, posted=True, receipt_date__range=(start, end)).count()

    spent = Expense.objects.filter(project=project, status=Expense.Status.APPROVED, date__range=(start, end))
    labels = dict(Category.choices)
    spend_rows = [{"label": labels.get(r["category"], r["category"]), "total": r["t"]}
                  for r in spent.order_by().values("category").annotate(t=Sum("amount")).order_by("-t")]

    phases = list(project.phases.all())
    return {
        "days": days, "activities": activities, "problems": problems, "attendance": attendance, "stock": stock,
        "spend_rows": spend_rows, "spend_total": sum((r["total"] for r in spend_rows), ZERO),
        "phases": phases, "progress": project.overall_progress(),
        "requisitions": Requisition.objects.filter(project=project, created_at__date__range=(start, end)).count(),
        "direct_pending": DirectDelivery.objects.filter(project=project, status=DirectDelivery.Status.PENDING).count(),
        "diaries_missing": [d["date"] for d in days if d["diary"] is None],
    }


# ------------------------------------------------------------------ materials movement
def materials_movement(project, start, end):
    val = _value()
    rows = (StockLedger.objects.filter(project=project, entry_date__lte=end)
            .order_by().values("material_id")
            .annotate(
                opening=Sum("quantity", filter=Q(entry_date__lt=start)),
                received=Sum("quantity", filter=Q(entry_type=T.RECEIPT, entry_date__gte=start)),
                received_value=Sum(val, filter=Q(entry_type=T.RECEIPT, entry_date__gte=start)),
                issued=Sum("quantity", filter=Q(entry_type=T.ISSUE, entry_date__gte=start)),
                issued_value=Sum(val, filter=Q(entry_type=T.ISSUE, entry_date__gte=start)),
                returned=Sum("quantity", filter=Q(entry_type=T.RETURN, entry_date__gte=start)),
                adjusted=Sum("quantity", filter=Q(entry_type=T.ADJUSTMENT, entry_date__gte=start)),
                wasted=Sum("quantity", filter=Q(entry_type=T.WASTAGE, entry_date__gte=start)),
                wasted_value=Sum(val, filter=Q(entry_type=T.WASTAGE, entry_date__gte=start)),
                closing=Sum("quantity")))
    from inventory.models import Material
    materials = {m.pk: m for m in Material.objects.select_related("unit", "category")
                 .filter(pk__in=[r["material_id"] for r in rows])}
    out, totals = [], defaultdict(lambda: ZERO)
    for r in rows:
        g = lambda k: r[k] or ZERO
        m = materials[r["material_id"]]
        line = {"material": m, "opening": g("opening"), "received": g("received"), "issued": -g("issued"),
                "returned": g("returned"), "adjusted": g("adjusted"), "wasted": -g("wasted"), "closing": g("closing"),
                "received_value": g("received_value"), "issued_value": -g("issued_value"), "wasted_value": -g("wasted_value")}
        if not any(line[k] for k in ("received", "issued", "returned", "adjusted", "wasted")) and not line["opening"]:
            continue
        out.append(line)
        for k in ("received_value", "issued_value", "wasted_value"):
            totals[k] += line[k]
    out.sort(key=lambda x: (x["material"].category.name, x["material"].name))
    wastage = list(WastageRecord.objects.filter(project=project, posted=True, date__range=(start, end))
                   .select_related("material__unit").order_by("date", "id"))
    return {"rows": out, "totals": dict(totals), "wastage": wastage}


# ------------------------------------------------------------------ budget vs actual
def budget_vs_actual(project):
    approved = Expense.objects.filter(project=project, status=Expense.Status.APPROVED)
    budget_by_cat = dict(Budget.objects.filter(project=project).order_by().values_list("category").annotate(t=Sum("amount")))
    spent_by_cat = dict(approved.order_by().values_list("category").annotate(t=Sum("amount")))
    labels = dict(Category.choices)

    def line(label, budget, spent):
        return {"label": label, "budget": budget, "spent": spent, "variance": budget - spent,
                "pct": (spent * 100 / budget) if budget else None,
                "status": "Not budgeted" if not budget and spent else ("Over budget" if spent > budget else "")}

    categories = [line(labels[c], budget_by_cat.get(c, ZERO), spent_by_cat.get(c, ZERO))
                  for c, _ in Category.choices if c in budget_by_cat or c in spent_by_cat]
    spent_by_phase = dict(approved.order_by().values_list("phase_id").annotate(t=Sum("amount")))
    phases = [line(ph.name, ph.budget, spent_by_phase.get(ph.pk, ZERO)) for ph in project.phases.all()
              if ph.budget or spent_by_phase.get(ph.pk)]
    unassigned = spent_by_phase.get(None, ZERO)
    if unassigned:
        phases.append(line("Not assigned to a component", ZERO, unassigned))
    total_budget = sum((c["budget"] for c in categories), ZERO)
    total_spent = sum((c["spent"] for c in categories), ZERO)
    return {"categories": categories, "phases": phases, "total": line("Total", total_budget, total_spent),
            "approved_budget": project.approved_budget, "unbudgeted_total": project.approved_budget - total_budget}


# ------------------------------------------------------------------ payroll and contractors
def payroll_contractors(project, start, end):
    payrolls = []
    for p in (Payroll.objects.filter(project=project, status=Payroll.Status.APPROVED, period_end__gte=start, period_start__lte=end)
              .prefetch_related("lines").order_by("period_start")):
        payrolls.append({"payroll": p, "workers": p.lines.count(), "gross": p.total, "deductions": p.deductions, "net": p.net_total})
    advances = list(WorkerAdvance.objects.filter(worker__project=project, status="approved", date__range=(start, end))
                    .select_related("worker").order_by("date"))
    certificates = list(PaymentCertificate.objects.filter(contract__project=project, status="approved", date__range=(start, end))
                        .select_related("contract__contractor").order_by("date"))
    mobilisations = list(ContractorAdvance.objects.filter(contract__project=project, status="approved", date__range=(start, end))
                         .select_related("contract__contractor").order_by("date"))
    contracts = labour.labour_summary(project)["contract_rows"]
    return {
        "payrolls": payrolls, "advances": advances, "certificates": certificates, "mobilisations": mobilisations,
        "contracts": contracts,
        "totals": {
            "gross": sum((r["gross"] for r in payrolls), ZERO), "net": sum((r["net"] for r in payrolls), ZERO),
            "worker_advances": sum((a.amount for a in advances), ZERO),
            "certified": sum((c.gross_amount for c in certificates), ZERO),
            "certificate_net": sum((c.net_amount or ZERO for c in certificates), ZERO),
            "mobilisation": sum((m.amount for m in mobilisations), ZERO),
        },
    }

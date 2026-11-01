from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from accounts.permissions import can
from core.views import pick_project
from finance import supplier as supplier_mod
from finance.models import Budget, Category, Expense, Receipt

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@login_required
def budget_report(request):
    """Budget vs actual by category and by phase, plus what is owed to suppliers."""
    if not can(request.user, "view_finance"):
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    ctx = {"projects": projects, "project": project}
    if project is None:
        return render(request, "budget_report.html", ctx)

    approved = Expense.objects.filter(project=project, status=Expense.Status.APPROVED)
    budget_by_cat = {c: t for c, t in
                     Budget.objects.filter(project=project).order_by().values_list("category").annotate(t=Sum("amount"))}
    spent_by_cat = {c: t for c, t in approved.order_by().values_list("category").annotate(t=Sum("amount"))}

    labels = dict(Category.choices)
    rows, tb, ts = [], ZERO, ZERO
    for cat in [c for c, _ in Category.choices if c in budget_by_cat or c in spent_by_cat]:
        b, s = budget_by_cat.get(cat, ZERO), spent_by_cat.get(cat, ZERO)
        pct = (s * HUNDRED / b) if b else None
        if b == 0 and s > 0:
            flag = "Unplanned (no budget)"
        elif s > b:
            flag = "Exceeded"
        elif pct is not None and pct >= 90:
            flag = "Approaching limit"
        else:
            flag = ""
        rows.append({"label": labels[cat], "budget": b, "spent": s, "balance": b - s, "pct": pct, "flag": flag})
        tb += b
        ts += s

    spent_by_phase = {p: t for p, t in approved.order_by().values_list("phase_id").annotate(t=Sum("amount"))}
    phase_rows = []
    for ph in project.phases.all():
        s = spent_by_phase.get(ph.pk, ZERO)
        if ph.budget or s:
            phase_rows.append({"phase": ph, "budget": ph.budget, "spent": s, "balance": ph.budget - s,
                               "flag": "Exceeded" if s > ph.budget else ""})
    unassigned = spent_by_phase.get(None, ZERO)

    owed = {}
    supplier_expenses = list(approved.filter(supplier__isnull=False).select_related("supplier"))
    paid_map = supplier_mod.paid_by_expense([e.pk for e in supplier_expenses])
    for e in supplier_expenses:
        out = e.amount - paid_map[e.pk]
        if out > 0:
            owed[e.supplier.name] = owed.get(e.supplier.name, ZERO) + out
    payables = sorted(owed.items(), key=lambda kv: -kv[1])
    advances = supplier_mod.advances_overview(project)

    ctx.update(rows=rows, total_budget=tb, total_spent=ts, total_balance=tb - ts, phase_rows=phase_rows,
               unassigned=unassigned, payables=payables, advances=advances,
               total_advances=sum((a["advance"] for a in advances), ZERO), total_payable=sum(owed.values(), ZERO),
               project_budget=project.approved_budget)
    return render(request, "budget_report.html", ctx)


# ---------------------------------------------------------------- receipts
def _visible_receipts(request):
    if not can(request.user, "view_finance"):
        raise PermissionDenied
    return Receipt.objects.filter(project__in=request.user.accessible_projects()).select_related("project", "created_by")


@login_required
def receipt_list(request):
    """Receipt register with search, payee type and date filters."""
    qs = _visible_receipts(request)
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    qs = qs.filter(project=project) if project else qs.none()
    q = request.GET.get("q", "").strip()
    ptype = request.GET.get("type", "")
    start, end = request.GET.get("from", ""), request.GET.get("to", "")
    if q:
        qs = qs.filter(Q(payee_name__icontains=q) | Q(number__icontains=q) | Q(reference__icontains=q))
    if ptype in Receipt.PayeeType.values:
        qs = qs.filter(payee_type=ptype)
    try:
        if start:
            qs = qs.filter(date__gte=start)
        if end:
            qs = qs.filter(date__lte=end)
    except ValidationError:
        qs = qs.none()
    total = qs.filter(voided=False).aggregate(t=Sum("amount"))["t"] or ZERO
    return render(request, "receipt_list.html", {
        "projects": projects, "project": project, "receipts": qs[:300], "total": total,
        "q": q, "ptype": ptype, "start": start, "end": end, "types": Receipt.PayeeType.choices,
    })


@login_required
def receipt_print(request, pk):
    receipt = get_object_or_404(_visible_receipts(request), pk=pk)
    return render(request, "receipt_print.html", {"receipts": [receipt]})


@login_required
def receipt_batch(request):
    """Print several receipts at once: ?payroll=<id> (all worker receipts of a payroll) or ?ids=1,2,3."""
    qs = _visible_receipts(request)
    if request.GET.get("payroll", "").isdigit():
        qs = qs.filter(payroll_id=int(request.GET["payroll"]))
    else:
        ids = [int(i) for i in request.GET.get("ids", "").split(",") if i.strip().isdigit()][:100]
        qs = qs.filter(pk__in=ids)
    receipts = list(qs.order_by("id"))
    if not receipts:
        raise Http404("No receipts to print.")
    return render(request, "receipt_print.html", {"receipts": receipts})


# ---------------------------------------------------------------- supplier accounts
@login_required
def supplier_list(request):
    """Every supplier with what was ordered, delivered, paid, advanced and still owed."""
    if not can(request.user, "view_finance"):
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    rows = supplier_mod.supplier_summary(project) if project else []
    totals = {k: sum((r[k] for r in rows), ZERO) for k in ("ordered", "delivered", "paid", "advance", "balance_due")}
    advances = supplier_mod.advances_overview(project) if project else []
    return render(request, "supplier_list.html", {"projects": projects, "project": project, "rows": rows,
                                                  "totals": totals, "advances": advances})


@login_required
def supplier_statement(request, pk):
    """Statement for one supplier: orders, register of items supplied, payments and demand notes."""
    from inventory.models import Supplier

    if not can(request.user, "view_finance"):
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    if project is None:
        raise Http404("No project.")
    supplier = get_object_or_404(Supplier, pk=pk)
    data = supplier_mod.statement(project, supplier)
    return render(request, "supplier_statement.html", {"project": project, "projects": projects,
                                                       "supplier": supplier, "s": data})
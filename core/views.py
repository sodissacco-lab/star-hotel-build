from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import F, Sum
from django.shortcuts import render

from accounts.permissions import can
from finance import supplier
from finance.models import Category, Expense, Payment, PaymentAllocation
from inventory import services
from django.utils import timezone

from inventory.models import GoodsReceipt, MaterialIssue
from notifications import services as alerts
from procurement.models import DirectDelivery
from projects.models import SiteDiary
from workforce.models import Attendance

ZERO = Decimal("0")


def pick_project(request, projects):
    """The project chosen with ?project=<id>, else the first one the user can see."""
    pid = request.GET.get("project")
    if pid and pid.isdigit():
        chosen = projects.filter(pk=int(pid)).first()
        if chosen:
            return chosen
    return projects.first()


@login_required
def dashboard(request):
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    ctx = {"projects": projects, "project": project}
    if project is None:
        return render(request, "dashboard.html", ctx)

    show_finance = can(request.user, "view_finance")
    rows, totals = services.stock_summary(project)
    ctx.update(show_finance=show_finance, stock_value=totals.get("stock_value", ZERO),
               purchased_value=totals.get("purchased_value", ZERO),
               consumed_value=totals.get("consumed_value", ZERO),
               wastage_value=totals.get("wastage_value", ZERO),
               low_stock=[r for r in rows if r["below_reorder"]][:10])

    if show_finance:
        approved = Expense.objects.filter(project=project, status=Expense.Status.APPROVED)
        spent = approved.aggregate(t=Sum("amount"))["t"] or ZERO
        paid = (Payment.objects.filter(expense__project=project, expense__status=Expense.Status.APPROVED,
                                       status=Payment.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or ZERO) \
            + (PaymentAllocation.objects.filter(expense__project=project, expense__status=Expense.Status.APPROVED)
               .aggregate(t=Sum("amount"))["t"] or ZERO)
        advances = supplier.advances_overview(project)
        labels = dict(Category.choices)
        by_cat = [
            {"label": labels.get(r["category"], r["category"]), "total": r["total"]}
            for r in approved.values("category").annotate(total=Sum("amount")).order_by("-total")
        ]
        ctx.update(
            budget=project.approved_budget, spent=spent, available=project.approved_budget - spent,
            outstanding=spent - paid, by_category=by_cat, advances=advances,
            total_advances=sum((a["advance"] for a in advances), ZERO),
            stale_advances=[a for a in advances if a["stale"]],
            pending_payments=Payment.objects.filter(purchase_order__project=project, status=Payment.Status.PENDING).count(),
            pending_expenses=Expense.objects.filter(project=project, status=Expense.Status.PENDING).count(),
            budget_used_pct=int(min(100, spent * 100 / project.approved_budget)) if project.approved_budget else 0,
        )

    phases = list(project.phases.all())
    ctx.update(
        progress=project.overall_progress(),
        phases=phases,
        delayed=[p for p in phases if p.is_delayed],
        recent_diaries=SiteDiary.objects.filter(project=project).select_related("supervisor")[:5],
    )
    today = timezone.localdate()
    alerts.refresh(request.user)
    ctx.update(
        attention=list(alerts.active(request.user)[:12]),
        today=today,
        today_diary=SiteDiary.objects.filter(project=project, date=today).first(),
        today_present=Attendance.objects.filter(worker__project=project, date=today, status="present").count(),
        today_issues=MaterialIssue.objects.filter(project=project, issue_date=today, posted=True).count(),
        today_receipts=GoodsReceipt.objects.filter(po__project=project, receipt_date=today, posted=True).count(),
        pending_direct=DirectDelivery.objects.filter(project=project, status=DirectDelivery.Status.PENDING).count(),
    )
    return render(request, "dashboard.html", ctx)
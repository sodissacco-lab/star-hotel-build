from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from accounts.models import User
from accounts.permissions import can
from core.views import pick_project
from reports import services
from reports.excel import workbook_response

R = User.Role


def _site_staff(user):
    return user.is_superuser or user.role != R.CONTRACTOR


def _setup(request, allowed, ranged=True, default_days=7):
    if not allowed(request.user):
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    ctx = {"projects": projects, "project": project, "ranged": ranged}
    if ranged:
        start, end = services.parse_period(request.GET, default_days)
        ctx.update(start=start, end=end, presets=services.presets())
    return project, ctx


def _respond(request, template, ctx, title, filename, sheets):
    if ctx["project"] is not None and request.GET.get("format") == "xlsx":
        name = f"{filename}-{ctx['project'].code}" + (f"-{ctx['start']}-to-{ctx['end']}" if ctx.get("ranged") else "")
        return workbook_response(name + ".xlsx", f"{title} - {ctx['project'].name}", sheets(ctx))
    ctx["title"] = title
    return render(request, template, ctx)


@login_required
def index(request):
    projects = request.user.accessible_projects()
    return render(request, "reports/index.html", {
        "projects": projects, "project": pick_project(request, projects),
        "can_finance": can(request.user, "view_finance"), "can_labour": can(request.user, "view_labour"),
        "site_staff": _site_staff(request.user)})


@login_required
def progress(request):
    project, ctx = _setup(request, _site_staff)
    if project:
        ctx.update(services.weekly_progress(project, ctx["start"], ctx["end"]))

    def sheets(c):
        return [
            ("Days", ["Date", "Weather", "Workers (diary)", "Workers (attendance)", "Diary written"],
             [[d["date"], d["weather"], d["diary_workers"], d["attendance"], "Yes" if d["diary"] else "No"] for d in c["days"]]),
            ("Work done", ["Date", "Component", "Section", "Activity", "Completed"],
             [[a["date"], a["phase"], a["location"], a["description"], a["completed"]] for a in c["activities"]]),
            ("Problems", ["Date", "Challenges", "Safety incidents", "Instructions issued"],
             [[p["date"], p["challenges"], p["safety"], p["instructions"]] for p in c["problems"]]),
            ("Components", ["Component", "Progress %", "Delayed"],
             [[p.name, p.percent_complete, "Yes" if p.is_delayed else ""] for p in c["phases"]]),
            ("Spending", ["Category", "UGX"], [[r["label"], r["total"]] for r in c["spend_rows"]]),
        ]
    return _respond(request, "reports/progress.html", ctx, "Progress report", "progress", sheets)


@login_required
def materials(request):
    project, ctx = _setup(request, _site_staff, default_days=30)
    if project:
        ctx.update(services.materials_movement(project, ctx["start"], ctx["end"]))

    def sheets(c):
        return [
            ("Movement", ["Material", "Unit", "Opening", "Received", "Issued", "Returned", "Adjusted", "Wasted", "Closing",
                          "Received value", "Issued value", "Wastage value"],
             [[r["material"].name, str(r["material"].unit), r["opening"], r["received"], r["issued"], r["returned"],
               r["adjusted"], r["wasted"], r["closing"], r["received_value"], r["issued_value"], r["wasted_value"]]
              for r in c["rows"]]),
            ("Wastage", ["Date", "Material", "Quantity", "Reason", "Location", "Responsible", "Value", "Remarks"],
             [[w.date, w.material.name, w.quantity, w.get_reason_display(), w.location, w.responsible, w.value, w.remarks]
              for w in c["wastage"]]),
        ]
    return _respond(request, "reports/materials.html", ctx, "Materials movement report", "materials", sheets)


@login_required
def budget(request):
    project, ctx = _setup(request, lambda u: can(u, "view_finance"), ranged=False)
    if project:
        ctx.update(services.budget_vs_actual(project))

    def sheets(c):
        head = ["Item", "Budget", "Spent", "Remaining", "% used", "Flag"]
        rows = lambda items: [[i["label"], i["budget"], i["spent"], i["variance"],
                               None if i["pct"] is None else round(i["pct"], 1), i["status"]] for i in items]
        return [("By category", head, rows(c["categories"] + [c["total"]])), ("By component", head, rows(c["phases"]))]
    return _respond(request, "reports/budget.html", ctx, "Budget against actual", "budget", sheets)


@login_required
def payroll(request):
    project, ctx = _setup(request, lambda u: can(u, "view_labour"), default_days=30)
    if project:
        ctx.update(services.payroll_contractors(project, ctx["start"], ctx["end"]))

    def sheets(c):
        return [
            ("Payrolls", ["Number", "From", "To", "Workers", "Gross", "Advance deducted", "Net paid"],
             [[r["payroll"].number, r["payroll"].period_start, r["payroll"].period_end, r["workers"], r["gross"],
               r["deductions"], r["net"]] for r in c["payrolls"]]),
            ("Salary advances", ["Number", "Date", "Worker", "Amount", "Reason"],
             [[a.number, a.date, a.worker.full_name, a.amount, a.reason] for a in c["advances"]]),
            ("Certificates", ["Number", "Date", "Contractor", "Work certified", "Gross", "Retention", "Advance recovered", "Net"],
             [[x.number, x.date, x.contract.contractor.name, x.description, x.gross_amount, x.retention_amount,
               x.advance_recovered, x.net_amount] for x in c["certificates"]]),
            ("Mobilisation advances", ["Number", "Date", "Contractor", "Amount", "Purpose"],
             [[m.number, m.date, m.contract.contractor.name, m.amount, m.purpose] for m in c["mobilisations"]]),
            ("Contracts to date", ["Contractor", "Contract amount", "Certified", "% certified", "Retention held", "Advance paid",
                                   "Advance outstanding", "Paid", "Unpaid"],
             [[r["contract"].contractor.name, r["contract"].amount, r["certified"], r["percent"], r["retention"], r["advance"],
               r["advance_outstanding"], r["paid"], r["unpaid"]] for r in c["contracts"]]),
        ]
    return _respond(request, "reports/payroll.html", ctx, "Payroll and contractor payments", "payroll", sheets)

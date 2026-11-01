from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from core.views import pick_project
from inventory import services


@login_required
def stock_report(request):
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    rows, totals = ([], {})
    if project:
        rows, totals = services.stock_summary(project)
    return render(request, "stock_report.html", {
        "projects": projects, "project": project, "rows": rows, "totals": totals,
    })

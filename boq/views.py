from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from accounts.permissions import can
from boq import services
from core.views import pick_project


@login_required
def boq_report(request):
    if not can(request.user, "view_boq"):
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    rows, phase_rows, totals = ([], [], {})
    if project:
        rows, phase_rows, totals = services.item_report(project)
    return render(request, "boq_report.html", {
        "projects": projects, "project": project, "rows": rows, "phase_rows": phase_rows, "totals": totals,
    })

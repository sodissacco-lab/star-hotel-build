from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from accounts.permissions import can
from core.views import pick_project
from workforce import services


@login_required
def labour_report(request):
    if not can(request.user, "view_labour"):
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    summary = services.labour_summary(project) if project else {}
    return render(request, "labour_report.html", {"projects": projects, "project": project, **summary})

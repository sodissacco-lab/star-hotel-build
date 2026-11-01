import mimetypes

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.models import User
from accounts.permissions import can
from core.views import pick_project
from documents.forms import AttachmentForm
from documents.models import Attachment


def _project(request):
    if request.user.role == User.Role.CONTRACTOR and not request.user.is_superuser:
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    if project is None:
        raise Http404("No project is available to you yet.")
    return project, projects


@login_required
def gallery(request):
    project, projects = _project(request)
    items = project.attachments.select_related("diary", "purchase_order", "created_by")
    kind = request.GET.get("kind", "")
    if kind in Attachment.Kind.values:
        items = items.filter(kind=kind)
    return render(request, "documents/gallery.html", {
        "project": project, "projects": projects, "items": items[:100], "kind": kind,
        "kinds": Attachment.Kind.choices, "can_upload": can(request.user, "attach_files"),
    })


@login_required
def upload(request):
    if not can(request.user, "attach_files"):
        raise PermissionDenied
    project, projects = _project(request)
    form = AttachmentForm(request.POST or None, request.FILES or None, project=project)
    if request.method == "POST" and form.is_valid():
        att = form.save(commit=False)
        att.project = project
        att.original_name = request.FILES["file"].name[:200]
        att.created_by = att.updated_by = request.user
        att.save()
        messages.success(request, "File saved.")
        return redirect(f"{reverse('doc_gallery')}?project={project.pk}")
    return render(request, "documents/upload.html", {"project": project, "projects": projects, "form": form})


@login_required
def serve(request, pk):
    """Files are never public: only members of the project may open them."""
    if request.user.role == User.Role.CONTRACTOR and not request.user.is_superuser:
        raise PermissionDenied
    att = get_object_or_404(Attachment, pk=pk, project__in=request.user.accessible_projects())
    try:
        handle = att.file.open("rb")
    except FileNotFoundError:
        raise Http404("The file is missing from storage.")
    ctype = mimetypes.guess_type(att.file.name)[0] or "application/octet-stream"
    response = FileResponse(handle, content_type=ctype)
    response["Content-Disposition"] = f'inline; filename="{att.original_name or att.file.name.rsplit("/", 1)[-1]}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response

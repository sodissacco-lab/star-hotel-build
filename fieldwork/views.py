from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.permissions import WorkflowError, can
from core.views import pick_project
from fieldwork.forms import ActivityFormSet, DiaryForm, LineFormSet, RequisitionForm
from inventory import services as stock
from inventory.models import (
    GoodsReceipt, GoodsReceiptLine, Material, MaterialIssue, MaterialIssueLine, PurchaseOrder,
)
from procurement import services as req_services
from procurement.models import Requisition
from projects.models import SiteDiary
from workforce import services as wf_services
from workforce.models import Attendance, Worker

ZERO = Decimal("0")

TILES = [
    ("Site diary", "fw_diary", "record_diary", "Today's work, weather, workers, problems"),
    ("Attendance", "fw_attendance", "record_attendance", "Who worked today, hours and overtime"),
    ("Request materials", "fw_requisition", "raise_requisition", "Raise a requisition for the site"),
    ("Issue materials", "fw_issue", "issue_materials", "Take materials out of the store to the site"),
    ("Photos & documents", "doc_gallery", "attach_files", "Upload site photos, delivery notes and receipts"),
    ("Receive goods", "fw_receive_pick", "receive_goods", "Book in a delivery against a purchase order"),
]


def _gate(request, capability):
    if not can(request.user, capability):
        raise PermissionDenied
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    if project is None:
        raise Http404("No project is available to you yet.")
    return project, projects


def _back(project):
    return redirect(f"{reverse('fw_home')}?project={project.pk}")


def _dec(text, default=None):
    text = (text or "").strip().replace(",", "")
    if not text:
        return default
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a number")


def _day(text):
    try:
        return date_cls.fromisoformat(text)
    except (TypeError, ValueError):
        return None


@login_required
def home(request):
    projects = request.user.accessible_projects()
    project = pick_project(request, projects)
    tiles = [t for t in TILES if can(request.user, t[2])]
    return render(request, "fieldwork/home.html", {"projects": projects, "project": project, "tiles": tiles})


# ------------------------------------------------------------------ site diary
@login_required
def diary(request):
    project, projects = _gate(request, "record_diary")
    form = DiaryForm(request.POST or None, project=project)
    formset = ActivityFormSet(request.POST or None, instance=SiteDiary(), prefix="act")
    for f in formset.forms:
        f.fields["phase"].queryset = project.phases.all()
    if request.method == "POST":
        ok = form.is_valid()
        ok = formset.is_valid() and ok
        if ok:
            with transaction.atomic():
                entry = form.save(commit=False)
                entry.project, entry.supervisor = project, request.user
                entry.created_by = entry.updated_by = request.user
                entry.save()
                formset.instance = entry
                formset.save()
            messages.success(request, f"Site diary for {entry.date:%d/%m/%Y} saved.")
            return _back(project)
    return render(request, "fieldwork/diary.html", {"project": project, "projects": projects, "form": form, "formset": formset})


# ------------------------------------------------------------------ attendance
@login_required
def attendance(request):
    project, projects = _gate(request, "record_attendance")
    day = _day(request.POST.get("date") or request.GET.get("date")) or timezone.localdate()
    workers = list(Worker.objects.filter(project=project, is_active=True).select_related("contractor").order_by("full_name"))
    existing = {a.worker_id: a for a in Attendance.objects.filter(worker__in=workers, date=day)}
    phases = list(project.phases.all())
    errors, posted = [], request.method == "POST"
    if posted:
        entries = []
        phase = next((p for p in phases if str(p.pk) == request.POST.get("phase")), None)
        for w in workers:
            st = request.POST.get(f"w{w.pk}_status", "")
            if st not in ("present", "absent"):
                continue
            try:
                hours = _dec(request.POST.get(f"w{w.pk}_hours"), Decimal("8"))
                overtime = _dec(request.POST.get(f"w{w.pk}_ot"), ZERO)
            except ValueError as exc:
                errors.append(f"{w.full_name}: {exc}")
                continue
            entries.append({"worker": w, "status": st, "hours_worked": hours, "overtime_hours": overtime, "phase": phase})
        if not entries and not errors:
            errors.append("Mark at least one worker present or absent.")
        if not errors:
            try:
                saved = wf_services.record_attendance_day(project, day, entries, request.user)
            except wf_services.AttendanceBatchError as exc:
                errors.extend(exc.errors)
            except WorkflowError as exc:
                errors.append(str(exc))
            else:
                messages.success(request, f"Attendance for {day:%d/%m/%Y} saved ({saved} worker(s)).")
                return redirect(f"{reverse('fw_attendance')}?project={project.pk}&date={day.isoformat()}")
    rows = []
    for w in workers:
        att = existing.get(w.pk)
        if posted:
            status, hours, ot = (request.POST.get(f"w{w.pk}_status", ""), request.POST.get(f"w{w.pk}_hours", ""),
                                 request.POST.get(f"w{w.pk}_ot", ""))
        else:
            status = att.status if att else ""
            hours = f"{att.hours_worked:g}" if att else "8"
            ot = f"{att.overtime_hours:g}" if att else "0"
        rows.append({"worker": w, "status": status, "hours": hours, "ot": ot, "locked": bool(att and att.payroll_id)})
    return render(request, "fieldwork/attendance.html", {
        "project": project, "projects": projects, "day": day, "rows": rows, "errors": errors, "phases": phases,
    })


# ------------------------------------------------------------------ requisition
@login_required
def requisition(request):
    project, projects = _gate(request, "raise_requisition")
    form = RequisitionForm(request.POST or None)
    form.fields["phase"].queryset = project.phases.all()
    formset = LineFormSet(request.POST or None, instance=Requisition(), prefix="lines")
    errors = []
    if request.method == "POST":
        ok = form.is_valid()
        ok = formset.is_valid() and ok
        if ok and not any(f.has_changed() for f in formset.forms):
            errors.append("Add at least one material and quantity.")
            ok = False
        if ok:
            try:
                with transaction.atomic():
                    req = form.save(commit=False)
                    req.project = project
                    req.created_by = req.updated_by = request.user
                    req.save()
                    formset.instance = req
                    formset.save()
                    if request.POST.get("action") == "submit":
                        req_services.submit_requisition(req, request.user)
            except WorkflowError as exc:
                errors.append(str(exc))
            else:
                state = "submitted for verification" if request.POST.get("action") == "submit" else "saved as a draft"
                messages.success(request, f"Requisition {req.number} {state}.")
                return _back(project)
    return render(request, "fieldwork/requisition.html", {
        "project": project, "projects": projects, "form": form, "formset": formset, "errors": errors,
    })


# ------------------------------------------------------------------ issue materials
@login_required
def issue(request):
    project, projects = _gate(request, "issue_materials")
    rows_stock, _ = stock.stock_summary(project)
    balance = {r["material"].pk: r["balance"] for r in rows_stock}
    materials = list(Material.objects.filter(is_active=True).select_related("unit").order_by("name"))
    phases = list(project.phases.all())
    errors = []
    posted = request.method == "POST"
    if posted:
        lines = []
        for i in range(8):
            mid, qty = request.POST.get(f"m{i}", ""), request.POST.get(f"q{i}", "")
            if not mid and not qty.strip():
                continue
            material = next((m for m in materials if str(m.pk) == mid), None)
            try:
                quantity = _dec(qty)
            except ValueError as exc:
                errors.append(f"Line {i + 1}: {exc}")
                continue
            if material is None or quantity is None or quantity <= 0:
                errors.append(f"Line {i + 1}: choose a material and a quantity above zero.")
                continue
            lines.append((material, quantity))
        day = _day(request.POST.get("date")) or timezone.localdate()
        if not lines and not errors:
            errors.append("Add at least one material.")
        if not errors:
            phase = next((p for p in phases if str(p.pk) == request.POST.get("phase")), None)
            try:
                with transaction.atomic():
                    iss = MaterialIssue.objects.create(
                        project=project, issue_date=day, phase=phase, activity=request.POST.get("activity", "")[:200],
                        requested_by=request.POST.get("requested_by", "")[:120], notes=request.POST.get("notes", ""),
                        authorised_by=request.user, created_by=request.user, updated_by=request.user)
                    for material, quantity in lines:
                        MaterialIssueLine.objects.create(issue=iss, material=material, quantity_issued=quantity)
                    stock.post_issue(iss, request.user)
            except WorkflowError as exc:
                errors.append(str(exc))
            else:
                messages.success(request, f"Issue ISS-{iss.pk} posted: {len(lines)} item(s) taken from stock.")
                return _back(project)
    options = [{"pk": m.pk, "label": f"{m.name} ({m.unit.abbreviation}) - in stock {balance.get(m.pk, ZERO):g}"} for m in materials]
    slots = [{"i": i, "m": request.POST.get(f"m{i}", "") if posted else "", "q": request.POST.get(f"q{i}", "") if posted else ""}
             for i in range(6)]
    return render(request, "fieldwork/issue.html", {
        "project": project, "projects": projects, "options": options, "phases": phases, "errors": errors,
        "slots": slots, "today": timezone.localdate(), "post": request.POST if posted else {},
    })


# ------------------------------------------------------------------ receive goods
@login_required
def receive_pick(request):
    project, projects = _gate(request, "receive_goods")
    orders = PurchaseOrder.objects.filter(
        project=project, status__in=(PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL)
    ).select_related("supplier").order_by("-order_date")
    return render(request, "fieldwork/receive_pick.html", {"project": project, "projects": projects, "orders": orders})


@login_required
def receive(request, po_id):
    if not can(request.user, "receive_goods"):
        raise PermissionDenied
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "project"), pk=po_id,
        project__in=request.user.accessible_projects(),
        status__in=(PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL))
    project = po.project
    po_lines = list(po.lines.select_related("material__unit"))
    errors, posted = [], request.method == "POST"
    rows = []
    for pl in po_lines:
        got = pl.received_quantity
        rows.append({"line": pl, "received": got, "outstanding": max(pl.quantity - got, ZERO),
                     "d": request.POST.get(f"d{pl.pk}", "") if posted else "",
                     "a": request.POST.get(f"a{pl.pk}", "") if posted else ""})
    if posted:
        entries = []
        for r in rows:
            pl = r["line"]
            try:
                delivered = _dec(r["d"], ZERO)
                accepted = _dec(r["a"], None)
            except ValueError as exc:
                errors.append(f"{pl.material.name}: {exc}")
                continue
            if delivered <= 0:
                continue
            accepted = delivered if accepted is None else accepted
            if accepted < 0 or accepted > delivered:
                errors.append(f"{pl.material.name}: accepted must be between 0 and the quantity delivered.")
                continue
            entries.append((pl, delivered, accepted))
        if not entries and not errors:
            errors.append("Enter the quantity delivered for at least one item.")
        if not errors:
            try:
                with transaction.atomic():
                    grn = GoodsReceipt.objects.create(
                        po=po, receipt_date=_day(request.POST.get("date")) or timezone.localdate(),
                        delivery_note_no=request.POST.get("delivery_note", "")[:60],
                        inspected_by=request.POST.get("inspected_by", "")[:120], notes=request.POST.get("notes", ""),
                        created_by=request.user, updated_by=request.user)
                    for pl, delivered, accepted in entries:
                        GoodsReceiptLine.objects.create(receipt=grn, po_line=pl, quantity_delivered=delivered,
                                                        quantity_accepted=accepted, quantity_rejected=delivered - accepted)
                    stock.post_goods_receipt(grn, request.user)
            except WorkflowError as exc:
                errors.append(str(exc))
            else:
                messages.success(request, f"Delivery against {po.number} booked into stock.")
                return _back(project)
    return render(request, "fieldwork/receive.html", {
        "project": project, "po": po, "rows": rows, "errors": errors, "today": timezone.localdate(),
        "post": request.POST if posted else {},
    })
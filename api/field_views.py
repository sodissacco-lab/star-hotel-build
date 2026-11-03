"""Mobile endpoints for the store, photos and approvals."""
import json
import mimetypes
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import WorkflowError, can
from api.idempotency import idempotent
from api.permissions import capability
from api.views import project_for
from documents.models import Attachment
from inventory import services as stock
from inventory.models import (
    GoodsReceipt, GoodsReceiptLine, Material, MaterialIssue, MaterialIssueLine, PurchaseOrder, Supplier,
)
from procurement import services as req_services
from procurement.models import DirectDelivery, Requisition
from workforce import services as wf
from workforce.models import ContractorAdvance, PaymentCertificate, Payroll, WorkerAdvance

ZERO = Decimal("0")


def _num(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise ValidationError({label: "Enter a number."})
    return number


def _day(value):
    if not value:
        return timezone.localdate()
    day = parse_date(str(value))
    if day is None:
        raise ValidationError({"date": "Use the format YYYY-MM-DD."})
    return day


def _lines(data):
    lines = data.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValidationError({"lines": "Add at least one line."})
    return lines


# ---------------------------------------------------------------- material issue
class IssueView(APIView):
    def get_permissions(self):
        perms = [IsAuthenticated()]
        if self.request.method == "POST":
            perms.append(capability("issue_materials")())
        return perms

    def get(self, request, pk):
        project = project_for(request, pk)
        return Response([{
            "id": i.pk, "number": f"ISS-{i.pk}", "date": str(i.issue_date), "activity": i.activity,
            "requested_by": i.requested_by, "posted": i.posted,
            "lines": [{"material": l.material.name, "quantity": str(l.quantity_issued)} for l in i.lines.all()],
        } for i in project.material_issues.prefetch_related("lines__material")[:30]])

    @idempotent
    def post(self, request, pk):
        project = project_for(request, pk)
        data = request.data
        phase = None
        if data.get("phase"):
            phase = project.phases.filter(pk=data["phase"]).first()
            if phase is None:
                raise ValidationError({"phase": "Not a component of this project."})
        entries = []
        for n, row in enumerate(_lines(data), start=1):
            material = Material.objects.filter(pk=row.get("material"), is_active=True).first()
            qty = _num(row.get("quantity"), "lines")
            if material is None or qty <= 0:
                raise ValidationError({"lines": f"Line {n}: choose a material and a quantity above zero."})
            entries.append((material, qty))
        try:
            with transaction.atomic():
                issue = MaterialIssue.objects.create(
                    project=project, issue_date=_day(data.get("date")), phase=phase,
                    activity=str(data.get("activity", ""))[:200], requested_by=str(data.get("requested_by", ""))[:120],
                    notes=str(data.get("notes", "")), authorised_by=request.user,
                    created_by=request.user, updated_by=request.user)
                for material, qty in entries:
                    MaterialIssueLine.objects.create(issue=issue, material=material, quantity_issued=qty)
                stock.post_issue(issue, request.user)
        except WorkflowError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"id": issue.pk, "number": f"ISS-{issue.pk}", "posted": True}, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- goods received
class ReceivableOrdersView(APIView):
    permission_classes = [IsAuthenticated, capability("receive_goods")]

    def get(self, request, pk):
        project = project_for(request, pk)
        orders = project.purchase_orders.filter(
            status__in=(PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL)
        ).select_related("supplier").prefetch_related("lines__material__unit")
        out = []
        for po in orders:
            out.append({
                "id": po.pk, "number": po.number, "supplier": po.supplier.name, "date": str(po.order_date),
                "lines": [{
                    "id": l.pk, "material": l.material.name, "unit": l.material.unit.abbreviation,
                    "ordered": str(l.quantity), "received": str(l.received_quantity),
                    "outstanding": str(max(l.quantity - l.received_quantity, ZERO)),
                } for l in po.lines.all()],
            })
        return Response(out)


class ReceiveGoodsView(APIView):
    permission_classes = [IsAuthenticated, capability("receive_goods")]

    @idempotent
    def post(self, request, pk, po_id):
        project = project_for(request, pk)
        po = get_object_or_404(PurchaseOrder, pk=po_id, project=project)
        data = request.data
        entries = []
        for n, row in enumerate(_lines(data), start=1):
            pl = po.lines.filter(pk=row.get("po_line")).first()
            if pl is None:
                raise ValidationError({"lines": f"Line {n}: not a line of {po.number}."})
            delivered = _num(row.get("delivered"), "lines")
            accepted = delivered if row.get("accepted") in (None, "") else _num(row.get("accepted"), "lines")
            if delivered <= 0:
                continue
            if accepted < 0 or accepted > delivered:
                raise ValidationError({"lines": f"{pl.material.name}: accepted must be between 0 and the quantity delivered."})
            entries.append((pl, delivered, accepted))
        if not entries:
            raise ValidationError({"lines": "Enter the quantity delivered for at least one item."})
        try:
            with transaction.atomic():
                grn = GoodsReceipt.objects.create(
                    po=po, receipt_date=_day(data.get("date")), delivery_note_no=str(data.get("delivery_note", ""))[:60],
                    inspected_by=str(data.get("inspected_by", ""))[:120], notes=str(data.get("notes", "")),
                    created_by=request.user, updated_by=request.user)
                for pl, delivered, accepted in entries:
                    GoodsReceiptLine.objects.create(receipt=grn, po_line=pl, quantity_delivered=delivered,
                                                    quantity_accepted=accepted, quantity_rejected=delivered - accepted)
                stock.post_goods_receipt(grn, request.user)
        except WorkflowError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"id": grn.pk, "po": po.number, "posted": True}, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- photos and documents
def _not_contractor(user):
    return user.is_superuser or user.role != User.Role.CONTRACTOR


# ---------------------------------------------------------------- goods received without a purchase order
def _direct_json(dd):
    return {
        "id": dd.pk, "number": dd.number, "date": str(dd.delivery_date), "supplier": dd.supplier.name,
        "invoice_no": dd.invoice_no, "delivery_note": dd.delivery_note_no, "status": dd.status,
        "status_display": dd.get_status_display(), "reason": dd.reason, "total": str(dd.grand_total),
        "recorded_by": str(dd.created_by) if dd.created_by_id else None, "rejection_note": dd.rejection_note,
        "lines": [{"material": l.material.name, "delivered": str(l.quantity_delivered), "accepted": str(l.quantity_accepted),
                   "rejected": str(l.quantity_rejected), "unit_price": str(l.unit_price)} for l in dd.lines.all()],
    }


class SupplierListView(APIView):
    def get(self, request):
        if not _not_contractor(request.user):
            raise PermissionDenied()
        return Response([{"id": s.pk, "name": s.name, "phone": s.phone, "tin": s.tin}
                         for s in Supplier.objects.filter(is_active=True).order_by("name")])


class LastPricesView(APIView):
    """What each material cost on its most recent delivery, to help spot price jumps."""

    def get(self, request):
        if not _not_contractor(request.user):
            raise PermissionDenied()
        return Response({str(k): str(v) for k, v in req_services.last_prices().items()})


class DirectDeliveryView(APIView):
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_permissions(self):
        perms = [IsAuthenticated()]
        if self.request.method == "POST":
            perms.append(capability("receive_goods")())
        return perms

    def get(self, request, pk):
        if not _not_contractor(request.user):
            raise PermissionDenied()
        project = project_for(request, pk)
        qs = project.direct_deliveries.select_related("supplier", "created_by").prefetch_related("lines__material")
        return Response([_direct_json(d) for d in qs[:40]])

    @idempotent
    def post(self, request, pk):
        project = project_for(request, pk)
        data = request.data
        if hasattr(data, "get") and data.get("payload"):  # photo + details in one upload
            try:
                data = json.loads(data["payload"])
            except (TypeError, ValueError):
                raise ValidationError({"payload": "The details could not be read."})
        if not isinstance(data, dict):
            raise ValidationError({"detail": "Send the delivery details as an object."})
        try:
            with transaction.atomic():
                dd = req_services.record_direct_delivery(project, request.user, data)
                upload = request.FILES.get("file")
                if upload is not None:
                    att = Attachment(project=project, kind=Attachment.Kind.DELIVERY, caption=f"Delivery note {dd.number}",
                                     file=upload, original_name=upload.name[:200], direct_delivery=dd,
                                     created_by=request.user, updated_by=request.user)
                    try:
                        att.full_clean(exclude=["created_by", "updated_by"])
                    except DjangoValidationError as exc:
                        raise ValidationError({"file": exc.messages})
                    att.save()
        except WorkflowError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(_direct_json(dd), status=status.HTTP_201_CREATED)


def _file_json(a, request):
    return {
        "id": a.pk, "kind": a.kind, "kind_display": a.get_kind_display(), "caption": a.caption,
        "is_image": a.is_image, "date": a.created_at.date().isoformat(),
        "diary": a.diary_id, "purchase_order": a.purchase_order.number if a.purchase_order_id else None,
        "uploaded_by": str(a.created_by) if a.created_by_id else None,
        "download": request.build_absolute_uri(f"/api/v1/files/{a.pk}/download/"),
    }


class FileListCreateView(APIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        perms = [IsAuthenticated()]
        if self.request.method == "POST":
            perms.append(capability("attach_files")())
        return perms

    def get(self, request, pk):
        if not _not_contractor(request.user):
            raise PermissionDenied()
        project = project_for(request, pk)
        qs = project.attachments.select_related("purchase_order", "created_by")
        if request.query_params.get("kind") in Attachment.Kind.values:
            qs = qs.filter(kind=request.query_params["kind"])
        return Response([_file_json(a, request) for a in qs[:100]])

    @idempotent
    def post(self, request, pk):
        project = project_for(request, pk)
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Choose a file."})
        att = Attachment(project=project, kind=request.data.get("kind") or Attachment.Kind.PHOTO,
                         caption=str(request.data.get("caption", "")).strip()[:200], file=upload,
                         original_name=upload.name[:200], created_by=request.user, updated_by=request.user)
        if request.data.get("diary"):
            att.diary = project.diaries.filter(pk=request.data["diary"]).first()
            if att.diary is None:
                raise ValidationError({"diary": "Not a diary of this project."})
        if request.data.get("purchase_order"):
            att.purchase_order = project.purchase_orders.filter(pk=request.data["purchase_order"]).first()
            if att.purchase_order is None:
                raise ValidationError({"purchase_order": "Not an order of this project."})
        try:
            att.full_clean(exclude=["created_by", "updated_by"])
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "error_dict") else {"detail": exc.messages})
        att.save()
        return Response(_file_json(att, request), status=status.HTTP_201_CREATED)


class FileDownloadView(APIView):
    def get(self, request, pk):
        if not _not_contractor(request.user):
            raise PermissionDenied()
        att = get_object_or_404(Attachment, pk=pk, project__in=request.user.accessible_projects())
        ctype = mimetypes.guess_type(att.file.name)[0] or "application/octet-stream"
        response = FileResponse(att.file.open("rb"), content_type=ctype)
        response["X-Content-Type-Options"] = "nosniff"
        return response


# ---------------------------------------------------------------- approvals
def _approval_items(user):
    projects = user.accessible_projects()
    items = []

    def add(kind, obj, project, title, detail, amount, actions):
        items.append({
            "kind": kind, "id": obj.pk, "project": project.name, "title": title, "detail": detail,
            "amount": None if amount is None else str(amount), "actions": actions,
            "raised_by": str(obj.created_by) if getattr(obj, "created_by_id", None) else None,
        })

    for r in Requisition.objects.filter(project__in=projects, status__in=("submitted", "verified")) \
            .select_related("project", "created_by").prefetch_related("lines"):
        actions = []
        if r.status == "submitted" and can(user, "verify_requisition"):
            actions = ["verify", "reject"]
        if can(user, "approve_requisition") and (r.status == "verified" or (r.emergency and r.status == "submitted")):
            actions = ["approve", "reject"]
        if actions:
            add("requisition", r, r.project, f"{r.number}{' (EMERGENCY)' if r.emergency else ''}",
                f"{r.lines.count()} item(s)", r.estimated_total, actions)
    if can(user, "approve_po"):
        for po in PurchaseOrder.objects.filter(project__in=projects, status=PurchaseOrder.Status.SUBMITTED) \
                .select_related("project", "supplier", "created_by"):
            add("purchase_order", po, po.project, po.number, po.supplier.name, po.total, ["approve", "reject"])
    if can(user, "approve_direct_delivery"):
        for d in DirectDelivery.objects.filter(project__in=projects, status=DirectDelivery.Status.PENDING) \
                .select_related("project", "supplier", "created_by").prefetch_related("lines"):
            add("direct_delivery", d, d.project, d.number,
                f"{d.supplier.name}: delivery without an order. {d.reason}", d.grand_total, ["approve", "reject"])
    if can(user, "approve_certificate"):
        for c in PaymentCertificate.objects.filter(contract__project__in=projects, status="draft") \
                .select_related("contract__project", "contract__contractor", "created_by"):
            add("certificate", c, c.contract.project, c.number, f"{c.contract.contractor.name}: {c.description}",
                c.gross_amount, ["approve", "reject"])
    if can(user, "approve_mobilisation"):
        for a in ContractorAdvance.objects.filter(contract__project__in=projects, status="draft") \
                .select_related("contract__project", "contract__contractor", "created_by"):
            add("contractor_advance", a, a.contract.project, a.number,
                f"Mobilisation advance: {a.contract.contractor.name}", a.amount, ["approve", "reject"])
    if can(user, "approve_worker_advance"):
        for a in WorkerAdvance.objects.filter(worker__project__in=projects, status="draft") \
                .select_related("worker__project", "created_by"):
            add("worker_advance", a, a.worker.project, a.number, f"Salary advance: {a.worker.full_name}",
                a.amount, ["approve", "reject"])
    if can(user, "approve_payroll"):
        for p in Payroll.objects.filter(project__in=projects, status="draft").select_related("project", "created_by") \
                .prefetch_related("lines"):
            if p.lines.exists():
                add("payroll", p, p.project, p.number, f"Wages {p.period_start:%d/%m} to {p.period_end:%d/%m}",
                    p.net_total, ["approve"])
    return items


class ApprovalListView(APIView):
    def get(self, request):
        return Response(_approval_items(request.user))


def _po_reject(po, user):
    from accounts.permissions import require
    require(user, "approve_po")
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if po.status != PurchaseOrder.Status.SUBMITTED:
        raise WorkflowError("Only submitted purchase orders can be rejected.")
    po.status = PurchaseOrder.Status.CANCELLED
    po.save(update_fields=["status", "updated_at"])
    return po


KINDS = {
    "requisition": (lambda: Requisition, {"verify": req_services.verify_requisition, "approve": req_services.approve_requisition,
                                          "reject": req_services.reject_requisition}, "project"),
    "purchase_order": (lambda: PurchaseOrder, {"approve": stock.approve_po, "reject": _po_reject}, "project"),
    "direct_delivery": (lambda: DirectDelivery, {"approve": req_services.approve_direct_delivery,
                                                 "reject": req_services.reject_direct_delivery}, "project"),
    "certificate": (lambda: PaymentCertificate, {"approve": wf.approve_certificate, "reject": wf.reject_certificate},
                    "contract__project"),
    "contractor_advance": (lambda: ContractorAdvance, {"approve": wf.approve_contractor_advance,
                                                       "reject": wf.reject_contractor_advance}, "contract__project"),
    "worker_advance": (lambda: WorkerAdvance, {"approve": wf.approve_worker_advance, "reject": wf.reject_worker_advance},
                       "worker__project"),
    "payroll": (lambda: Payroll, {"approve": wf.approve_payroll}, "project"),
}


class ApprovalActionView(APIView):
    @idempotent
    def post(self, request, kind, pk, action):
        if kind not in KINDS or action not in KINDS[kind][1]:
            return Response({"detail": "Unknown action."}, status=status.HTTP_404_NOT_FOUND)
        model, funcs, project_path = KINDS[kind]
        obj = get_object_or_404(model().objects.filter(**{f"{project_path}__in": request.user.accessible_projects()}), pk=pk)
        try:
            funcs[action](obj, request.user)
        except WorkflowError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"kind": kind, "id": pk, "action": action, "done": True})
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import WorkflowError, can
from api import serializers as s
from api.permissions import capability
from boq import services as boq_services
from finance import supplier as supplier_services
from inventory import services as stock_services
from inventory.models import Material, PurchaseOrder
from procurement import services as req_services
from procurement.models import Requisition
from projects.models import SiteDiary
from workforce.models import Attendance, Worker


def project_for(request, pk):
    """404 (not 403) for projects the user cannot see, so their existence is not revealed."""
    return get_object_or_404(request.user.accessible_projects(), pk=pk)


def _s(value):
    return None if value is None else str(value)


# ---------------------------------------------------------------- auth
class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "login"

    def post(self, request):
        user = authenticate(request, username=request.data.get("username", ""), password=request.data.get("password", ""))
        if user is None or not user.is_active:
            return Response({"detail": "Wrong username or password."}, status=status.HTTP_401_UNAUTHORIZED)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "user": s.UserSerializer(user).data})


class LogoutView(APIView):
    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response(s.UserSerializer(request.user).data)


# ---------------------------------------------------------------- reference data
class ProjectListView(APIView):
    def get(self, request):
        projects = request.user.accessible_projects().prefetch_related("phases")
        return Response(s.ProjectSerializer(projects, many=True).data)


class MaterialListView(APIView):
    def get(self, request):
        qs = Material.objects.filter(is_active=True).select_related("unit", "category").order_by("category__name", "name")
        return Response(s.MaterialSerializer(qs, many=True).data)


# ---------------------------------------------------------------- reports
class StockView(APIView):
    def get(self, request, pk):
        project = project_for(request, pk)
        rows, totals = stock_services.stock_summary(project)
        return Response({
            "totals": {k: _s(v) for k, v in totals.items()},
            "materials": [{
                "material_id": r["material"].pk, "code": r["material"].code, "name": r["material"].name,
                "category": r["material"].category.name, "unit": r["material"].unit.abbreviation,
                "balance": _s(r["balance"]), "avg_cost": _s(r["avg_cost"]), "stock_value": _s(r["stock_value"]),
                "received": _s(r["received"]), "issued": _s(r["issued"]), "returned": _s(r["returned"]),
                "wasted": _s(r["wasted"]), "below_reorder": r["below_reorder"],
            } for r in rows],
        })


class BOQView(APIView):
    permission_classes = [IsAuthenticated, capability("view_boq")]

    def get(self, request, pk):
        project = project_for(request, pk)
        rows, phase_rows, totals = boq_services.item_report(project)
        return Response({
            "totals": {k: _s(v) for k, v in totals.items()},
            "items": [{
                "id": r["item"].pk, "code": r["item"].code, "description": r["item"].description,
                "unit": r["item"].unit.abbreviation, "phase": r["item"].phase.name if r["item"].phase_id else None,
                "quantity": _s(r["item"].revised_quantity), "rate": _s(r["item"].rate),
                "done": _s(r["done"]), "remaining": _s(r["remaining"]), "percent": _s(round(r["percent"], 1)),
                "estimated": _s(r["estimated"]), "earned": _s(r["earned"]), "overrun": r["overrun"],
            } for r in rows],
            "phases": [{
                "phase": p["phase"].name, "budget": _s(p["budget"]), "boq_estimate": _s(p["boq_estimate"]),
                "difference": _s(p["difference"]),
            } for p in phase_rows],
        })


# ---------------------------------------------------------------- site diary
class DiaryListCreateView(APIView):
    def get_permissions(self):
        perms = [IsAuthenticated()]
        if self.request.method == "POST":
            perms.append(capability("record_diary")())
        return perms

    def get(self, request, pk):
        project = project_for(request, pk)
        diaries = project.diaries.prefetch_related("activities").select_related("created_by")[:60]
        return Response(s.SiteDiarySerializer(diaries, many=True, context={"project": project}).data)

    def post(self, request, pk):
        project = project_for(request, pk)
        ser = s.SiteDiarySerializer(data=request.data, context={"request": request, "project": project})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            diary = ser.save()
        return Response(s.SiteDiarySerializer(diary, context={"project": project}).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- labour
class WorkerListView(APIView):
    permission_classes = [IsAuthenticated, capability("record_attendance")]

    def get(self, request, pk):
        project = project_for(request, pk)
        workers = Worker.objects.filter(project=project, is_active=True).select_related("contractor").order_by("full_name")
        return Response(s.WorkerSerializer(workers, many=True).data)


class AttendanceView(APIView):
    permission_classes = [IsAuthenticated, capability("record_attendance")]

    def get(self, request, pk):
        project = project_for(request, pk)
        qs = Attendance.objects.filter(worker__project=project).select_related("worker")
        date = request.query_params.get("date")
        if date:
            qs = qs.filter(date=date)
        return Response(s.AttendanceSerializer(qs[:500], many=True).data)

    def post(self, request, pk):
        """Record (or correct) a whole day's attendance in one call."""
        project = project_for(request, pk)
        ser = s.AttendanceBulkSerializer(data=request.data, context={"project": project})
        ser.is_valid(raise_exception=True)
        date, entries = ser.validated_data["date"], ser.validated_data["entries"]
        errors, saved = [], 0
        with transaction.atomic():
            for e in entries:
                name = e["worker"].full_name
                obj = Attendance.objects.filter(worker=e["worker"], date=date).first()
                if obj is None:
                    obj = Attendance(worker=e["worker"], date=date, created_by=request.user)
                elif obj.payroll_id:
                    errors.append(f"{name}: already included in a payroll and cannot be changed.")
                    continue
                obj.status, obj.hours_worked, obj.overtime_hours = e["status"], e["hours_worked"], e["overtime_hours"]
                obj.phase, obj.notes, obj.updated_by = e.get("phase"), e["notes"], request.user
                try:
                    obj.full_clean()
                except DjangoValidationError as exc:
                    errors.append(f"{name}: {s.full_clean_message(exc)}")
                    continue
                obj.save()
                saved += 1
            if errors:  # all or nothing: roll back the day's entries
                raise ValidationError({"entries": errors})
        return Response({"saved": saved, "date": str(date)}, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- requisitions
class RequisitionListCreateView(APIView):
    def get_permissions(self):
        perms = [IsAuthenticated()]
        if self.request.method == "POST":
            perms.append(capability("raise_requisition")())
        return perms

    def get(self, request, pk):
        project = project_for(request, pk)
        qs = project.requisitions.prefetch_related("lines__material").select_related("created_by")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response(s.RequisitionSerializer(qs[:100], many=True, context={"project": project}).data)

    def post(self, request, pk):
        project = project_for(request, pk)
        ser = s.RequisitionSerializer(data=request.data, context={"request": request, "project": project})
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            req = ser.save()
        return Response(s.RequisitionSerializer(req, context={"project": project}).data, status=status.HTTP_201_CREATED)


ACTIONS = {
    "submit": (req_services.submit_requisition, "raise_requisition"),
    "verify": (req_services.verify_requisition, None),
    "approve": (req_services.approve_requisition, None),
    "reject": (req_services.reject_requisition, None),
}


class RequisitionActionView(APIView):
    def post(self, request, pk, action):
        if action not in ACTIONS:
            return Response({"detail": "Unknown action."}, status=status.HTTP_404_NOT_FOUND)
        req = get_object_or_404(Requisition.objects.filter(project__in=request.user.accessible_projects()), pk=pk)
        func, cap = ACTIONS[action]
        if cap and not can(request.user, cap):
            return Response({"detail": "Your role is not permitted to do this."}, status=status.HTTP_403_FORBIDDEN)
        try:
            req = func(req, request.user)
        except WorkflowError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        req = Requisition.objects.prefetch_related("lines__material").get(pk=req.pk)
        return Response(s.RequisitionSerializer(req, context={"project": req.project}).data)


# ---------------------------------------------------------------- supplier accounts
class SupplierSummaryView(APIView):
    permission_classes = [IsAuthenticated, capability("view_finance")]

    def get(self, request, pk):
        project = project_for(request, pk)
        rows = supplier_services.supplier_summary(project)
        return Response([{
            "supplier_id": r["supplier"].pk, "supplier": r["supplier"].name, "orders": r["orders"],
            "ordered": _s(r["ordered"]), "delivered": _s(r["delivered"]), "paid": _s(r["paid"]),
            "advance_goods_not_delivered": _s(r["advance"]), "balance_due": _s(r["balance_due"]),
        } for r in rows])


class PurchaseOrderAccountView(APIView):
    permission_classes = [IsAuthenticated, capability("view_finance")]

    def get(self, request, pk, po_id):
        project = project_for(request, pk)
        po = get_object_or_404(PurchaseOrder, pk=po_id, project=project)
        a = supplier_services.po_account(po)
        tone, text = supplier_services.position_text(a)
        return Response({
            "po": po.number, "supplier": po.supplier.name, "position": text, "tone": tone,
            "ordered": _s(a["ordered"]), "delivered": _s(a["delivered"]), "paid": _s(a["paid"]),
            "awaiting_approval": _s(a["pending"]), "advance_goods_not_delivered": _s(a["advance"]),
            "balance_due": _s(a["balance_due"]),
            "items": [{
                "material": l["material"], "unit": l["unit"], "ordered": _s(l["ordered"]), "delivered": _s(l["delivered"]),
                "outstanding": _s(l["outstanding"]), "unit_price": _s(l["unit_price"]),
            } for l in a["lines"]],
        })
    
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts.models import User
from accounts.permissions import CAPABILITIES
from boq.models import BOQItem
from inventory.models import Material
from procurement.models import Requisition, RequisitionLine
from projects.models import Phase, Project, SiteActivity, SiteDiary, WorkPackage
from workforce.models import Attendance, Worker


def capabilities_for(user):
    if user.is_superuser:
        return sorted(CAPABILITIES)
    return sorted(name for name, roles in CAPABILITIES.items() if user.role in roles)


class UserSerializer(serializers.ModelSerializer):
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    capabilities = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "phone", "role", "role_display", "capabilities"]

    def get_capabilities(self, user):
        return capabilities_for(user)


class PhaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Phase
        fields = ["id", "name", "kind"]


class ProjectSerializer(serializers.ModelSerializer):
    phases = PhaseSerializer(many=True, read_only=True)

    class Meta:
        model = Project
        fields = ["id", "code", "name", "status", "location", "approved_budget", "phases"]


class MaterialSerializer(serializers.ModelSerializer):
    unit = serializers.CharField(source="unit.abbreviation")
    category = serializers.CharField(source="category.name")

    class Meta:
        model = Material
        fields = ["id", "code", "name", "category", "unit", "standard_price", "reorder_level"]


# ---------------------------------------------------------------- site diary
class SiteActivitySerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteActivity
        fields = ["id", "phase", "work_package", "location", "description", "work_completed"]


class SiteDiarySerializer(serializers.ModelSerializer):
    activities = SiteActivitySerializer(many=True, required=False)
    recorded_by = serializers.SerializerMethodField()

    class Meta:
        model = SiteDiary
        fields = ["id", "date", "weather", "workers_present", "equipment_used", "challenges",
                  "safety_incidents", "instructions_issued", "remarks", "activities", "recorded_by"]

    def get_recorded_by(self, diary):
        return str(diary.created_by) if diary.created_by_id else None

    def validate_activities(self, activities):
        project = self.context["project"]
        for a in activities:
            if a.get("phase") and a["phase"].project_id != project.pk:
                raise serializers.ValidationError("A component does not belong to this project.")
            wp = a.get("work_package")
            if wp and wp.phase.project_id != project.pk:
                raise serializers.ValidationError("A work package does not belong to this project.")
        return activities

    def validate_date(self, value):
        project = self.context["project"]
        if SiteDiary.objects.filter(project=project, date=value).exists():
            raise serializers.ValidationError("A site diary for this date already exists.")
        return value

    def create(self, validated):
        activities = validated.pop("activities", [])
        user = self.context["request"].user
        diary = SiteDiary.objects.create(project=self.context["project"], supervisor=user,
                                         created_by=user, updated_by=user, **validated)
        SiteActivity.objects.bulk_create([SiteActivity(diary=diary, **a) for a in activities])
        return diary


# ---------------------------------------------------------------- labour
class WorkerSerializer(serializers.ModelSerializer):
    contractor = serializers.CharField(source="contractor.name", default=None, read_only=True)

    class Meta:
        model = Worker
        fields = ["id", "full_name", "trade", "phone", "contractor"]


class ProjectWorkerField(serializers.PrimaryKeyRelatedField):
    """Only active workers of the project in the request context (context is read at validation time)."""

    def get_queryset(self):
        return Worker.objects.filter(project=self.context["project"], is_active=True)


class ProjectPhaseField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return Phase.objects.filter(project=self.context["project"])


class AttendanceEntrySerializer(serializers.Serializer):
    worker = ProjectWorkerField()
    status = serializers.ChoiceField(choices=Attendance.Status.choices, default=Attendance.Status.PRESENT)
    hours_worked = serializers.DecimalField(max_digits=4, decimal_places=1, default=Decimal("8"))
    overtime_hours = serializers.DecimalField(max_digits=4, decimal_places=1, default=Decimal("0"))
    phase = ProjectPhaseField(required=False, allow_null=True)
    notes = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class AttendanceBulkSerializer(serializers.Serializer):
    date = serializers.DateField()
    entries = AttendanceEntrySerializer(many=True, allow_empty=False)

    def validate_entries(self, entries):
        ids = [e["worker"].pk for e in entries]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("A worker appears more than once.")
        return entries


class AttendanceSerializer(serializers.ModelSerializer):
    worker_name = serializers.CharField(source="worker.full_name", read_only=True)

    class Meta:
        model = Attendance
        fields = ["id", "worker", "worker_name", "date", "status", "hours_worked", "overtime_hours", "phase", "notes"]


def full_clean_message(exc):
    if isinstance(exc, DjangoValidationError):
        return "; ".join(exc.messages)
    return str(exc)


# ---------------------------------------------------------------- requisitions
class RequisitionLineSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source="material.name", read_only=True)

    class Meta:
        model = RequisitionLine
        fields = ["id", "material", "material_name", "quantity", "estimated_unit_price", "boq_item"]
        extra_kwargs = {"estimated_unit_price": {"required": False}, "boq_item": {"required": False}}

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        return value


class RequisitionSerializer(serializers.ModelSerializer):
    lines = RequisitionLineSerializer(many=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    raised_by = serializers.SerializerMethodField()
    estimated_total = serializers.SerializerMethodField()

    class Meta:
        model = Requisition
        fields = ["id", "number", "status", "status_display", "needed_by", "phase", "work_package", "emergency",
                  "justification", "notes", "preferred_supplier", "verification_notes", "raised_by",
                  "estimated_total", "lines"]
        read_only_fields = ["number", "status", "verification_notes"]

    def get_raised_by(self, req):
        return str(req.created_by) if req.created_by_id else None

    def get_estimated_total(self, req):
        return str(sum((l.estimated_total for l in req.lines.all()), Decimal("0")))

    def validate(self, data):
        project = self.context["project"]
        if data.get("phase") and data["phase"].project_id != project.pk:
            raise serializers.ValidationError({"phase": "Component does not belong to this project."})
        wp = data.get("work_package")
        if wp and wp.phase.project_id != project.pk:
            raise serializers.ValidationError({"work_package": "Work package does not belong to this project."})
        if data.get("emergency") and not (data.get("justification") or "").strip():
            raise serializers.ValidationError({"justification": "Emergency requisitions need a justification."})
        for line in data.get("lines", []):
            boq = line.get("boq_item")
            if boq and boq.project_id != project.pk:
                raise serializers.ValidationError({"lines": "A BOQ item does not belong to this project."})
            if boq and boq.material_id and boq.material_id != line["material"].pk:
                raise serializers.ValidationError({"lines": "A BOQ item is linked to a different material."})
        if not data.get("lines"):
            raise serializers.ValidationError({"lines": "Add at least one line."})
        return data

    def create(self, validated):
        lines = validated.pop("lines")
        user = self.context["request"].user
        req = Requisition.objects.create(project=self.context["project"], created_by=user, updated_by=user, **validated)
        for line in lines:
            RequisitionLine(requisition=req, **line).save()
        return req
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from core.models import AuditedModel


class Project(AuditedModel):
    class Status(models.TextChoices):
        PLANNING = "planning", "Planning"
        ACTIVE = "active", "Active"
        ON_HOLD = "on_hold", "On hold"
        COMPLETED = "completed", "Completed"

    class StarTarget(models.TextChoices):
        THREE = "3", "3-star"
        FOUR = "4", "4-star"
        FOUR_PLUS = "4+", "4-star with selected 5-star facilities"
        FIVE = "5", "5-star"

    name = models.CharField(max_length=200)
    code = models.CharField(max_length=30, unique=True)
    description = models.TextField(blank=True)
    location = models.CharField(max_length=200, blank=True)
    district = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    land_size_sqm = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    owner_name = models.CharField("owner/developer", max_length=200, blank=True)
    project_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="managed_projects",
    )
    architect = models.CharField(max_length=200, blank=True)
    structural_engineer = models.CharField(max_length=200, blank=True)
    quantity_surveyor = models.CharField(max_length=200, blank=True)
    mechanical_engineer = models.CharField(max_length=200, blank=True)
    electrical_engineer = models.CharField(max_length=200, blank=True)
    main_contractor = models.CharField(max_length=200, blank=True)

    start_date = models.DateField(null=True, blank=True)
    expected_completion = models.DateField(null=True, blank=True)
    approved_budget = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    financing_notes = models.TextField(blank=True)
    planned_floors = models.PositiveSmallIntegerField(null=True, blank=True)
    planned_rooms = models.PositiveSmallIntegerField(null=True, blank=True)
    star_target = models.CharField(max_length=2, choices=StarTarget.choices, default=StarTarget.FOUR_PLUS)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PLANNING)

    members = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="projects")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    def overall_progress(self):
        """Budget-weighted completion %, or a simple average when no phase budgets are set."""
        phases = list(self.phases.all())
        if not phases:
            return Decimal("0")
        total_budget = sum((p.budget for p in phases), Decimal("0"))
        if total_budget > 0:
            weighted = sum((p.budget * p.percent_complete for p in phases), Decimal("0"))
            return (weighted / total_budget).quantize(Decimal("0.1"))
        return (sum((p.percent_complete for p in phases), Decimal("0")) / len(phases)).quantize(Decimal("0.1"))


class Phase(models.Model):
    """A construction component (foundation, roofing...) or a hotel facility (restaurant, spa...)."""

    class Kind(models.TextChoices):
        CONSTRUCTION = "construction", "Construction component"
        FACILITY = "facility", "Hotel facility"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="phases")
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CONSTRUCTION)
    sequence = models.PositiveSmallIntegerField(default=0)
    planned_start = models.DateField(null=True, blank=True)
    planned_end = models.DateField(null=True, blank=True)
    actual_start = models.DateField(null=True, blank=True)
    actual_end = models.DateField(null=True, blank=True)
    budget = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    percent_complete = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["project", "sequence", "name"]
        unique_together = [("project", "name")]

    def __str__(self):
        return f"{self.name}"

    @property
    def is_delayed(self):
        return bool(self.planned_end and self.planned_end < timezone.localdate() and self.percent_complete < 100)


class WorkPackage(models.Model):
    phase = models.ForeignKey(Phase, on_delete=models.CASCADE, related_name="work_packages")
    code = models.CharField(max_length=30)
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["phase", "code"]
        unique_together = [("phase", "code")]

    def __str__(self):
        return f"{self.code} – {self.name}"


class SiteDiary(AuditedModel):
    class Weather(models.TextChoices):
        SUNNY = "sunny", "Sunny"
        CLOUDY = "cloudy", "Cloudy"
        RAINY = "rainy", "Rainy"
        STORMY = "stormy", "Stormy"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="diaries")
    date = models.DateField(default=timezone.localdate)
    weather = models.CharField(max_length=10, choices=Weather.choices, default=Weather.SUNNY)
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    workers_present = models.PositiveIntegerField(default=0)
    equipment_used = models.TextField(blank=True)
    challenges = models.TextField(blank=True)
    safety_incidents = models.TextField(blank=True)
    instructions_issued = models.TextField(blank=True)
    remarks = models.TextField(blank=True)

    class Meta:
        ordering = ["-date"]
        unique_together = [("project", "date")]
        verbose_name_plural = "site diaries"

    def __str__(self):
        return f"{self.project.code} diary {self.date:%d/%m/%Y}"


class SiteActivity(models.Model):
    diary = models.ForeignKey(SiteDiary, on_delete=models.CASCADE, related_name="activities")
    work_package = models.ForeignKey(WorkPackage, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    phase = models.ForeignKey(Phase, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    location = models.CharField("building section", max_length=150, blank=True)
    description = models.CharField(max_length=300)
    work_completed = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "site activities"

    def __str__(self):
        return self.description

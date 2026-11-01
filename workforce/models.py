from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from core.models import AuditedModel

ZERO = Decimal("0")
MONEY = dict(max_digits=16, decimal_places=2)


def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------ contractors
class Contractor(AuditedModel):
    name = models.CharField(max_length=200, unique=True)
    registration_no = models.CharField(max_length=60, blank=True)
    tin = models.CharField("TIN", max_length=30, blank=True)
    contact_person = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=250, blank=True)
    insurance_expiry = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Contract(AuditedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        TERMINATED = "terminated", "Terminated"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="contracts")
    contractor = models.ForeignKey(Contractor, on_delete=models.PROTECT, related_name="contracts")
    reference = models.CharField(max_length=60, unique=True)
    scope = models.TextField()
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    amount = models.DecimalField("contract amount (UGX)", **MONEY)
    retention_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage held back from each payment certificate",
    )
    start_date = models.DateField(null=True, blank=True)
    expected_end = models.DateField(null=True, blank=True)
    payment_terms = models.CharField(max_length=200, blank=True)
    performance_guarantee = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.reference} – {self.contractor}"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Contract amount must be greater than zero."})

    @property
    def certified_gross(self):
        return self.certificates.filter(status=PaymentCertificate.Status.APPROVED) \
            .aggregate(t=Sum("gross_amount"))["t"] or ZERO

    @property
    def retention_held(self):
        return self.certificates.filter(status=PaymentCertificate.Status.APPROVED) \
            .aggregate(t=Sum("retention_amount"))["t"] or ZERO

    @property
    def balance(self):
        return self.amount - self.certified_gross


class PaymentCertificate(AuditedModel):
    """Interim payment certificate. Approving it books an approved subcontractor expense
    for the net amount (gross less retention); the Accountant then records the payment."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    contract = models.ForeignKey(Contract, on_delete=models.PROTECT, related_name="certificates")
    number = models.CharField(max_length=20, unique=True, blank=True, editable=False)
    date = models.DateField(default=timezone.localdate)
    description = models.CharField(max_length=300, help_text="Work certified, e.g. Foundation slab complete")
    gross_amount = models.DecimalField("gross amount this certificate (UGX)", **MONEY)
    cumulative_percent = models.DecimalField(
        "cumulative % complete", max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, editable=False)
    retention_amount = models.DecimalField(null=True, editable=False, **MONEY)
    net_amount = models.DecimalField(null=True, editable=False, **MONEY)
    approved_by = models.ForeignKey("accounts.User", null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number or "New certificate"

    def save(self, *args, **kwargs):
        if not self.number:
            last = PaymentCertificate.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            self.number = f"CERT-{last + 1:04d}"
        super().save(*args, **kwargs)

    def clean(self):
        if self.gross_amount is not None and self.gross_amount <= 0:
            raise ValidationError({"gross_amount": "Gross amount must be greater than zero."})
        if self._state.adding and self.contract_id and self.contract.status != Contract.Status.ACTIVE:
            raise ValidationError("Certificates can only be raised against an active contract.")

    @property
    def preview_retention(self):
        return money(self.gross_amount * self.contract.retention_percent / 100)

    @property
    def preview_net(self):
        return self.gross_amount - self.preview_retention


# ------------------------------------------------------------ workers
class Worker(AuditedModel):
    class Trade(models.TextChoices):
        MASON = "mason", "Mason"
        CARPENTER = "carpenter", "Carpenter"
        ELECTRICIAN = "electrician", "Electrician"
        PLUMBER = "plumber", "Plumber"
        WELDER = "welder", "Welder"
        PAINTER = "painter", "Painter"
        STEEL_FIXER = "steel_fixer", "Steel fixer"
        LABOURER = "labourer", "General labourer"
        OPERATOR = "operator", "Machine operator"
        SUPERVISOR = "supervisor", "Site supervisor"
        ENGINEER = "engineer", "Engineer"
        SECURITY = "security", "Security personnel"
        OTHER = "other", "Other"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="workers")
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=30, blank=True)
    trade = models.CharField(max_length=15, choices=Trade.choices, default=Trade.LABOURER)
    contractor = models.ForeignKey(
        Contractor, null=True, blank=True, on_delete=models.PROTECT, related_name="workers",
        help_text="Leave empty for workers employed directly by the project (paid through payroll)",
    )
    daily_wage = models.DecimalField("daily wage (UGX)", default=0, **MONEY,
                                     help_text="Used for direct-hire workers only")
    date_engaged = models.DateField(default=timezone.localdate)
    next_of_kin = models.CharField(max_length=150, blank=True)
    next_of_kin_phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.get_trade_display()})"

    def clean(self):
        if self.contractor_id is None and self.daily_wage is not None and self.daily_wage <= 0:
            raise ValidationError({"daily_wage": "Direct-hire workers need a daily wage."})


class Attendance(AuditedModel):
    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        ABSENT = "absent", "Absent"

    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, related_name="attendance")
    date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PRESENT)
    hours_worked = models.DecimalField(max_digits=4, decimal_places=1, default=8)
    overtime_hours = models.DecimalField(max_digits=4, decimal_places=1, default=0)
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                              help_text="Component the worker was on (for labour cost by component)")
    notes = models.CharField(max_length=200, blank=True)
    payroll = models.ForeignKey("Payroll", null=True, blank=True, editable=False,
                                on_delete=models.SET_NULL, related_name="attendance")

    class Meta:
        ordering = ["-date", "worker__full_name"]
        unique_together = [("worker", "date")]
        verbose_name_plural = "attendance"

    def __str__(self):
        return f"{self.worker.full_name} {self.date:%d/%m/%Y}"

    def clean(self):
        if self.status == self.Status.ABSENT:
            self.hours_worked = ZERO
            self.overtime_hours = ZERO
        if self.hours_worked is not None and not 0 <= self.hours_worked <= 24:
            raise ValidationError({"hours_worked": "Hours must be between 0 and 24."})
        if self.overtime_hours is not None and not 0 <= self.overtime_hours <= 12:
            raise ValidationError({"overtime_hours": "Overtime must be between 0 and 12 hours."})
        if self.phase_id and self.worker_id and self.phase.project_id != self.worker.project_id:
            raise ValidationError({"phase": "This component belongs to a different project."})


# ------------------------------------------------------------ payroll
class Payroll(AuditedModel):
    """Wages for direct-hire workers over a period, built from unpaid attendance.
    Approving it books an approved labour expense; each attendance day can be paid once."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="payrolls")
    number = models.CharField(max_length=20, unique=True, blank=True, editable=False)
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, editable=False)
    approved_by = models.ForeignKey("accounts.User", null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-period_end", "-id"]

    def __str__(self):
        return self.number or "New payroll"

    def save(self, *args, **kwargs):
        if not self.number:
            last = Payroll.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            self.number = f"PAY-{last + 1:04d}"
        super().save(*args, **kwargs)

    def eligible_attendance(self):
        return Attendance.objects.filter(
            worker__project=self.project, worker__contractor__isnull=True,
            status=Attendance.Status.PRESENT, payroll__isnull=True,
            date__gte=self.period_start, date__lte=self.period_end,
        )

    def clean(self):
        if not (self.project_id and self.period_start and self.period_end):
            return
        if self.period_end < self.period_start:
            raise ValidationError({"period_end": "The period must end on or after its start."})
        if self._state.adding and not self.eligible_attendance().exists():
            raise ValidationError("There is no unpaid attendance for direct-hire workers in this period.")

    @property
    def total(self):
        return sum((l.total for l in self.lines.all()), ZERO)


class PayrollLine(models.Model):
    payroll = models.ForeignKey(Payroll, on_delete=models.CASCADE, related_name="lines")
    worker = models.ForeignKey(Worker, on_delete=models.PROTECT, related_name="+")
    days_present = models.DecimalField(max_digits=6, decimal_places=2)
    regular_pay = models.DecimalField(**MONEY)
    overtime_pay = models.DecimalField(**MONEY)

    @property
    def total(self):
        return self.regular_pay + self.overtime_pay

    def __str__(self):
        return f"{self.worker.full_name}: {self.total:,.0f}"

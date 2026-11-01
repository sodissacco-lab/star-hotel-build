from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from core.models import AuditedModel

QTY = dict(max_digits=14, decimal_places=3)
ZERO = Decimal("0")


class BOQItem(AuditedModel):
    """One line of the approved Bill of Quantities.

    The original quantity is fixed once the item is created; it changes only
    through approved variation orders, which update the revised quantity.
    """

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="boq_items")
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="boq_items")
    work_package = models.ForeignKey("projects.WorkPackage", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    code = models.CharField("item code", max_length=30)
    description = models.CharField(max_length=300)
    unit = models.ForeignKey("inventory.Unit", on_delete=models.PROTECT, related_name="+")
    material = models.ForeignKey(
        "inventory.Material", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Optional: link a material item so site issues can be compared with the BOQ quantity",
    )
    original_quantity = models.DecimalField(**QTY)
    revised_quantity = models.DecimalField(editable=False, default=0, **QTY)
    rate = models.DecimalField("rate (UGX)", max_digits=16, decimal_places=2)

    class Meta:
        ordering = ["project", "code"]
        unique_together = [("project", "code")]

    def __str__(self):
        return f"{self.code} – {self.description}"

    def save(self, *args, **kwargs):
        if self.pk is None:
            self.revised_quantity = self.original_quantity
        super().save(*args, **kwargs)

    def clean(self):
        if self.original_quantity is not None and self.original_quantity < 0:
            raise ValidationError({"original_quantity": "Quantity cannot be negative."})
        if self.rate is not None and self.rate < 0:
            raise ValidationError({"rate": "Rate cannot be negative."})

    @property
    def estimated_cost(self):
        return self.revised_quantity * self.rate

    @property
    def quantity_done(self):
        return self.measurements.aggregate(t=Sum("quantity"))["t"] or ZERO


class BOQMeasurement(AuditedModel):
    """Measured work completed against a BOQ item (append-only; correct with a negative entry)."""

    item = models.ForeignKey(BOQItem, on_delete=models.PROTECT, related_name="measurements")
    date = models.DateField(default=timezone.localdate)
    quantity = models.DecimalField("quantity measured", **QTY, help_text="Use a negative number to correct an earlier entry")
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.item.code} {self.quantity:+} on {self.date:%d/%m/%Y}"

    def clean(self):
        if self.quantity is None:
            return
        if self.quantity == 0:
            raise ValidationError({"quantity": "Quantity cannot be zero."})
        if self.item_id:
            others = self.item.measurements.exclude(pk=self.pk).aggregate(t=Sum("quantity"))["t"] or ZERO
            total = others + self.quantity
            if total < 0:
                raise ValidationError("Total measured quantity cannot be negative.")
            if total > self.item.revised_quantity:
                raise ValidationError(
                    f"Total measured ({total}) would exceed the approved quantity ({self.item.revised_quantity}). "
                    "Raise a variation order first."
                )


class Variation(AuditedModel):
    """Variation order: an approved change to a BOQ item's quantity."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    item = models.ForeignKey(BOQItem, on_delete=models.PROTECT, related_name="variations")
    number = models.CharField(max_length=20, unique=True, blank=True, editable=False)
    date = models.DateField(default=timezone.localdate)
    description = models.CharField(max_length=300)
    reason = models.CharField("instruction / reason", max_length=300, blank=True,
                              help_text="e.g. Structural engineer's instruction no. 12")
    quantity_change = models.DecimalField("quantity change (+/-)", **QTY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, editable=False)
    approved_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number or "New variation"

    def save(self, *args, **kwargs):
        if not self.number:
            last = Variation.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            self.number = f"VO-{last + 1:04d}"
        super().save(*args, **kwargs)

    def clean(self):
        if self.quantity_change == 0:
            raise ValidationError({"quantity_change": "A variation must change the quantity."})

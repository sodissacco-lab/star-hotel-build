from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import AuditedModel

QTY = dict(max_digits=14, decimal_places=3)
MONEY = dict(max_digits=16, decimal_places=2)


class Requisition(AuditedModel):
    """Purchase requisition: site need -> QS verification -> approval -> purchase order."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        VERIFIED = "verified", "Verified by QS"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        ORDERED = "ordered", "Purchase order raised"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="requisitions")
    number = models.CharField(max_length=20, unique=True, blank=True, editable=False)
    needed_by = models.DateField(null=True, blank=True)
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    work_package = models.ForeignKey("projects.WorkPackage", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    emergency = models.BooleanField(default=False, help_text="Skips QS verification; a justification is required")
    justification = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    preferred_supplier = models.ForeignKey(
        "inventory.Supplier", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Used when there are no quotations (e.g. emergency purchases)",
    )
    verification_notes = models.TextField(blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, editable=False)
    verified_by = models.ForeignKey("accounts.User", null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    approved_by = models.ForeignKey("accounts.User", null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    purchase_order = models.OneToOneField(
        "inventory.PurchaseOrder", null=True, blank=True, editable=False, on_delete=models.SET_NULL,
        related_name="requisition",
    )

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return self.number or "New requisition"

    def save(self, *args, **kwargs):
        if not self.number:
            last = Requisition.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            self.number = f"REQ-{last + 1:05d}"
        super().save(*args, **kwargs)

    def clean(self):
        if self.emergency and not (self.justification or "").strip():
            raise ValidationError({"justification": "Emergency requisitions need a justification."})

    @property
    def estimated_total(self):
        return sum((l.estimated_total for l in self.lines.all()), Decimal("0"))


class RequisitionLine(models.Model):
    requisition = models.ForeignKey(Requisition, on_delete=models.CASCADE, related_name="lines")
    material = models.ForeignKey("inventory.Material", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(**QTY)
    estimated_unit_price = models.DecimalField(
        "estimated unit price (UGX)", default=0, blank=True, **MONEY,
        help_text="Leave 0 to use the material's standard price",
    )
    boq_item = models.ForeignKey("boq.BOQItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="requisition_lines")

    def __str__(self):
        return f"{self.material.name} x {self.quantity}"

    def save(self, *args, **kwargs):
        if not self.estimated_unit_price:
            self.estimated_unit_price = self.material.standard_price
        super().save(*args, **kwargs)

    @property
    def estimated_total(self):
        return (self.quantity or Decimal("0")) * (self.estimated_unit_price or Decimal("0"))

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})
        if self.boq_item_id and self.material_id and self.boq_item.material_id \
                and self.boq_item.material_id != self.material_id:
            raise ValidationError({"boq_item": "This BOQ item is linked to a different material."})


class Quotation(AuditedModel):
    requisition = models.ForeignKey(Requisition, on_delete=models.CASCADE, related_name="quotations")
    supplier = models.ForeignKey("inventory.Supplier", on_delete=models.PROTECT, related_name="quotations")
    amount = models.DecimalField("quoted total (UGX)", **MONEY)
    valid_until = models.DateField(null=True, blank=True)
    is_selected = models.BooleanField("selected", default=False)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["amount"]
        unique_together = [("requisition", "supplier")]

    def __str__(self):
        return f"{self.supplier} – UGX {self.amount:,.0f}"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Quoted amount must be greater than zero."})
        if self.valid_until and self.valid_until < timezone.localdate() and self._state.adding:
            raise ValidationError({"valid_until": "This quotation has already expired."})

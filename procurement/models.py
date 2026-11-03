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


# ---------------------------------------------------------------- deliveries without a purchase order
class DirectDelivery(AuditedModel):
    """Goods that arrive on site with no purchase order (a cash purchase, a walk-in buy, a supplier who
    delivered before the paperwork). The site records what arrived; nothing reaches stock or the supplier's
    account until an authorised person approves it. Approval creates an approved purchase order, a posted
    goods receipt, the material expense and, if money was paid on the spot, the supplier payment, so stock,
    expenditure and the supplier statement all stay consistent with ordered deliveries."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved and booked into stock"
        REJECTED = "rejected", "Rejected"

    class PayMethod(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        CHEQUE = "cheque", "Cheque"
        MOBILE_MONEY = "mobile_money", "Mobile money"
        OTHER = "other", "Other"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="direct_deliveries")
    number = models.CharField(max_length=20, unique=True, blank=True, editable=False)
    supplier = models.ForeignKey("inventory.Supplier", on_delete=models.PROTECT, related_name="direct_deliveries")
    delivery_date = models.DateField(default=timezone.localdate)
    delivery_note_no = models.CharField(max_length=60, blank=True)
    invoice_no = models.CharField("supplier invoice no.", max_length=60, blank=True)
    vehicle_reg = models.CharField("vehicle registration", max_length=30, blank=True)
    driver_name = models.CharField(max_length=120, blank=True)
    driver_phone = models.CharField(max_length=30, blank=True)
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reason = models.CharField("why there was no purchase order", max_length=300)
    notes = models.TextField(blank=True)

    transport_charges = models.DecimalField("transport / loading charges (UGX)", default=0, **MONEY)
    other_charges = models.DecimalField("other charges (UGX)", default=0, **MONEY)
    amount_paid = models.DecimalField("paid to the supplier for the goods (UGX)", default=0, **MONEY)
    payment_method = models.CharField(max_length=15, choices=PayMethod.choices, default=PayMethod.CASH, blank=True)
    payment_reference = models.CharField(max_length=60, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, editable=False)
    approved_by = models.ForeignKey("accounts.User", null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    rejection_note = models.CharField(max_length=300, blank=True, editable=False)
    purchase_order = models.OneToOneField("inventory.PurchaseOrder", null=True, blank=True, editable=False,
                                          on_delete=models.SET_NULL, related_name="direct_delivery")
    receipt = models.OneToOneField("inventory.GoodsReceipt", null=True, blank=True, editable=False,
                                   on_delete=models.SET_NULL, related_name="direct_delivery")

    class Meta:
        ordering = ["-id"]
        verbose_name_plural = "deliveries without an order"

    def __str__(self):
        return self.number or "New delivery"

    def save(self, *args, **kwargs):
        if not self.number:
            last = DirectDelivery.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            self.number = f"DD-{last + 1:05d}"
        super().save(*args, **kwargs)

    @staticmethod
    def find_duplicate_invoice(supplier, invoice_no, exclude_pk=None):
        """The number of an earlier record carrying this supplier invoice number, or None."""
        invoice = (invoice_no or "").strip()
        if not invoice or supplier is None:
            return None
        qs = DirectDelivery.objects.filter(supplier=supplier, invoice_no__iexact=invoice).exclude(status=DirectDelivery.Status.REJECTED)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
        hit = qs.first()
        if hit:
            return hit.number
        from finance.models import DemandNote
        dn = DemandNote.objects.filter(purchase_order__supplier=supplier, supplier_ref__iexact=invoice) \
            .exclude(status=DemandNote.Status.REJECTED).first()
        return dn.number if dn else None

    @property
    def accepted_value(self):
        return sum((l.accepted_value for l in self.lines.all()), Decimal("0"))

    @property
    def delivered_value(self):
        return sum((l.delivered_value for l in self.lines.all()), Decimal("0"))

    @property
    def grand_total(self):
        return self.accepted_value + (self.transport_charges or Decimal("0")) + (self.other_charges or Decimal("0"))

    def clean(self):
        errors = {}
        if self.supplier_id:
            dup = DirectDelivery.find_duplicate_invoice(self.supplier, self.invoice_no, exclude_pk=self.pk)
            if dup:
                errors["invoice_no"] = f"This supplier's invoice {self.invoice_no} is already recorded ({dup})."
        for field in ("transport_charges", "other_charges", "amount_paid"):
            value = getattr(self, field)
            if value is not None and value < 0:
                errors[field] = "Cannot be negative."
        if errors:
            raise ValidationError(errors)


class DirectDeliveryLine(models.Model):
    delivery = models.ForeignKey(DirectDelivery, on_delete=models.CASCADE, related_name="lines")
    material = models.ForeignKey("inventory.Material", on_delete=models.PROTECT, related_name="+")
    quantity_delivered = models.DecimalField(**QTY)
    quantity_accepted = models.DecimalField(**QTY)
    quantity_rejected = models.DecimalField(default=0, **QTY)
    unit_price = models.DecimalField("unit price (UGX)", **MONEY)
    condition = models.CharField(max_length=200, blank=True, help_text="e.g. good, damaged, substandard")

    def __str__(self):
        return f"{self.material.name} x {self.quantity_delivered}"

    @property
    def accepted_value(self):
        return (self.quantity_accepted or Decimal("0")) * (self.unit_price or Decimal("0"))

    @property
    def delivered_value(self):
        return (self.quantity_delivered or Decimal("0")) * (self.unit_price or Decimal("0"))

    def clean(self):
        d, a, r = self.quantity_delivered, self.quantity_accepted, self.quantity_rejected
        if None not in (d, a, r):
            if d <= 0:
                raise ValidationError({"quantity_delivered": "Quantity must be greater than zero."})
            if min(a, r) < 0:
                raise ValidationError("Quantities cannot be negative.")
            if a + r != d:
                raise ValidationError("Accepted + rejected must equal the quantity delivered.")
        if self.unit_price is not None and self.unit_price <= 0:
            raise ValidationError({"unit_price": "Enter the price paid per unit."})
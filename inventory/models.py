from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone

from core.models import AuditedModel

QTY = dict(max_digits=14, decimal_places=3)
MONEY = dict(max_digits=16, decimal_places=2)


class MaterialCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "material categories"

    def __str__(self):
        return self.name


class Unit(models.Model):
    name = models.CharField(max_length=50, unique=True)
    abbreviation = models.CharField(max_length=15)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.abbreviation


class Supplier(AuditedModel):
    name = models.CharField(max_length=200, unique=True)
    registration_no = models.CharField(max_length=60, blank=True)
    tin = models.CharField("TIN", max_length=30, blank=True)
    address = models.CharField(max_length=250, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    payment_details = models.TextField(blank=True, help_text="Bank / mobile money details (restricted)")
    rating = models.PositiveSmallIntegerField(null=True, blank=True, help_text="1 (poor) to 5 (excellent)")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.rating is not None and not 1 <= self.rating <= 5:
            raise ValidationError({"rating": "Rating must be between 1 and 5."})


class Material(AuditedModel):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=200)
    category = models.ForeignKey(MaterialCategory, on_delete=models.PROTECT, related_name="materials")
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name="+")
    specification = models.CharField(max_length=300, blank=True)
    preferred_supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    standard_price = models.DecimalField("standard estimated price (UGX)", default=0, **MONEY)
    reorder_level = models.DecimalField(default=0, **QTY)
    storage_location = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["category__name", "name"]

    def __str__(self):
        return f"{self.code} – {self.name} ({self.unit})"


# ---------------------------------------------------------------- procurement
class PurchaseOrder(AuditedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted for approval"
        APPROVED = "approved", "Approved"
        PARTIAL = "partial", "Partially received"
        RECEIVED = "received", "Fully received"
        CANCELLED = "cancelled", "Cancelled"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="purchase_orders")
    number = models.CharField(max_length=30, unique=True, blank=True, editable=False)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    order_date = models.DateField(default=timezone.localdate)
    requisition_ref = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, editable=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-order_date", "-id"]

    def __str__(self):
        return self.number or "New purchase order"

    def save(self, *args, **kwargs):
        if not self.number:
            last = PurchaseOrder.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            self.number = f"PO-{last + 1:05d}"
        super().save(*args, **kwargs)

    @property
    def total(self):
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))


class PurchaseOrderLine(models.Model):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="+")
    work_package = models.ForeignKey("projects.WorkPackage", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    quantity = models.DecimalField(**QTY)
    unit_price = models.DecimalField("unit price (UGX)", **MONEY)

    def __str__(self):
        return f"{self.material.name} x {self.quantity}"

    @property
    def line_total(self):
        return (self.quantity or Decimal("0")) * (self.unit_price or Decimal("0"))

    @property
    def received_quantity(self):
        return (
            self.receipt_lines.filter(receipt__posted=True).aggregate(t=Sum("quantity_accepted"))["t"]
            or Decimal("0")
        )

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})
        if self.unit_price is not None and self.unit_price < 0:
            raise ValidationError({"unit_price": "Unit price cannot be negative."})


class GoodsReceipt(AuditedModel):
    """Delivery verified by the storekeeper. Posting it books stock and the material expense."""

    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    receipt_date = models.DateField(default=timezone.localdate)
    delivery_note_no = models.CharField(max_length=60, blank=True)
    inspected_by = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    posted = models.BooleanField(default=False, editable=False)
    posted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-receipt_date", "-id"]

    def __str__(self):
        return f"GRN-{self.pk or 'new'} ({self.po})"


class GoodsReceiptLine(models.Model):
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines")
    quantity_delivered = models.DecimalField(**QTY)
    quantity_accepted = models.DecimalField(**QTY)
    quantity_rejected = models.DecimalField(default=0, **QTY)
    condition = models.CharField(max_length=200, blank=True)

    def clean(self):
        d, a, r = self.quantity_delivered, self.quantity_accepted, self.quantity_rejected
        if None in (d, a, r):
            return
        if min(d, a, r) < 0:
            raise ValidationError("Quantities cannot be negative.")
        if a + r != d:
            raise ValidationError("Accepted + rejected must equal the quantity delivered.")


# ---------------------------------------------------------------- stores
class MaterialIssue(AuditedModel):
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="material_issues")
    issue_date = models.DateField(default=timezone.localdate)
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    work_package = models.ForeignKey("projects.WorkPackage", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    activity = models.CharField(max_length=200, blank=True, help_text="e.g. Column casting, ground floor")
    requested_by = models.CharField(max_length=120, blank=True)
    authorised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+",
    )
    notes = models.TextField(blank=True)
    posted = models.BooleanField(default=False, editable=False)
    posted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-issue_date", "-id"]

    def __str__(self):
        return f"ISS-{self.pk or 'new'} {self.issue_date:%d/%m/%Y}"


class MaterialIssueLine(models.Model):
    issue = models.ForeignKey(MaterialIssue, on_delete=models.CASCADE, related_name="lines")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="+")
    quantity_issued = models.DecimalField(**QTY)
    quantity_used = models.DecimalField(default=0, **QTY, help_text="Actually consumed (informational)")

    def clean(self):
        if self.quantity_issued is not None and self.quantity_issued <= 0:
            raise ValidationError({"quantity_issued": "Quantity must be greater than zero."})
        if None not in (self.quantity_used, self.quantity_issued) and self.quantity_used > self.quantity_issued:
            raise ValidationError({"quantity_used": "Used quantity cannot exceed the quantity issued."})


class MaterialReturn(AuditedModel):
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="material_returns")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="+")
    return_date = models.DateField(default=timezone.localdate)
    quantity = models.DecimalField(**QTY)
    reason = models.CharField(max_length=200, blank=True)
    posted = models.BooleanField(default=False, editable=False)
    posted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-return_date", "-id"]

    def __str__(self):
        return f"RET-{self.pk or 'new'}"

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})


class StockAdjustment(AuditedModel):
    """Physical stocktake. Posting books the difference between count and system balance."""

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="stock_adjustments")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="+")
    date = models.DateField(default=timezone.localdate)
    counted_quantity = models.DecimalField(**QTY)
    reason = models.CharField(max_length=300, blank=True, help_text="Explanation of any difference")
    system_quantity = models.DecimalField(null=True, editable=False, **QTY)
    difference = models.DecimalField(null=True, editable=False, **QTY)
    posted = models.BooleanField(default=False, editable=False)
    posted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"ADJ-{self.pk or 'new'} {self.material.name}"

    def clean(self):
        if self.counted_quantity is not None and self.counted_quantity < 0:
            raise ValidationError({"counted_quantity": "Counted quantity cannot be negative."})


class WastageRecord(AuditedModel):
    class Reason(models.TextChoices):
        DAMAGED = "damaged", "Damaged"
        EXPIRED = "expired", "Expired"
        STOLEN = "stolen", "Stolen / lost"
        MISUSED = "misused", "Improperly used"
        OTHER = "other", "Other"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="wastage_records")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="+")
    date = models.DateField(default=timezone.localdate)
    quantity = models.DecimalField(**QTY)
    reason = models.CharField(max_length=10, choices=Reason.choices, default=Reason.DAMAGED)
    location = models.CharField(max_length=150, blank=True)
    responsible = models.CharField("responsible department/contractor", max_length=150, blank=True)
    remarks = models.TextField(blank=True)
    value = models.DecimalField("value (UGX)", null=True, editable=False, **MONEY)
    posted = models.BooleanField(default=False, editable=False)
    posted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"WST-{self.pk or 'new'} {self.material.name}"

    def clean(self):
        if self.quantity is not None and self.quantity <= 0:
            raise ValidationError({"quantity": "Quantity must be greater than zero."})


# ---------------------------------------------------------------- ledger
class LedgerQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("Stock ledger entries cannot be deleted. Post an adjustment instead.")

    def update(self, **kwargs):
        raise ValidationError("Stock ledger entries cannot be changed. Post an adjustment instead.")


class StockLedger(models.Model):
    """Append-only record of every stock movement. Balances are always SUM(quantity);
    they are never stored or edited directly."""

    class Type(models.TextChoices):
        RECEIPT = "receipt", "Goods received"
        ISSUE = "issue", "Issued to site"
        RETURN = "return", "Returned to store"
        ADJUSTMENT = "adjustment", "Stocktake adjustment"
        WASTAGE = "wastage", "Wastage / loss"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="stock_ledger")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="ledger")
    entry_date = models.DateField()
    entry_type = models.CharField(max_length=12, choices=Type.choices)
    quantity = models.DecimalField("signed quantity", **QTY)  # + in, - out
    unit_cost = models.DecimalField(**MONEY)
    reference = models.CharField(max_length=60, blank=True)
    work_package = models.ForeignKey("projects.WorkPackage", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source_type = models.CharField(max_length=60, blank=True)
    source_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")

    objects = LedgerQuerySet.as_manager()

    class Meta:
        ordering = ["entry_date", "id"]
        indexes = [models.Index(fields=["project", "material"])]

    def __str__(self):
        return f"{self.entry_date} {self.entry_type} {self.material.code} {self.quantity:+}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Stock ledger entries cannot be changed. Post an adjustment instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stock ledger entries cannot be deleted. Post an adjustment instead.")

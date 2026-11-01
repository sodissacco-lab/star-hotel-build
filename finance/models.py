from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from core.models import AuditedModel


class Category(models.TextChoices):
    MATERIALS = "materials", "Construction materials"
    LABOUR = "labour", "Labour payments"
    EQUIPMENT = "equipment", "Equipment hire"
    TRANSPORT = "transport", "Transport"
    FUEL = "fuel", "Fuel"
    SUBCONTRACTOR = "subcontractor", "Subcontractor payments"
    PROFESSIONAL = "professional", "Professional consultancy fees"
    PERMITS = "permits", "Permits and statutory fees"
    UTILITIES = "utilities", "Utilities"
    SECURITY = "security", "Security"
    ADMIN = "admin", "Administrative costs"
    FURNISHING = "furnishing", "Furnishing and hotel equipment"
    CONTINGENCY = "contingency", "Contingencies"
    MISC = "misc", "Miscellaneous"


class Budget(AuditedModel):
    """Approved budget line for a project, by expenditure category (and optionally phase)."""

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="budget_lines")
    category = models.CharField(max_length=20, choices=Category.choices)
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    amount = models.DecimalField("approved amount (UGX)", max_digits=16, decimal_places=2)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["project", "category"]

    def __str__(self):
        return f"{self.project.code} {self.get_category_display()}: {self.amount:,.0f}"


class Expense(AuditedModel):
    """An approved obligation of the project. Only APPROVED expenses count as expenditure.

    Material receipts create approved expenses automatically (see inventory.services).
    Corrections are made by reversal entries, never by editing approved records.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE, related_name="expenses")
    phase = models.ForeignKey("projects.Phase", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    date = models.DateField(default=timezone.localdate)
    category = models.CharField(max_length=20, choices=Category.choices)
    description = models.CharField(max_length=300)
    amount = models.DecimalField("amount (UGX)", max_digits=16, decimal_places=2)
    supplier = models.ForeignKey("inventory.Supplier", null=True, blank=True, on_delete=models.SET_NULL, related_name="expenses")
    reference = models.CharField(max_length=60, blank=True, help_text="Invoice, receipt or delivery note number")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    source_type = models.CharField(max_length=60, blank=True, editable=False)
    source_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"{self.date:%d/%m/%Y} {self.description} – UGX {self.amount:,.0f}"

    @property
    def paid_amount(self):
        """Direct payments plus the parts of supplier payments the system applied to this expense."""
        direct = self.payments.filter(status=Payment.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        applied = self.allocations.aggregate(t=Sum("amount"))["t"] or Decimal("0")
        return direct + applied

    @property
    def outstanding(self):
        return self.amount - self.paid_amount


class Payment(AuditedModel):
    """Money paid out. Pay either an expense, or a supplier's purchase order (which covers advances,
    part payments and payments against a demand note; the system applies it to deliveries)."""

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer"
        CHEQUE = "cheque", "Cheque"
        MOBILE_MONEY = "mobile_money", "Mobile money"
        OTHER = "other", "Other approved method"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for approval (advance)"
        APPROVED = "approved", "Paid"
        REJECTED = "rejected", "Rejected"

    expense = models.ForeignKey(Expense, null=True, blank=True, on_delete=models.PROTECT, related_name="payments",
                                help_text="For expenses other than goods bought on a purchase order")
    purchase_order = models.ForeignKey("inventory.PurchaseOrder", null=True, blank=True, on_delete=models.PROTECT,
                                       related_name="payments", help_text="Pay a supplier: advance or payment for delivered goods")
    demand_note = models.ForeignKey("finance.DemandNote", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="payments", help_text="Optional: the supplier's demand this pays")
    date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField("amount (UGX)", max_digits=16, decimal_places=2)
    method = models.CharField(max_length=15, choices=Method.choices, default=Method.BANK)
    reference = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED, editable=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
                                    on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"Payment {self.amount:,.0f} on {self.date:%d/%m/%Y}"

    @property
    def payee_ref(self):
        if self.purchase_order_id:
            return f"{self.purchase_order.number} - {self.purchase_order.supplier.name}"
        return str(self.expense) if self.expense_id else ""

    def save(self, *args, **kwargs):
        adding = self._state.adding
        if not adding:
            return super().save(*args, **kwargs)
        from finance import supplier
        with transaction.atomic():
            if self.purchase_order_id:
                supplier.prepare_po_payment(self)  # locks the order, checks limits, decides advance or normal
            super().save(*args, **kwargs)
            if self.status == self.Status.APPROVED:
                supplier.finalise_payment(self)  # applies it to deliveries and issues the receipt

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "Payment must be greater than zero."})
        if not self.expense_id and not self.purchase_order_id:
            raise ValidationError("Choose what is being paid: an expense, or a supplier's purchase order.")
        if self.expense_id and self.purchase_order_id:
            raise ValidationError("Pay either an expense or a purchase order, not both.")
        if self.demand_note_id and not self.purchase_order_id:
            raise ValidationError({"demand_note": "A demand note is paid through its purchase order."})
        if self.purchase_order_id:
            from finance import supplier
            supplier.validate_po_payment(self)
            return
        if self.expense.status != Expense.Status.APPROVED:
            raise ValidationError("Payments can only be recorded against approved expenses.")
        if self.expense.source_type == "inventory.goodsreceipt":
            raise ValidationError("Goods bought on a purchase order are paid through the purchase order (choose the order, not the expense).")
        if self.amount is not None:
            direct = self.expense.payments.filter(status=self.Status.APPROVED).exclude(pk=self.pk) \
                .aggregate(t=Sum("amount"))["t"] or Decimal("0")
            already = direct + (self.expense.allocations.aggregate(t=Sum("amount"))["t"] or Decimal("0"))
            if already + self.amount > self.expense.amount:
                raise ValidationError(
                    f"Payment exceeds the outstanding balance of UGX {self.expense.amount - already:,.0f}."
                )


class PaymentAllocation(models.Model):
    """The part of a supplier payment applied to one delivery's expense. Rebuilt by the system
    whenever goods are received or a payment is approved; never entered by hand."""

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="allocations")
    amount = models.DecimalField(max_digits=16, decimal_places=2)


class DemandNote(AuditedModel):
    """A supplier's demand for payment (invoice, demand note or pro-forma) against a purchase order."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        VERIFIED = "verified", "Verified against deliveries"
        APPROVED = "approved", "Approved for payment"
        REJECTED = "rejected", "Rejected"

    number = models.CharField(max_length=20, unique=True, blank=True, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, editable=False, related_name="demand_notes")
    purchase_order = models.ForeignKey("inventory.PurchaseOrder", on_delete=models.PROTECT, related_name="demand_notes")
    supplier_ref = models.CharField("supplier's invoice / demand no.", max_length=60)
    date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField("amount demanded (UGX)", max_digits=16, decimal_places=2)
    description = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED, editable=False)
    is_advance_request = models.BooleanField(default=False, editable=False,
                                             help_text="Demands more than the value delivered and not yet demanded")
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return self.number or "New demand note"

    @property
    def supplier(self):
        return self.purchase_order.supplier

    def save(self, *args, **kwargs):
        self.project_id = self.purchase_order.project_id
        if not self.number:
            last = DemandNote.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            self.number = f"DN-{last + 1:05d}"
        super().save(*args, **kwargs)

    @property
    def paid(self):
        return self.payments.filter(status=Payment.Status.APPROVED).aggregate(t=Sum("amount"))["t"] or Decimal("0")

    @property
    def pending(self):
        return self.payments.filter(status=Payment.Status.PENDING).aggregate(t=Sum("amount"))["t"] or Decimal("0")

    @property
    def balance(self):
        return self.amount - self.paid

    @property
    def payment_state(self):
        if self.status != self.Status.APPROVED:
            return "-"
        if self.paid <= 0:
            return "Unpaid"
        return "Paid in full" if self.balance <= 0 else "Part paid"

    def clean(self):
        if self.amount is not None and self.amount <= 0:
            raise ValidationError({"amount": "The amount demanded must be greater than zero."})
        if self.purchase_order_id:
            from inventory.models import PurchaseOrder
            if self.purchase_order.status not in (PurchaseOrder.Status.APPROVED, PurchaseOrder.Status.PARTIAL,
                                                  PurchaseOrder.Status.RECEIVED):
                raise ValidationError({"purchase_order": "Demands can only be recorded against an approved purchase order."})
            if self.amount is not None:
                others = DemandNote.objects.filter(purchase_order=self.purchase_order).exclude(
                    status=self.Status.REJECTED).exclude(pk=self.pk).aggregate(t=Sum("amount"))["t"] or Decimal("0")
                if others + self.amount > self.purchase_order.total:
                    raise ValidationError(
                        f"Demands would total UGX {others + self.amount:,.0f}, above the order value of "
                        f"UGX {self.purchase_order.total:,.0f}.")


class Receipt(AuditedModel):
    """Numbered acknowledgement of a payment made by the project, to a supplier, contractor or worker.

    Receipts are issued by the system (never typed in), cannot be edited, and are cancelled
    (voided, with a reason) rather than deleted, so the numbering has no gaps.
    """

    class PayeeType(models.TextChoices):
        SUPPLIER = "supplier", "Supplier"
        CONTRACTOR = "contractor", "Contractor"
        WORKER = "worker", "Worker"
        OTHER = "other", "Other payee"

    number = models.CharField(max_length=20, unique=True, null=True, blank=True, editable=False)
    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="receipts")
    date = models.DateField(default=timezone.localdate)
    payee_type = models.CharField(max_length=12, choices=PayeeType.choices)
    payee_name = models.CharField(max_length=200)
    supplier = models.ForeignKey("inventory.Supplier", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    contractor = models.ForeignKey("workforce.Contractor", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    worker = models.ForeignKey("workforce.Worker", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="receipts")
    payroll = models.ForeignKey("workforce.Payroll", null=True, blank=True, on_delete=models.PROTECT, related_name="receipts")
    purchase_order = models.ForeignKey("inventory.PurchaseOrder", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    demand_note = models.ForeignKey(DemandNote, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    snapshot = models.JSONField(default=dict, blank=True, editable=False,
                                help_text="Items and account position as at the day the receipt was issued")
    amount = models.DecimalField("amount (UGX)", max_digits=16, decimal_places=2)
    method = models.CharField(max_length=15, choices=Payment.Method.choices, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    purpose = models.CharField("being payment for", max_length=300)

    voided = models.BooleanField(default=False, editable=False)
    void_reason = models.CharField(max_length=200, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
                                  on_delete=models.PROTECT, related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True, editable=False)

    FROZEN = ("project_id", "date", "payee_type", "payee_name", "payment_id", "payroll_id", "worker_id",
              "amount", "method", "reference", "purpose", "purchase_order_id", "demand_note_id", "snapshot")

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["payment"], condition=Q(payment__isnull=False, voided=False),
                                    name="one_live_receipt_per_payment"),
            models.UniqueConstraint(fields=["payroll", "worker"], condition=Q(payroll__isnull=False, voided=False),
                                    name="one_live_receipt_per_payroll_worker"),
        ]

    def __str__(self):
        return self.number or "New receipt"

    @property
    def amount_words(self):
        from finance.words import amount_in_words
        return amount_in_words(self.amount)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            before = Receipt.objects.get(pk=self.pk)
            for name in self.FROZEN:
                if getattr(before, name) != getattr(self, name):
                    raise ValidationError("An issued receipt cannot be changed; void it and issue a new one.")
        super().save(*args, **kwargs)
        if not self.number:
            self.number = f"RCT-{self.pk:05d}"
            Receipt.objects.filter(pk=self.pk).update(number=self.number)
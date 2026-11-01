import os

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from core.models import AuditedModel

MAX_BYTES = 8 * 1024 * 1024
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp"}


def validate_upload(f):
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in ALLOWED:
        raise ValidationError("Only photos (jpg, png, webp) and PDF files are allowed.")
    if f.size > MAX_BYTES:
        raise ValidationError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")


def upload_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    now = timezone.now()
    return f"attachments/p{instance.project_id}/{now:%Y/%m}/{now:%d%H%M%S%f}{ext}"


class Attachment(AuditedModel):
    class Kind(models.TextChoices):
        PHOTO = "photo", "Site photo"
        DELIVERY = "delivery_note", "Delivery note"
        RECEIPT = "receipt", "Receipt / invoice"
        DRAWING = "drawing", "Drawing / plan"
        CERTIFICATE = "certificate", "Certificate / permit"
        OTHER = "other", "Other"

    project = models.ForeignKey("projects.Project", on_delete=models.PROTECT, related_name="attachments")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.PHOTO)
    caption = models.CharField(max_length=200)
    file = models.FileField(upload_to=upload_path, validators=[validate_upload])
    original_name = models.CharField(max_length=200, blank=True, editable=False)
    diary = models.ForeignKey("projects.SiteDiary", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="attachments", help_text="Link to a site diary day (optional)")
    purchase_order = models.ForeignKey("inventory.PurchaseOrder", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="attachments", help_text="Link to a purchase order (optional)")

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.caption

    @property
    def extension(self):
        return os.path.splitext(self.file.name)[1].lower()

    @property
    def is_image(self):
        return self.extension in IMAGES

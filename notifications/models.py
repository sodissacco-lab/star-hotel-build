from django.conf import settings
from django.db import models


class Notification(models.Model):
    """Something waiting for a person (an approval, low stock, an advance not delivered).

    Rows are created and closed by notifications.services.refresh() from the live records, so a
    notification disappears from the bell by itself once the underlying matter is dealt with."""

    class Kind(models.TextChoices):
        APPROVAL = "approval", "Waiting for your decision"
        LOW_STOCK = "low_stock", "Low stock"
        ADVANCE = "advance", "Advance not delivered"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    ref = models.CharField(max_length=80, help_text="Identifies the matter, so it is only notified once")
    title = models.CharField(max_length=200)
    body = models.CharField(max_length=400, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["user", "ref"], name="one_notification_per_matter")]

    def __str__(self):
        return self.title

    @property
    def is_active(self):
        return self.resolved_at is None

from django.conf import settings
from django.db import models


class AuditedModel(models.Model):
    """Abstract base: who created/changed a record and when.

    Approved financial and stock records must never be silently altered; this
    base class gives every important record a creator and timestamps.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
        on_delete=models.PROTECT, related_name="+",
    )

    class Meta:
        abstract = True

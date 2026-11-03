from django.conf import settings
from django.db import models


class ClientRequest(models.Model):
    """Remembers the answer to a request sent with an Idempotency-Key header, so the phone app can
    safely resend queued entries after a poor connection without creating duplicates."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    key = models.CharField(max_length=80)
    status_code = models.PositiveSmallIntegerField()
    body = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="api_unique_client_key")]

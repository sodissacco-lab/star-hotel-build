import functools
import json

from django.db import IntegrityError, transaction
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from api.models import ClientRequest


def idempotent(func):
    """Decorate a POST handler. A request carrying the same Idempotency-Key header as an earlier
    successful one gets the earlier answer back and does nothing again."""

    @functools.wraps(func)
    def wrapper(self, request, *args, **kwargs):
        key = request.headers.get("Idempotency-Key", "").strip()[:80]
        if not key:
            return func(self, request, *args, **kwargs)
        previous = ClientRequest.objects.filter(user=request.user, key=key).first()
        if previous is not None:
            return Response(previous.body, status=previous.status_code)
        response = func(self, request, *args, **kwargs)
        if 200 <= response.status_code < 300:
            body = json.loads(JSONRenderer().render(response.data) or b"{}")
            try:
                with transaction.atomic():
                    ClientRequest.objects.create(user=request.user, key=key, status_code=response.status_code, body=body)
            except IntegrityError:
                pass
        return response

    return wrapper

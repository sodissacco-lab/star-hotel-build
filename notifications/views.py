from rest_framework.response import Response
from rest_framework.views import APIView

from notifications import services


def _json(n):
    return {"id": n.pk, "kind": n.kind, "title": n.title, "body": n.body, "read": n.read_at is not None,
            "created": n.created_at.isoformat()}


class NotificationListView(APIView):
    def get(self, request):
        services.refresh(request.user)
        items = list(services.active(request.user)[:60])
        return Response({"unread": sum(1 for n in items if n.read_at is None), "items": [_json(n) for n in items]})


class NotificationReadView(APIView):
    def post(self, request):
        ids = request.data.get("ids")
        if request.data.get("all") or ids is None:
            services.mark_read(request.user)
        else:
            services.mark_read(request.user, [int(i) for i in ids if str(i).isdigit()])
        return Response({"unread": services.unread_count(request.user)})
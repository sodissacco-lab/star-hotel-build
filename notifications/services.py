"""Work out what is waiting for each person, keep their notification list in step with it, and e-mail it."""
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.permissions import can
from notifications.models import Notification

K = Notification.Kind
APPROVAL_LABELS = {
    "requisition": "Requisition", "purchase_order": "Purchase order", "direct_delivery": "Delivery without an order",
    "certificate": "Payment certificate", "contractor_advance": "Mobilisation advance",
    "worker_advance": "Salary advance", "payroll": "Payroll",
}


def _money(value):
    try:
        return f"UGX {float(value):,.0f}"
    except (TypeError, ValueError):
        return ""


def _approvals(user):
    from api.field_views import _approval_items  # the same list the Approvals screen shows

    for item in _approval_items(user):
        label = APPROVAL_LABELS.get(item["kind"], item["kind"])
        amount = _money(item["amount"]) if item.get("amount") else ""
        body = " · ".join(x for x in (item["project"], item["detail"], amount, f"raised by {item['raised_by']}" if item.get("raised_by") else "") if x)
        yield {"kind": K.APPROVAL, "ref": f"approval:{item['kind']}:{item['id']}",
               "title": f"{label} {item['title']} is waiting for your decision", "body": body[:400]}


def _low_stock(user):
    if not (can(user, "receive_goods") or can(user, "create_po") or can(user, "approve_po")):
        return
    from inventory import services as stock

    for project in user.accessible_projects():
        rows, _ = stock.stock_summary(project)
        for r in rows:
            if r["below_reorder"]:
                m = r["material"]
                yield {"kind": K.LOW_STOCK, "ref": f"lowstock:{project.pk}:{m.pk}", "title": f"Low stock: {m.name}",
                       "body": f"{project.name}: {r['balance']:g} {m.unit.abbreviation} left (reorder level {m.reorder_level:g})."[:400]}


def _advances(user):
    if not can(user, "view_finance"):
        return
    from finance import supplier

    for project in user.accessible_projects():
        for a in supplier.advances_overview(project):
            if a["stale"]:
                yield {"kind": K.ADVANCE, "ref": f"advance:{a['po'].pk}", "title": f"Advance to {a['supplier'].name} not yet delivered",
                       "body": f"{a['po'].number}: {_money(a['advance'])} paid {a['days']} days ago and goods are still outstanding."[:400]}


def refresh(user):
    """Bring the user's notifications in step with what is waiting right now.

    New matters get a notification; matters that have been dealt with are closed (and leave the bell);
    a matter that comes back is opened again. A notification the person marked read stays read."""
    current = {}
    for generate in (_approvals, _low_stock, _advances):
        for alert in generate(user):
            current[alert["ref"]] = alert
    now = timezone.now()
    with transaction.atomic():
        existing = {n.ref: n for n in Notification.objects.filter(user=user)}
        for ref, alert in current.items():
            note = existing.get(ref)
            if note is None:
                Notification.objects.get_or_create(user=user, ref=ref, defaults={
                    "kind": alert["kind"], "title": alert["title"], "body": alert["body"]})
            elif note.resolved_at is not None:  # it has come back
                note.title, note.body = alert["title"], alert["body"]
                note.resolved_at = note.read_at = note.emailed_at = None
                note.created_at = now
                note.save()
            elif (note.title, note.body) != (alert["title"], alert["body"]):
                note.title, note.body = alert["title"], alert["body"]
                note.save(update_fields=["title", "body"])
        for ref, note in existing.items():
            if ref not in current and note.resolved_at is None:
                note.resolved_at = now
                note.save(update_fields=["resolved_at"])
        Notification.objects.filter(user=user, resolved_at__lt=now - timedelta(days=30)).delete()


def active(user):
    return Notification.objects.filter(user=user, resolved_at__isnull=True)


def unread_count(user):
    return active(user).filter(read_at__isnull=True).count()


def mark_read(user, ids=None):
    qs = active(user).filter(read_at__isnull=True)
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return qs.update(read_at=timezone.now())


def email_digests():
    """E-mail each person the notifications they have not been e-mailed about yet. Returns the number of e-mails sent."""
    sent = 0
    users = User.objects.filter(is_active=True).exclude(role=User.Role.CONTRACTOR).exclude(email="")
    for user in users:
        refresh(user)
        pending = list(active(user).filter(read_at__isnull=True, emailed_at__isnull=True).order_by("created_at"))
        if not pending:
            continue
        lines = [f"- {n.title}" + (f"\n  {n.body}" if n.body else "") for n in pending]
        message = (f"Hello {user.first_name or user.username},\n\n{len(pending)} item(s) need your attention in StarBuild:\n\n"
                   + "\n".join(lines) + f"\n\nOpen the StarBuild app, or {settings.SITE_URL}\n")
        try:
            send_mail(f"StarBuild: {len(pending)} item(s) need your attention", message, None, [user.email], fail_silently=False)
        except Exception:  # a mail problem must not stop the other people's e-mails; they are retried next time
            continue
        Notification.objects.filter(pk__in=[n.pk for n in pending]).update(emailed_at=timezone.now())
        sent += 1
    return sent

"""E-mail people about approvals, low stock and undelivered advances that are waiting for them.

    python manage.py send_alerts          (run daily by the Render cron job)
"""
from django.core.management.base import BaseCommand

from notifications.services import email_digests


class Command(BaseCommand):
    help = "E-mail each person the new items waiting for them."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS(f"{email_digests()} e-mail(s) sent."))

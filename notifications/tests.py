import gzip
import json
import tempfile
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings

from accounts.models import User
from api.tests import APITestBase
from notifications import services
from notifications.models import Notification

TMP = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=TMP, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NotificationTests(APITestBase):
    def test_api_lists_and_marks_read(self):
        client = self.login("pm")
        data = client.get("/api/v1/notifications/").json()
        self.assertEqual(data["unread"], len(data["items"]))
        client.post("/api/v1/notifications/read/", {"all": True}, format="json")
        self.assertEqual(client.get("/api/v1/notifications/").json()["unread"], 0)

    def test_refresh_is_idempotent(self):
        pm = User.objects.get(username="pm")
        services.refresh(pm)
        first = Notification.objects.filter(user=pm).count()
        services.refresh(pm)
        self.assertEqual(Notification.objects.filter(user=pm).count(), first)

    def test_cleared_matters_leave_the_bell(self):
        pm = User.objects.get(username="pm")
        Notification.objects.create(user=pm, kind="approval", ref="approval:requisition:9999", title="Gone")
        services.refresh(pm)  # nothing real matches this ref any more
        self.assertIsNotNone(Notification.objects.get(ref="approval:requisition:9999").resolved_at)
        self.assertNotIn("Gone", [n.title for n in services.active(pm)])

    def test_digest_email_sent_once(self):
        store = User.objects.get(username="store")
        store.email = "store@example.com"
        store.save()
        Notification.objects.create(user=store, kind="low_stock", ref="lowstock:0:0", title="Low stock: Cement", body="2 bags left")
        # the made-up item is closed by refresh (no such stock), so use a real approval for the PM instead
        pm = User.objects.get(username="pm")
        pm.email = "pm@example.com"
        pm.save()
        Notification.objects.create(user=pm, kind="approval", ref="approval:requisition:1", title="Req", body="")
        services.email_digests()
        self.assertEqual(Notification.objects.filter(emailed_at__isnull=False).count(), len(mail.outbox))
        before = len(mail.outbox)
        services.email_digests()
        self.assertEqual(len(mail.outbox), before)  # nothing new, nothing sent again

    def test_backup_command_writes_and_prunes(self):
        for stamp in ("20200101-0000", "20200102-0000", "20200103-0000"):
            default_storage.save(f"backups/starbuild-{stamp}.json.gz", ContentFile(gzip.compress(b"[]")))
        call_command("backup_data", "--keep", "2", stdout=StringIO())
        files = sorted(f for f in default_storage.listdir("backups")[1] if f.endswith(".json.gz"))
        self.assertEqual(len(files), 2)
        self.assertNotIn("starbuild-20200101-0000.json.gz", files)
        with default_storage.open("backups/" + files[-1], "rb") as fh:
            data = json.loads(gzip.decompress(fh.read()))
        self.assertTrue(any(row["model"] == "projects.project" for row in data))

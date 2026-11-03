"""Save a compressed copy of all StarBuild data, and delete the oldest copies.

    python manage.py backup_data          (run nightly by the Render cron job)

Copies are written to the file storage (the R2 bucket when it is configured) under backups/.
To restore into an empty database:   python manage.py migrate   then   python manage.py loaddata <file>.json.gz
Login tokens and session data are not included (people simply sign in again).
"""
import gzip
import io
import re

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

PATTERN = re.compile(r"^starbuild-\d{8}-\d{4}\.json\.gz$")
EXCLUDE = ["contenttypes", "auth.permission", "sessions", "admin.logentry", "authtoken.token", "api.clientrequest"]


class Command(BaseCommand):
    help = "Save a compressed copy of all data to the file storage and prune old copies."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=settings.BACKUP_KEEP, help="how many copies to keep")

    def handle(self, *args, **options):
        if not settings.R2_BUCKET and not settings.DEBUG:
            self.stderr.write("Warning: R2_BUCKET is not set, so this copy is saved on this machine only.")
        out = io.StringIO()
        call_command("dumpdata", exclude=EXCLUDE, use_natural_foreign_keys=True, indent=None, stdout=out)
        payload = gzip.compress(out.getvalue().encode("utf-8"))
        name = f"backups/starbuild-{timezone.localtime():%Y%m%d-%H%M}.json.gz"
        saved = default_storage.save(name, ContentFile(payload))
        self.stdout.write(self.style.SUCCESS(f"Saved {saved} ({len(payload) / 1024:,.0f} KB)."))

        try:
            files = sorted(f for f in default_storage.listdir("backups")[1] if PATTERN.match(f))
        except (FileNotFoundError, OSError) as exc:
            raise CommandError(f"Could not list the backups: {exc}")
        keep = max(options["keep"], 1)
        for old in files[:-keep]:
            default_storage.delete(f"backups/{old}")
            self.stdout.write(f"Deleted old copy {old}.")

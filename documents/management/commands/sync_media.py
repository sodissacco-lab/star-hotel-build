"""Copy photos and documents saved on the server's disk into the cloud bucket.

Run once, in the Render Shell of the web service, after R2_BUCKET has been set:
    python manage.py sync_media
It is safe to run again: files already in the bucket are skipped.
"""
from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError

from documents.models import Attachment


class Command(BaseCommand):
    help = "Copy attachments from the local disk into the configured cloud file storage."

    def handle(self, *args, **options):
        if not settings.R2_BUCKET:
            raise CommandError("R2_BUCKET is not set, so files are already stored on the local disk.")
        local = FileSystemStorage(location=settings.MEDIA_ROOT)
        copied = skipped = missing = 0
        for att in Attachment.objects.exclude(file=""):
            name = att.file.name
            if default_storage.exists(name):
                skipped += 1
                continue
            if not local.exists(name):
                missing += 1
                self.stderr.write(f"Not found on this disk: {name} (attachment {att.pk}, {att.caption})")
                continue
            with local.open(name) as handle:
                default_storage.save(name, File(handle))
            copied += 1
        self.stdout.write(self.style.SUCCESS(f"Copied {copied}, already in the bucket {skipped}, not found {missing}."))

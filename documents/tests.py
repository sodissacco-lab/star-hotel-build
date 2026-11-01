import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User
from documents import models as doc_models
from documents.models import Attachment
from projects.models import Project, SiteDiary

R = User.Role
PW = "pass-" + "x" * 10
TMP = tempfile.mkdtemp()
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


@override_settings(MEDIA_ROOT=TMP)
class DocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password=PW, role=role)
        cls.store = mk("store", R.STOREKEEPER)
        cls.audit = mk("audit", R.AUDITOR)
        cls.outsider = mk("outsider", R.STOREKEEPER)
        cls.project = Project.objects.create(name="Hotel", code="D-1")
        cls.project.members.add(cls.store, cls.audit)
        cls.diary = SiteDiary.objects.create(project=cls.project, date="2026-11-01")

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP, ignore_errors=True)

    def url(self, name, *args):
        return f"{reverse(name, args=args)}?project={self.project.pk}"

    def upload(self, name="site.png", data=PNG, **extra):
        body = {"kind": "photo", "caption": "Foundation trench", "file": SimpleUploadedFile(name, data)}
        body.update(extra)
        return self.client.post(self.url("doc_upload"), body)

    def test_upload_and_view(self):
        self.client.login(username="store", password=PW)
        res = self.upload(diary=self.diary.pk)
        self.assertEqual(res.status_code, 302, res.content[:400])
        att = Attachment.objects.get()
        self.assertEqual((att.created_by, att.project, att.diary), (self.store, self.project, self.diary))
        self.assertTrue(att.is_image)
        got = self.client.get(reverse("doc_serve", args=[att.pk]))
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got["Content-Type"], "image/png")
        self.assertIn("Foundation trench", self.client.get(self.url("doc_gallery")).content.decode())

    def test_bad_extension_rejected(self):
        self.client.login(username="store", password=PW)
        self.assertEqual(self.upload("virus.exe", b"MZ").status_code, 200)
        self.assertEqual(self.upload("page.html", b"<script>").status_code, 200)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_oversize_rejected(self):
        self.client.login(username="store", password=PW)
        with mock.patch.object(doc_models, "MAX_BYTES", 10):
            self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_auditor_can_view_but_not_upload(self):
        self.client.login(username="store", password=PW)
        self.upload()
        att = Attachment.objects.get()
        self.client.logout()
        self.client.login(username="audit", password=PW)
        self.assertEqual(self.client.get(reverse("doc_serve", args=[att.pk])).status_code, 200)
        self.assertEqual(self.client.get(self.url("doc_upload")).status_code, 403)

    def test_outsider_cannot_open_file(self):
        self.client.login(username="store", password=PW)
        self.upload()
        att = Attachment.objects.get()
        self.client.logout()
        self.client.login(username="outsider", password=PW)
        self.assertEqual(self.client.get(reverse("doc_serve", args=[att.pk])).status_code, 404)

    def test_login_required(self):
        self.assertEqual(self.client.get(reverse("doc_gallery")).status_code, 302)

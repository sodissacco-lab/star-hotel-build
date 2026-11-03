import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from api.models import ClientRequest
from api.tests import APITestBase
from documents.models import Attachment
from inventory.models import MaterialIssue, PurchaseOrder, PurchaseOrderLine, Supplier
from procurement.models import Requisition, RequisitionLine
from workforce.models import Contract, Contractor, PaymentCertificate

D = Decimal
TMP = tempfile.mkdtemp()
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


class FieldBase(APITestBase):
    def make_po(self, qty="100"):
        supplier = Supplier.objects.create(name="Acme")
        po = PurchaseOrder.objects.create(project=self.project, supplier=supplier, status="approved",
                                          created_by=self.eng)
        line = PurchaseOrderLine.objects.create(po=po, material=self.cement, quantity=D(qty), unit_price=D("38000"))
        return po, line

    def receive(self, client, po, line, delivered="100", accepted=None, **headers):
        body = {"date": "2026-11-02", "delivery_note": "DN-1",
                "lines": [{"po_line": line.pk, "delivered": delivered, **({"accepted": accepted} if accepted else {})}]}
        return client.post(f"/api/v1/projects/{self.project.pk}/purchase-orders/{po.pk}/receive/", body, format="json",
                           **headers)


class StoreTests(FieldBase):
    def test_receive_then_issue_and_stock(self):
        po, line = self.make_po()
        c = self.login("store")
        listing = c.get(f"/api/v1/projects/{self.project.pk}/receivable-orders/").json()
        self.assertEqual(listing[0]["lines"][0]["outstanding"], "100.000")
        self.assertEqual(self.receive(c, po, line, "60", "58").status_code, 201)
        res = c.post(f"/api/v1/projects/{self.project.pk}/issues/",
                     {"date": "2026-11-02", "activity": "Blinding", "lines": [{"material": self.cement.pk, "quantity": "20"}]},
                     format="json")
        self.assertEqual(res.status_code, 201, res.content)
        stock = c.get(f"/api/v1/projects/{self.project.pk}/stock/").json()
        self.assertEqual(D(stock["materials"][0]["balance"]), D("38"))

    def test_cannot_issue_more_than_stock(self):
        res = self.login("store").post(f"/api/v1/projects/{self.project.pk}/issues/",
                                       {"lines": [{"material": self.cement.pk, "quantity": "5"}]}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(MaterialIssue.objects.count(), 0)

    def test_over_receive_rejected(self):
        po, line = self.make_po("10")
        self.assertEqual(self.receive(self.login("store"), po, line, "11").status_code, 400)

    def test_roles(self):
        po, line = self.make_po()
        eng = self.login("eng")
        self.assertEqual(eng.post(f"/api/v1/projects/{self.project.pk}/issues/",
                                  {"lines": [{"material": self.cement.pk, "quantity": "1"}]}, format="json").status_code, 403)
        self.assertEqual(self.receive(eng, po, line).status_code, 403)
        self.assertEqual(self.receive(self.login("outsider"), po, line).status_code, 403)  # role not allowed

    def test_idempotency_key_prevents_duplicates(self):
        po, line = self.make_po()
        c = self.login("store")
        self.receive(c, po, line, "50")
        body = {"lines": [{"material": self.cement.pk, "quantity": "10"}]}
        url = f"/api/v1/projects/{self.project.pk}/issues/"
        first = c.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="abc-1")
        again = c.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="abc-1")
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(first.json(), again.json())
        self.assertEqual(MaterialIssue.objects.count(), 1)
        self.assertEqual(ClientRequest.objects.count(), 1)
        c.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="abc-2")
        self.assertEqual(MaterialIssue.objects.count(), 2)

    def test_failed_request_is_not_remembered(self):
        c = self.login("store")
        url = f"/api/v1/projects/{self.project.pk}/issues/"
        body = {"lines": [{"material": self.cement.pk, "quantity": "5"}]}
        self.assertEqual(c.post(url, body, format="json", HTTP_IDEMPOTENCY_KEY="k1").status_code, 400)
        self.assertEqual(ClientRequest.objects.count(), 0)


@override_settings(MEDIA_ROOT=TMP)
class FileTests(FieldBase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP, ignore_errors=True)

    def upload(self, client, name="site.png", data=PNG, **extra):
        return client.post(f"/api/v1/projects/{self.project.pk}/files/",
                           {"kind": "photo", "caption": "Trench", "file": SimpleUploadedFile(name, data), **extra},
                           format="multipart")

    def test_upload_list_download(self):
        c = self.login("store")
        res = self.upload(c)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Attachment.objects.get().created_by, self.store)
        listing = c.get(f"/api/v1/projects/{self.project.pk}/files/").json()
        self.assertEqual(listing[0]["caption"], "Trench")
        got = c.get(f"/api/v1/files/{listing[0]['id']}/download/")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(b"".join(got.streaming_content), PNG)

    def test_bad_type_rejected(self):
        res = self.upload(self.login("store"), "bad.exe", b"MZ")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_outsider_blocked(self):
        self.upload(self.login("store"))
        att = Attachment.objects.get()
        out = self.login("outsider")
        self.assertEqual(out.get(f"/api/v1/files/{att.pk}/download/").status_code, 404)
        self.assertEqual(out.get(f"/api/v1/projects/{self.project.pk}/files/").status_code, 404)


class ApprovalTests(FieldBase):
    def test_list_and_approve_requisition(self):
        req = Requisition.objects.create(project=self.project, emergency=True, justification="Urgent pour",
                                         created_by=self.eng)
        RequisitionLine.objects.create(requisition=req, material=self.cement, quantity=D("10"))
        req.status = "submitted"
        req.save()
        self.assertEqual(self.login("eng").get("/api/v1/approvals/").json(), [])
        pm = self.login("pm")
        items = pm.get("/api/v1/approvals/").json()
        self.assertEqual((items[0]["kind"], items[0]["actions"]), ("requisition", ["approve", "reject"]))
        res = pm.post(f"/api/v1/approvals/requisition/{req.pk}/approve/")
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")
        self.assertEqual(pm.get("/api/v1/approvals/").json(), [])

    def test_certificate_approval_and_wrong_role(self):
        contract = Contract.objects.create(project=self.project, contractor=Contractor.objects.create(name="BuildCo"),
                                           reference="C-1", scope="x", amount=D("10000000"),
                                           retention_percent=D("5"), status="active")
        cert = PaymentCertificate.objects.create(contract=contract, description="Slab", gross_amount=D("1000000"),
                                                 created_by=self.eng)
        items = self.login("pm").get("/api/v1/approvals/").json()
        self.assertEqual([i["kind"] for i in items], ["certificate"])
        res = self.login("store").post(f"/api/v1/approvals/certificate/{cert.pk}/approve/")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.login("pm").post(f"/api/v1/approvals/certificate/{cert.pk}/approve/").status_code, 200)
        cert.refresh_from_db()
        self.assertEqual(cert.status, "approved")

    def test_unknown_kind_and_other_project(self):
        self.assertEqual(self.login("pm").post("/api/v1/approvals/nothing/1/approve/").status_code, 404)
        req = Requisition.objects.create(project=self.other, emergency=True, justification="x", status="submitted")
        self.assertEqual(self.login("pm").post(f"/api/v1/approvals/requisition/{req.pk}/approve/").status_code, 404)
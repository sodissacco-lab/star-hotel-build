import json
import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from accounts.models import User
from api.tests import APITestBase
from documents.models import Attachment
from finance.models import Expense, Payment
from finance.supplier import po_account
from inventory.models import PurchaseOrder, Supplier
from procurement.models import DirectDelivery

D = Decimal
TMP = tempfile.mkdtemp()
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


@override_settings(MEDIA_ROOT=TMP, PO_APPROVAL_THRESHOLD_UGX=D("5000000"))
class DirectDeliveryTests(APITestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = User.objects.create_user("owner", password="pass-" + "x" * 10, role=User.Role.OWNER)
        cls.project.members.add(cls.owner)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP, ignore_errors=True)

    @property
    def url(self):
        return f"/api/v1/projects/{self.project.pk}/direct-deliveries/"

    def body(self, **over):
        data = {
            "date": "2026-11-02", "supplier_name": "Walk-in Hardware", "reason": "Cash buy, order not raised in time",
            "invoice_no": "INV-100", "delivery_note": "DN-9", "vehicle_reg": "UBA 123X", "driver_name": "Opio",
            "lines": [{"material": self.cement.pk, "delivered": "100", "accepted": "98", "unit_price": "38000",
                       "condition": "2 bags torn"}],
        }
        data.update(over)
        return data

    def record(self, who="store", **over):
        return self.login(who).post(self.url, self.body(**over), format="json")

    def act(self, who, pk, action="approve"):
        return self.login(who).post(f"/api/v1/approvals/direct_delivery/{pk}/{action}/", {}, format="json")

    def balance(self, client):
        rows = client.get(f"/api/v1/projects/{self.project.pk}/stock/").json()["materials"]
        return D(rows[0]["balance"]) if rows else D("0")

    # ------------------------------------------------------------ recording
    def test_recording_books_nothing_until_approved(self):
        store = self.login("store")
        res = store.post(self.url, self.body(), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["number"], "DD-00001")
        self.assertEqual(D(data["total"]), D("3724000"))  # 98 accepted x 38,000
        self.assertEqual(self.balance(store), D("0"))
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(Expense.objects.count(), 0)

    def test_new_supplier_is_created_once(self):
        self.assertEqual(self.record(invoice_no="A1").status_code, 201)
        self.assertEqual(self.record(invoice_no="A2", supplier_name="  walk-in   hardware ").status_code, 201)
        self.assertEqual(Supplier.objects.count(), 1)

    def test_listing_shows_recorded_deliveries(self):
        self.record()
        rows = self.login("store").get(self.url).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lines"][0]["rejected"], "2.000")

    def test_duplicate_invoice_is_refused(self):
        self.assertEqual(self.record().status_code, 201)
        res = self.record()
        self.assertEqual(res.status_code, 400)
        self.assertIn("already recorded", res.json()["detail"])
        self.assertEqual(DirectDelivery.objects.count(), 1)

    def test_validation_rules(self):
        self.assertEqual(self.record(reason="").status_code, 400)
        self.assertEqual(self.record(supplier_name="").status_code, 400)
        self.assertEqual(self.record(lines=[]).status_code, 400)
        bad = {"material": self.cement.pk, "delivered": "10", "accepted": "11", "unit_price": "100"}
        self.assertEqual(self.record(lines=[bad]).status_code, 400)  # accepted more than delivered
        free = {"material": self.cement.pk, "delivered": "10", "unit_price": "0"}
        self.assertEqual(self.record(lines=[free]).status_code, 400)  # price required
        self.assertEqual(self.record(amount_paid="9999999").status_code, 400)  # more than the goods are worth
        self.assertEqual(DirectDelivery.objects.count(), 0)
        self.assertEqual(Supplier.objects.count(), 0)  # a refused record leaves no new supplier behind

    def test_roles_and_project_access(self):
        self.assertEqual(self.record("eng").status_code, 403)
        self.assertEqual(self.record("outsider").status_code, 403)
        other = f"/api/v1/projects/{self.other.pk}/direct-deliveries/"
        self.assertEqual(self.login("store").post(other, self.body(), format="json").status_code, 404)

    def test_idempotency_key_prevents_duplicates(self):
        store = self.login("store")
        first = store.post(self.url, self.body(), format="json", HTTP_IDEMPOTENCY_KEY="dd-1")
        again = store.post(self.url, self.body(), format="json", HTTP_IDEMPOTENCY_KEY="dd-1")
        self.assertEqual((first.status_code, again.status_code), (201, 201))
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertEqual(DirectDelivery.objects.count(), 1)

    # ------------------------------------------------------------ approval
    def test_approval_books_stock_expense_and_supplier_account(self):
        dd_id = self.record(transport_charges="50000").json()["id"]
        res = self.act("pm", dd_id)
        self.assertEqual(res.status_code, 200, res.content)
        dd = DirectDelivery.objects.get(pk=dd_id)
        self.assertEqual(dd.status, "approved")
        self.assertEqual(self.balance(self.login("store")), D("98"))  # the 2 torn bags are not stock
        po = dd.purchase_order
        self.assertEqual(po.status, "received")
        self.assertEqual(po.requisition_ref, "DD-00001")
        self.assertEqual(po_account(po)["delivered"], D("3724000"))
        materials = Expense.objects.get(category="materials")
        self.assertEqual(materials.amount, D("3724000"))
        self.assertEqual(materials.status, "approved")
        self.assertEqual(Expense.objects.get(category="transport").amount, D("50000"))

    def test_payment_on_the_spot_is_applied_to_the_delivery(self):
        dd_id = self.record(amount_paid="2000000", payment_method="mobile_money", payment_reference="MM-77").json()["id"]
        self.assertEqual(self.act("pm", dd_id).status_code, 200)
        po = DirectDelivery.objects.get(pk=dd_id).purchase_order
        payment = Payment.objects.get(purchase_order=po)
        self.assertEqual((payment.status, payment.method), ("approved", "mobile_money"))
        acct = po_account(po)
        self.assertEqual(acct["paid"], D("2000000"))
        self.assertEqual(acct["balance_due"], D("1724000"))

    def test_you_cannot_approve_your_own_delivery(self):
        dd_id = self.record("pm").json()["id"]
        res = self.act("pm", dd_id)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Segregation", res.json()["detail"])
        self.assertEqual(DirectDelivery.objects.get(pk=dd_id).status, "pending")

    def test_storekeeper_cannot_approve(self):
        dd_id = self.record().json()["id"]
        self.assertEqual(self.act("store", dd_id).status_code, 400)
        self.assertEqual(DirectDelivery.objects.get(pk=dd_id).status, "pending")

    def test_large_deliveries_need_the_owner(self):
        big = [{"material": self.cement.pk, "delivered": "200", "unit_price": "38000"}]  # UGX 7.6m
        dd_id = self.record(lines=big).json()["id"]
        self.assertEqual(self.act("pm", dd_id).status_code, 400)
        self.assertEqual(DirectDelivery.objects.get(pk=dd_id).status, "pending")
        self.assertEqual(self.act("owner", dd_id).status_code, 200)
        self.assertEqual(self.balance(self.login("store")), D("200"))

    def test_nothing_accepted_cannot_be_approved(self):
        none_ok = [{"material": self.cement.pk, "delivered": "10", "accepted": "0", "unit_price": "38000"}]
        dd_id = self.record(lines=none_ok).json()["id"]
        res = self.act("pm", dd_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_approving_twice_is_refused(self):
        dd_id = self.record().json()["id"]
        self.assertEqual(self.act("pm", dd_id).status_code, 200)
        self.assertEqual(self.act("owner", dd_id).status_code, 400)
        self.assertEqual(self.balance(self.login("store")), D("98"))

    def test_rejection_books_nothing_and_frees_the_invoice_number(self):
        dd_id = self.record().json()["id"]
        self.assertEqual(self.act("pm", dd_id, "reject").status_code, 200)
        self.assertEqual(DirectDelivery.objects.get(pk=dd_id).status, "rejected")
        self.assertEqual(self.balance(self.login("store")), D("0"))
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(self.record().status_code, 201)  # same invoice may be recorded again

    # ------------------------------------------------------------ photo, approvals list, prices
    def test_delivery_note_photo_travels_with_the_record(self):
        store = self.login("store")
        res = store.post(self.url, {"payload": json.dumps(self.body()),
                                    "file": SimpleUploadedFile("dn.png", PNG, content_type="image/png")}, format="multipart")
        self.assertEqual(res.status_code, 201, res.content)
        dd_id = res.json()["id"]
        att = Attachment.objects.get(direct_delivery_id=dd_id)
        self.assertEqual(att.kind, "delivery_note")
        self.assertIsNone(att.purchase_order_id)
        self.assertEqual(self.act("pm", dd_id).status_code, 200)
        att.refresh_from_db()
        self.assertEqual(att.purchase_order_id, DirectDelivery.objects.get(pk=dd_id).purchase_order_id)

    def test_a_bad_photo_cancels_the_whole_record(self):
        res = self.login("store").post(self.url, {"payload": json.dumps(self.body()),
                                                  "file": SimpleUploadedFile("dn.exe", b"nope")}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(DirectDelivery.objects.count(), 0)

    def test_pending_deliveries_appear_in_approvals_for_approvers_only(self):
        dd_id = self.record().json()["id"]
        pm_items = self.login("pm").get("/api/v1/approvals/").json()
        mine = [i for i in pm_items if i["kind"] == "direct_delivery"]
        self.assertEqual([i["id"] for i in mine], [dd_id])
        self.assertEqual(mine[0]["actions"], ["approve", "reject"])
        self.assertEqual([i for i in self.login("store").get("/api/v1/approvals/").json() if i["kind"] == "direct_delivery"], [])

    def test_last_price_and_supplier_list(self):
        dd_id = self.record().json()["id"]
        self.assertEqual(self.login("store").get("/api/v1/last-prices/").json(), {})
        self.act("pm", dd_id)
        prices = self.login("store").get("/api/v1/last-prices/").json()
        self.assertEqual(D(prices[str(self.cement.pk)]), D("38000"))
        names = [s["name"] for s in self.login("store").get("/api/v1/suppliers/").json()]
        self.assertEqual(names, ["Walk-in Hardware"])

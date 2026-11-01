from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from inventory import services as stock
from inventory.models import (
    GoodsReceipt, Material, MaterialCategory, MaterialIssue, PurchaseOrder, PurchaseOrderLine, Supplier, Unit,
)
from procurement.models import Requisition
from projects.models import Phase, Project, SiteDiary
from workforce.models import Attendance, Worker

R = User.Role
D = Decimal
PW = "pass-" + "x" * 10


class FieldworkBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password=PW, role=role)
        cls.eng = mk("eng", R.SITE_ENGINEER)
        cls.store = mk("store", R.STOREKEEPER)
        cls.acct = mk("acct", R.ACCOUNTANT)
        cls.outsider = mk("outsider", R.SITE_ENGINEER)
        cls.project = Project.objects.create(name="Hotel", code="FW-1")
        cls.other = Project.objects.create(name="Other", code="FW-2")
        for u in (cls.eng, cls.store, cls.acct):
            cls.project.members.add(u)
        cls.phase = Phase.objects.create(project=cls.project, name="Foundation")
        unit = Unit.objects.create(name="Bag", abbreviation="bag")
        cat = MaterialCategory.objects.create(name="Cement")
        cls.cement = Material.objects.create(code="C1", name="Cement", category=cat, unit=unit, standard_price=D("38000"))
        cls.worker = Worker.objects.create(project=cls.project, full_name="Okello", daily_wage=D("20000"))
        cls.worker2 = Worker.objects.create(project=cls.project, full_name="Akello", daily_wage=D("20000"))
        cls.supplier = Supplier.objects.create(name="Acme")

    def as_user(self, name):
        self.client.login(username=name, password=PW)

    def url(self, name, *args):
        return f"{reverse(name, args=args)}?project={self.project.pk}"

    def make_po(self, qty="100"):
        po = PurchaseOrder.objects.create(project=self.project, supplier=self.supplier, status="approved",
                                          created_by=self.eng)
        line = PurchaseOrderLine.objects.create(po=po, material=self.cement, quantity=D(qty), unit_price=D("38000"))
        return po, line


class AccessTests(FieldworkBase):
    def test_login_required(self):
        self.assertEqual(self.client.get(reverse("fw_home")).status_code, 302)

    def test_home_shows_only_allowed_tiles(self):
        self.as_user("store")
        html = self.client.get(self.url("fw_home")).content.decode()
        self.assertIn("Issue materials", html)
        self.assertNotIn("Site diary", html)

    def test_forbidden_without_capability(self):
        self.as_user("acct")
        for name in ("fw_diary", "fw_attendance", "fw_issue", "fw_receive_pick"):
            self.assertEqual(self.client.get(self.url(name)).status_code, 403, name)

    def test_outsider_has_no_project(self):
        self.as_user("outsider")
        self.assertEqual(self.client.get(reverse("fw_diary")).status_code, 404)


class DiaryTests(FieldworkBase):
    def test_save_diary_with_activity(self):
        self.as_user("eng")
        res = self.client.post(self.url("fw_diary"), {
            "date": "2026-11-01", "weather": "sunny", "workers_present": 10,
            "act-TOTAL_FORMS": 5, "act-INITIAL_FORMS": 0, "act-MIN_NUM_FORMS": 0, "act-MAX_NUM_FORMS": 1000,
            "act-0-description": "Pouring blinding", "act-0-phase": self.phase.pk,
        })
        self.assertEqual(res.status_code, 302, res.content[:500])
        diary = SiteDiary.objects.get()
        self.assertEqual(diary.supervisor, self.eng)
        self.assertEqual(diary.activities.count(), 1)

    def test_duplicate_date_rejected(self):
        SiteDiary.objects.create(project=self.project, date="2026-11-01")
        self.as_user("eng")
        res = self.client.post(self.url("fw_diary"), {
            "date": "2026-11-01", "weather": "sunny", "workers_present": 1,
            "act-TOTAL_FORMS": 5, "act-INITIAL_FORMS": 0, "act-MIN_NUM_FORMS": 0, "act-MAX_NUM_FORMS": 1000,
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(SiteDiary.objects.count(), 1)


class AttendanceTests(FieldworkBase):
    def test_record_and_correct(self):
        self.as_user("eng")
        data = {"date": "2026-11-01", f"w{self.worker.pk}_status": "present", f"w{self.worker.pk}_hours": "8",
                f"w{self.worker.pk}_ot": "0", f"w{self.worker2.pk}_status": "absent"}
        self.assertEqual(self.client.post(self.url("fw_attendance"), data).status_code, 302)
        self.assertEqual(Attendance.objects.count(), 2)
        data[f"w{self.worker.pk}_hours"] = "6"
        self.client.post(self.url("fw_attendance"), data)
        self.assertEqual(Attendance.objects.count(), 2)
        self.assertEqual(Attendance.objects.get(worker=self.worker).hours_worked, D("6"))

    def test_nothing_marked_is_an_error(self):
        self.as_user("eng")
        res = self.client.post(self.url("fw_attendance"), {"date": "2026-11-01"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Attendance.objects.count(), 0)


class RequisitionTests(FieldworkBase):
    def body(self, **extra):
        data = {"needed_by": "2026-11-10", "justification": "", "notes": "",
                "lines-TOTAL_FORMS": 5, "lines-INITIAL_FORMS": 0, "lines-MIN_NUM_FORMS": 0, "lines-MAX_NUM_FORMS": 1000,
                "lines-0-material": self.cement.pk, "lines-0-quantity": "50", "action": "submit"}
        data.update(extra)
        return data

    def test_submit(self):
        self.as_user("eng")
        self.assertEqual(self.client.post(self.url("fw_requisition"), self.body()).status_code, 302)
        req = Requisition.objects.get()
        self.assertEqual(req.status, "submitted")
        self.assertEqual(req.lines.count(), 1)

    def test_draft(self):
        self.as_user("eng")
        self.client.post(self.url("fw_requisition"), self.body(action="draft"))
        self.assertEqual(Requisition.objects.get().status, "draft")

    def test_no_lines_rejected(self):
        self.as_user("eng")
        data = self.body()
        data.pop("lines-0-material"), data.pop("lines-0-quantity")
        res = self.client.post(self.url("fw_requisition"), data)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Requisition.objects.count(), 0)


class StoreTests(FieldworkBase):
    def test_receive_then_issue(self):
        po, line = self.make_po("100")
        self.as_user("store")
        res = self.client.post(self.url("fw_receive", po.pk), {f"d{line.pk}": "60", f"a{line.pk}": "58"})
        self.assertEqual(res.status_code, 302, res.content[:500])
        grn = GoodsReceipt.objects.get()
        self.assertTrue(grn.posted)
        rows, _ = stock.stock_summary(self.project)
        self.assertEqual([r["balance"] for r in rows if r["material"] == self.cement], [D("58")])
        po.refresh_from_db()
        self.assertEqual(po.status, "partial")

        res = self.client.post(self.url("fw_issue"), {"date": "2026-11-01", "m0": self.cement.pk, "q0": "20"})
        self.assertEqual(res.status_code, 302, res.content[:500])
        self.assertTrue(MaterialIssue.objects.get().posted)
        rows, _ = stock.stock_summary(self.project)
        self.assertEqual([r["balance"] for r in rows if r["material"] == self.cement], [D("38")])

    def test_cannot_issue_more_than_in_stock(self):
        self.as_user("store")
        res = self.client.post(self.url("fw_issue"), {"m0": self.cement.pk, "q0": "5"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(MaterialIssue.objects.count(), 0)

    def test_cannot_over_receive(self):
        po, line = self.make_po("10")
        self.as_user("store")
        res = self.client.post(self.url("fw_receive", po.pk), {f"d{line.pk}": "11"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(GoodsReceipt.objects.count(), 0)

    def test_po_of_other_project_is_404(self):
        po = PurchaseOrder.objects.create(project=self.other, supplier=self.supplier, status="approved")
        self.as_user("store")
        self.assertEqual(self.client.get(reverse("fw_receive", args=[po.pk])).status_code, 404)

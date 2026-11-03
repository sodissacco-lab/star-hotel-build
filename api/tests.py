from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from boq.models import BOQItem
from inventory.models import Material, MaterialCategory, Unit
from procurement.models import Requisition
from projects.models import Phase, Project, SiteDiary
from workforce.models import Attendance, Worker

R = User.Role
D = Decimal


class APITestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password="pass-" + "x" * 10, role=role)
        cls.eng = mk("eng", R.SITE_ENGINEER)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.store = mk("store", R.STOREKEEPER)
        cls.outsider = mk("outsider", R.SITE_ENGINEER)
        cls.project = Project.objects.create(name="Hotel", code="API-1")
        cls.other = Project.objects.create(name="Other", code="API-2")
        for u in (cls.eng, cls.pm, cls.store):
            cls.project.members.add(u)
        cls.phase = Phase.objects.create(project=cls.project, name="Foundation")
        unit = Unit.objects.create(name="Bag", abbreviation="bag")
        cat = MaterialCategory.objects.create(name="Cement")
        cls.cement = Material.objects.create(code="C1", name="Cement", category=cat, unit=unit, standard_price=D("38000"))
        cls.worker = Worker.objects.create(project=cls.project, full_name="Okello", daily_wage=D("20000"))
        cls.worker2 = Worker.objects.create(project=cls.project, full_name="Akello", daily_wage=D("20000"))

    def setUp(self):
        cache.clear()  # the login limit (30 a minute) is counted in the cache and would trip across many tests

    def login(self, username):
        client = APIClient()
        res = client.post("/api/v1/auth/login/", {"username": username, "password": "pass-" + "x" * 10}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        client.credentials(HTTP_AUTHORIZATION="Token " + res.json()["token"])
        return client


class AuthTests(APITestBase):
    def test_login_returns_token_and_capabilities(self):
        res = APIClient().post("/api/v1/auth/login/", {"username": "eng", "password": "pass-" + "x" * 10}, format="json")
        data = res.json()
        self.assertTrue(data["token"])
        self.assertIn("raise_requisition", data["user"]["capabilities"])
        self.assertNotIn("approve_requisition", data["user"]["capabilities"])

    def test_wrong_password_and_no_token(self):
        res = APIClient().post("/api/v1/auth/login/", {"username": "eng", "password": "nope"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(APIClient().get("/api/v1/projects/").status_code, 401)

    def test_logout_invalidates_token(self):
        c = self.login("eng")
        self.assertEqual(c.post("/api/v1/auth/logout/").status_code, 204)
        self.assertEqual(c.get("/api/v1/me/").status_code, 401)

    def test_projects_only_for_members(self):
        self.assertEqual([p["code"] for p in self.login("eng").get("/api/v1/projects/").json()], ["API-1"])
        self.assertEqual(self.login("outsider").get("/api/v1/projects/").json(), [])

    def test_other_project_is_404(self):
        c = self.login("eng")
        self.assertEqual(c.get(f"/api/v1/projects/{self.other.pk}/stock/").status_code, 404)
        self.assertEqual(self.login("outsider").get(f"/api/v1/projects/{self.project.pk}/diary/").status_code, 404)


class DiaryTests(APITestBase):
    def test_create_and_list_diary(self):
        c = self.login("eng")
        body = {"date": "2026-11-01", "weather": "rainy", "workers_present": 12,
                "activities": [{"description": "Pouring blinding", "phase": self.phase.pk}]}
        res = c.post(f"/api/v1/projects/{self.project.pk}/diary/", body, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        diary = SiteDiary.objects.get()
        self.assertEqual(diary.created_by, self.eng)
        self.assertEqual(diary.activities.count(), 1)
        listing = c.get(f"/api/v1/projects/{self.project.pk}/diary/").json()
        self.assertEqual(listing[0]["activities"][0]["description"], "Pouring blinding")

    def test_duplicate_date_rejected(self):
        c = self.login("eng")
        url = f"/api/v1/projects/{self.project.pk}/diary/"
        self.assertEqual(c.post(url, {"date": "2026-11-01"}, format="json").status_code, 201)
        self.assertEqual(c.post(url, {"date": "2026-11-01"}, format="json").status_code, 400)

    def test_storekeeper_cannot_write_diary(self):
        res = self.login("store").post(f"/api/v1/projects/{self.project.pk}/diary/", {"date": "2026-11-01"}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_phase_from_another_project_rejected(self):
        foreign = Phase.objects.create(project=self.other, name="Roof")
        body = {"date": "2026-11-01", "activities": [{"description": "x", "phase": foreign.pk}]}
        res = self.login("eng").post(f"/api/v1/projects/{self.project.pk}/diary/", body, format="json")
        self.assertEqual(res.status_code, 400)


class AttendanceTests(APITestBase):
    def url(self):
        return f"/api/v1/projects/{self.project.pk}/attendance/"

    def test_record_day_and_correct_it(self):
        c = self.login("eng")
        body = {"date": "2026-11-02", "entries": [
            {"worker": self.worker.pk, "hours_worked": "8", "overtime_hours": "2"},
            {"worker": self.worker2.pk, "status": "absent"},
        ]}
        self.assertEqual(c.post(self.url(), body, format="json").status_code, 201)
        self.assertEqual(Attendance.objects.count(), 2)
        a2 = Attendance.objects.get(worker=self.worker2)
        self.assertEqual(a2.hours_worked, 0)  # absent -> zero hours
        body["entries"][1] = {"worker": self.worker2.pk, "hours_worked": "6"}
        self.assertEqual(c.post(self.url(), body, format="json").status_code, 201)
        self.assertEqual(Attendance.objects.count(), 2)  # corrected, not duplicated
        a2.refresh_from_db()
        self.assertEqual(a2.hours_worked, D("6.0"))
        self.assertEqual(len(c.get(self.url() + "?date=2026-11-02").json()), 2)

    def test_bad_entry_rolls_back_whole_day(self):
        body = {"date": "2026-11-02", "entries": [
            {"worker": self.worker.pk, "hours_worked": "8"},
            {"worker": self.worker2.pk, "hours_worked": "30"},
        ]}
        res = self.login("eng").post(self.url(), body, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_worker_of_another_project_rejected(self):
        foreign = Worker.objects.create(project=self.other, full_name="Elsewhere", daily_wage=D("1"))
        body = {"date": "2026-11-02", "entries": [{"worker": foreign.pk}]}
        self.assertEqual(self.login("eng").post(self.url(), body, format="json").status_code, 400)

    def test_storekeeper_forbidden(self):
        self.assertEqual(self.login("store").get(f"/api/v1/projects/{self.project.pk}/workers/").status_code, 403)

    def test_worker_list(self):
        names = [w["full_name"] for w in self.login("eng").get(f"/api/v1/projects/{self.project.pk}/workers/").json()]
        self.assertEqual(names, ["Akello", "Okello"])


class RequisitionTests(APITestBase):
    def url(self):
        return f"/api/v1/projects/{self.project.pk}/requisitions/"

    def body(self, **extra):
        return {"lines": [{"material": self.cement.pk, "quantity": "100"}], **extra}

    def test_create_submit_approve_flow_with_segregation(self):
        eng, pm = self.login("eng"), self.login("pm")
        res = eng.post(self.url(), self.body(emergency=True, justification="Rain stopped delivery"), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        rid = res.json()["id"]
        self.assertEqual(D(res.json()["estimated_total"]), D("3800000"))  # standard price filled in
        self.assertEqual(eng.post(f"/api/v1/requisitions/{rid}/submit/").json()["status"], "submitted")
        # the engineer cannot approve
        self.assertEqual(eng.post(f"/api/v1/requisitions/{rid}/approve/").status_code, 400)
        res = pm.post(f"/api/v1/requisitions/{rid}/approve/")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["status"], "approved")

    def test_creator_cannot_approve_own(self):
        pm = self.login("pm")
        rid = pm.post(self.url(), self.body(emergency=True, justification="Urgent"), format="json").json()["id"]
        pm.post(f"/api/v1/requisitions/{rid}/submit/")
        res = pm.post(f"/api/v1/requisitions/{rid}/approve/")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Segregation", res.json()["detail"])

    def test_validation(self):
        eng = self.login("eng")
        self.assertEqual(eng.post(self.url(), {"lines": []}, format="json").status_code, 400)
        self.assertEqual(eng.post(self.url(), {"lines": [{"material": self.cement.pk, "quantity": "0"}]}, format="json").status_code, 400)
        self.assertEqual(eng.post(self.url(), self.body(emergency=True), format="json").status_code, 400)  # no justification
        foreign_item = BOQItem.objects.create(project=self.other, code="1", description="x", unit=self.cement.unit,
                                              original_quantity=D("1"), rate=D("1"))
        bad = {"lines": [{"material": self.cement.pk, "quantity": "1", "boq_item": foreign_item.pk}]}
        self.assertEqual(eng.post(self.url(), bad, format="json").status_code, 400)
        self.assertEqual(Requisition.objects.count(), 0)

    def test_roles(self):
        self.assertEqual(self.login("store").post(self.url(), self.body(), format="json").status_code, 201)
        outsider = self.login("outsider")
        self.assertEqual(outsider.post(self.url(), self.body(), format="json").status_code, 404)

    def test_cannot_act_on_requisition_in_other_project(self):
        req = Requisition.objects.create(project=self.other)
        self.assertEqual(self.login("pm").post(f"/api/v1/requisitions/{req.pk}/submit/").status_code, 404)

    def test_unknown_action(self):
        eng = self.login("eng")
        rid = eng.post(self.url(), self.body(), format="json").json()["id"]
        self.assertEqual(eng.post(f"/api/v1/requisitions/{rid}/delete/").status_code, 404)

    def test_list_and_materials(self):
        eng = self.login("eng")
        eng.post(self.url(), self.body(), format="json")
        self.assertEqual(len(eng.get(self.url()).json()), 1)
        self.assertEqual(eng.get("/api/v1/materials/").json()[0]["unit"], "bag")


class ReportTests(APITestBase):
    def test_boq_requires_view_capability(self):
        url = f"/api/v1/projects/{self.project.pk}/boq/"
        self.assertEqual(self.login("store").get(url).status_code, 403)
        BOQItem.objects.create(project=self.project, code="1", description="Concrete", unit=self.cement.unit,
                               original_quantity=D("10"), rate=D("100"))
        data = self.login("pm").get(url).json()
        self.assertEqual(data["items"][0]["code"], "1")
        self.assertEqual(D(data["totals"]["estimated"]), D("1000"))

    def test_stock_endpoint(self):
        res = self.login("store").get(f"/api/v1/projects/{self.project.pk}/stock/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["materials"], [])
import io
from datetime import date, timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.utils import timezone
from openpyxl import load_workbook

from accounts.models import User
from finance.models import Budget, Expense
from inventory.models import Material, MaterialCategory, StockLedger, Unit, WastageRecord
from projects.models import Phase, Project, SiteActivity, SiteDiary
from reports import services
from workforce.models import Attendance, Worker

R = User.Role
D = Decimal
PW = "pass-" + "x" * 10


class ReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password=PW, role=role)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.store = mk("store", R.STOREKEEPER)
        cls.contractor = mk("sub", R.CONTRACTOR)
        cls.project = Project.objects.create(name="Hotel", code="RP-1", approved_budget=D("1000000"))
        for u in (cls.pm, cls.store, cls.contractor):
            cls.project.members.add(u)
        cls.today = timezone.localdate()
        cls.start = cls.today - timedelta(days=6)
        cls.phase = Phase.objects.create(project=cls.project, name="Foundation", budget=D("400000"))
        unit = Unit.objects.create(name="Bag", abbreviation="bag")
        cat = MaterialCategory.objects.create(name="Cement")
        cls.cement = Material.objects.create(code="C1", name="Cement", category=cat, unit=unit, standard_price=D("38000"))
        old = cls.start - timedelta(days=5)
        led = lambda day, kind, qty: StockLedger.objects.create(
            project=cls.project, material=cls.cement, entry_date=day, entry_type=kind, quantity=D(qty), unit_cost=D("1000"))
        led(old, "receipt", "100")           # before the period -> opening
        led(cls.start, "receipt", "50")
        led(cls.start, "issue", "-30")
        led(cls.start, "wastage", "-5")
        WastageRecord.objects.create(project=cls.project, material=cls.cement, date=cls.start, quantity=D("5"),
                                     reason="damaged", value=D("5000"), posted=True)
        diary = SiteDiary.objects.create(project=cls.project, date=cls.start, workers_present=4,
                                         challenges="Rain delayed concrete", safety_incidents="")
        SiteActivity.objects.create(diary=diary, phase=cls.phase, description="Excavation", work_completed="Done")
        worker = Worker.objects.create(project=cls.project, full_name="Okello", daily_wage=D("20000"))
        Attendance.objects.create(worker=worker, date=cls.start, status="present")
        Budget.objects.create(project=cls.project, category="materials", amount=D("500000"))
        Expense.objects.create(project=cls.project, phase=cls.phase, date=cls.start, category="materials",
                               description="Cement", amount=D("600000"), status="approved")

    def get(self, who, name, **params):
        client = Client()
        client.login(username=who, password=PW)
        return client.get(f"/reports/{name}/" if name else "/reports/", params)

    # ------------------------------------------------------------ numbers
    def test_materials_movement_quantities_balance(self):
        data = services.materials_movement(self.project, self.start, self.today)
        row = data["rows"][0]
        self.assertEqual((row["opening"], row["received"], row["issued"], row["wasted"], row["closing"]),
                         (D("100"), D("50"), D("30"), D("5"), D("115")))
        self.assertEqual(row["opening"] + row["received"] + row["returned"] + row["adjusted"] - row["issued"] - row["wasted"],
                         row["closing"])
        self.assertEqual(len(data["wastage"]), 1)

    def test_materials_after_period_do_not_count(self):
        data = services.materials_movement(self.project, self.start - timedelta(days=10), self.start - timedelta(days=1))
        self.assertEqual(data["rows"][0]["closing"], D("100"))

    def test_budget_vs_actual_flags_overspend(self):
        data = services.budget_vs_actual(self.project)
        cat = data["categories"][0]
        self.assertEqual((cat["budget"], cat["spent"], cat["variance"], cat["status"]),
                         (D("500000"), D("600000"), D("-100000"), "Over budget"))
        phase = data["phases"][0]
        self.assertEqual((phase["label"], phase["spent"]), ("Foundation", D("600000")))

    def test_progress_collects_the_week(self):
        data = services.weekly_progress(self.project, self.start, self.today)
        self.assertEqual(len(data["days"]), 7)
        self.assertEqual(data["attendance"]["worker_days"], 1)
        self.assertEqual(data["stock"]["issued"], D("30000"))
        self.assertEqual(len(data["activities"]), 1)
        self.assertEqual(len(data["problems"]), 1)
        self.assertEqual(len(data["diaries_missing"]), 6)
        self.assertEqual(data["spend_total"], D("600000"))

    def test_period_parsing(self):
        s, e = services.parse_period({"from": "2026-03-10", "to": "2026-03-01"})
        self.assertEqual((s, e), (date(2026, 3, 1), date(2026, 3, 10)))  # swapped into order
        s, e = services.parse_period({"from": "nonsense"}, default_days=7, today=date(2026, 5, 10))
        self.assertEqual((s, e), (date(2026, 5, 4), date(2026, 5, 10)))
        s, e = services.parse_period({"from": "2000-01-01", "to": "2026-01-01"})
        self.assertEqual((e - s).days, services.MAX_DAYS)

    # ------------------------------------------------------------ pages and access
    def test_pages_open_for_the_project_manager(self):
        for name in ("", "progress", "materials", "budget", "payroll"):
            res = self.get("pm", name)
            self.assertEqual(res.status_code, 200, name)

    def test_progress_page_shows_the_data(self):
        res = self.get("pm", "progress", **{"from": str(self.start), "to": str(self.today)})
        self.assertContains(res, "Excavation")
        self.assertContains(res, "Rain delayed concrete")

    def test_roles_are_respected(self):
        self.assertEqual(self.get("store", "progress").status_code, 200)
        self.assertEqual(self.get("store", "materials").status_code, 200)
        self.assertEqual(self.get("store", "budget").status_code, 403)
        self.assertEqual(self.get("store", "payroll").status_code, 403)
        self.assertEqual(self.get("sub", "progress").status_code, 403)
        self.assertEqual(self.get("sub", "materials").status_code, 403)

    def test_login_required(self):
        self.assertEqual(Client().get("/reports/progress/").status_code, 302)

    # ------------------------------------------------------------ excel
    def test_excel_downloads_open_and_contain_numbers(self):
        for name, sheet in (("progress", "Days"), ("materials", "Movement"), ("budget", "By category"), ("payroll", "Payrolls")):
            res = self.get("pm", name, format="xlsx", **{"from": str(self.start), "to": str(self.today)})
            self.assertEqual(res.status_code, 200, name)
            self.assertIn("spreadsheetml", res["Content-Type"])
            wb = load_workbook(io.BytesIO(res.content))
            self.assertIn(sheet, wb.sheetnames)

    def test_materials_excel_has_the_closing_balance(self):
        res = self.get("pm", "materials", format="xlsx", **{"from": str(self.start), "to": str(self.today)})
        ws = load_workbook(io.BytesIO(res.content))["Movement"]
        self.assertEqual([c.value for c in ws[4]][:9], ["Cement", "bag", 100, 50, 30, 0, 0, 5, 115])

    def test_excel_respects_permissions(self):
        self.assertEqual(self.get("store", "budget", format="xlsx").status_code, 403)
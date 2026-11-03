from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from accounts.models import User
from accounts.permissions import WorkflowError
from core import starter
from inventory.models import Material, MaterialCategory, Unit
from projects.models import Phase, Project

R = User.Role


class StarterTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("boss", password="pass-" + "x" * 10, role=R.ADMIN)
        cls.store = User.objects.create_user("store", password="pass-" + "x" * 10, role=R.STOREKEEPER)
        cls.project = Project.objects.create(name="Hotel", code="ST-1")

    def test_loads_everything(self):
        starter.load_starter_data(self.project, self.admin)
        self.assertEqual(Unit.objects.count(), len(starter.UNITS))
        self.assertEqual(MaterialCategory.objects.count(), len(starter.CATEGORIES))
        self.assertEqual(Material.objects.count(), len(starter.MATERIALS))
        self.assertEqual(Phase.objects.filter(project=self.project).count(), len(starter.PHASES))
        self.assertEqual(Material.objects.get(code="CEM01").unit.abbreviation, "bag")

    def test_second_run_adds_nothing_and_says_so(self):
        starter.load_starter_data(self.project, self.admin)
        with self.assertRaises(WorkflowError):
            starter.load_starter_data(self.project, self.admin)
        self.assertEqual(Material.objects.count(), len(starter.MATERIALS))

    def test_existing_records_are_never_overwritten(self):
        starter.load_starter_data(self.project, self.admin)
        Material.objects.filter(code="CEM01").update(standard_price=38000, name="My cement")
        Phase.objects.filter(name="Roofing").update(percent_complete=40)
        Material.objects.filter(code="SND01").delete()
        starter.load_starter_data(self.project, self.admin)  # adds only the missing sand
        cem = Material.objects.get(code="CEM01")
        self.assertEqual((cem.name, cem.standard_price), ("My cement", 38000))
        self.assertEqual(Phase.objects.get(project=self.project, name="Roofing").percent_complete, 40)
        self.assertTrue(Material.objects.filter(code="SND01").exists())

    def test_storekeeper_cannot_load(self):
        with self.assertRaises(WorkflowError):
            starter.load_starter_data(self.project, self.store)
        self.assertEqual(Material.objects.count(), 0)

    def test_every_material_uses_a_listed_unit_and_category(self):
        units = {n for n, _ in starter.UNITS}
        for code, name, cat, unit, reorder in starter.MATERIALS:
            self.assertIn(unit, units, code)
            self.assertIn(cat, starter.CATEGORIES, code)
        self.assertEqual(len({m[0] for m in starter.MATERIALS}), len(starter.MATERIALS))  # codes are unique

    def test_admin_action_reports_what_was_added(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from projects.admin import ProjectAdmin
        request = RequestFactory().post("/admin/projects/project/")
        request.user = self.admin
        request.session = {}
        request._messages = FallbackStorage(request)
        ProjectAdmin(Project, AdminSite()).load_starter_list(request, Project.objects.filter(pk=self.project.pk))
        text = " ".join(str(m) for m in request._messages)
        self.assertIn(f"added {len(starter.UNITS)} units", text)
        self.assertEqual(Material.objects.count(), len(starter.MATERIALS))
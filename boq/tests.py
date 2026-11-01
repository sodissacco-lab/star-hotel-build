from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import User
from accounts.permissions import WorkflowError
from boq import services
from boq.models import BOQItem, BOQMeasurement, Variation
from inventory.models import Material, MaterialCategory, Unit
from projects.models import Project

D = Decimal
R = User.Role


class BOQTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password="x" * 12, role=role)
        cls.qs = mk("qs", R.QUANTITY_SURVEYOR)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.engineer = mk("eng", R.SITE_ENGINEER)
        cls.project = Project.objects.create(name="Test Hotel", code="T-1")
        cls.unit = Unit.objects.create(name="Bag", abbreviation="bag")
        cat = MaterialCategory.objects.create(name="Cement")
        cls.cement = Material.objects.create(code="C1", name="Cement", category=cat, unit=cls.unit)
        cls.item = BOQItem.objects.create(
            project=cls.project, code="3.1", description="Concrete grade 25", unit=cls.unit,
            material=cls.cement, original_quantity=D("100"), rate=D("50000"),
        )

    def test_revised_quantity_starts_equal_to_original(self):
        self.assertEqual(self.item.revised_quantity, D("100"))
        self.assertEqual(self.item.estimated_cost, D("5000000"))

    def test_measurement_cannot_exceed_approved_quantity(self):
        BOQMeasurement.objects.create(item=self.item, quantity=D("60"), created_by=self.engineer)
        over = BOQMeasurement(item=self.item, quantity=D("50"))
        with self.assertRaises(ValidationError):
            over.full_clean()
        ok = BOQMeasurement(item=self.item, quantity=D("40"))
        ok.full_clean()

    def test_variation_approval_updates_revised_quantity(self):
        var = Variation.objects.create(item=self.item, description="Extra reinforcement",
                                       quantity_change=D("20"), created_by=self.qs)
        services.approve_variation(var, self.pm)
        self.item.refresh_from_db()
        self.assertEqual(self.item.revised_quantity, D("120"))
        self.assertEqual(self.item.original_quantity, D("100"))
        with self.assertRaises(WorkflowError):
            services.approve_variation(var, self.pm)  # already approved

    def test_creator_cannot_approve_own_variation(self):
        var = Variation.objects.create(item=self.item, description="x", quantity_change=D("5"), created_by=self.pm)
        with self.assertRaises(WorkflowError):
            services.approve_variation(var, self.pm)

    def test_variation_cannot_cut_below_measured_work(self):
        BOQMeasurement.objects.create(item=self.item, quantity=D("80"), created_by=self.engineer)
        var = Variation.objects.create(item=self.item, description="Reduce", quantity_change=D("-30"), created_by=self.qs)
        with self.assertRaises(WorkflowError):
            services.approve_variation(var, self.pm)

    def test_report_totals(self):
        BOQMeasurement.objects.create(item=self.item, quantity=D("25"), created_by=self.engineer)
        rows, phase_rows, totals = services.item_report(self.project)
        self.assertEqual(rows[0]["done"], D("25"))
        self.assertEqual(totals["estimated"], D("5000000"))
        self.assertEqual(totals["earned"], D("1250000"))
        self.assertEqual(totals["percent"], D("25"))

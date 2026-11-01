from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from accounts.permissions import WorkflowError
from boq.models import BOQItem
from finance.models import Budget, Category
from inventory.models import Material, MaterialCategory, PurchaseOrder, Supplier, Unit
from procurement import services
from procurement.models import Quotation, Requisition, RequisitionLine
from projects.models import Project

D = Decimal
R = User.Role


class RequisitionFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password="x" * 12, role=role)
        cls.engineer = mk("eng", R.SITE_ENGINEER)
        cls.qs = mk("qs", R.QUANTITY_SURVEYOR)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.buyer = mk("buyer", R.PROCUREMENT)
        cls.project = Project.objects.create(name="Test Hotel", code="T-1")
        unit = Unit.objects.create(name="Bag", abbreviation="bag")
        cat = MaterialCategory.objects.create(name="Cement")
        cls.cement = Material.objects.create(code="C1", name="Cement", category=cat, unit=unit,
                                             standard_price=D("38000"))
        cls.supplier = Supplier.objects.create(name="Acme")
        cls.supplier2 = Supplier.objects.create(name="Budget Mart")
        cls.item = BOQItem.objects.create(project=cls.project, code="3.1", description="Concrete", unit=unit,
                                          material=cls.cement, original_quantity=D("100"), rate=D("50000"))

    def make_req(self, qty="60", **kw):
        req = Requisition.objects.create(project=self.project, created_by=self.engineer, **kw)
        RequisitionLine.objects.create(requisition=req, material=self.cement, quantity=D(qty), boq_item=self.item)
        return req

    def test_line_takes_standard_price_when_blank(self):
        req = self.make_req()
        self.assertEqual(req.lines.get().estimated_unit_price, D("38000"))
        self.assertEqual(req.estimated_total, D("60") * D("38000"))

    def test_full_flow_to_draft_po_using_lowest_quotation(self):
        req = self.make_req()
        services.submit_requisition(req, self.engineer)
        services.verify_requisition(req, self.qs)
        services.approve_requisition(req, self.pm)
        Quotation.objects.create(requisition=req, supplier=self.supplier, amount=D("2400000"))
        Quotation.objects.create(requisition=req, supplier=self.supplier2, amount=D("2200000"))
        po = services.create_po_from_requisition(req, self.buyer)
        req.refresh_from_db()
        self.assertEqual(req.status, Requisition.Status.ORDERED)
        self.assertEqual(po.supplier, self.supplier2)
        self.assertEqual(po.status, PurchaseOrder.Status.DRAFT)
        self.assertEqual(po.lines.get().quantity, D("60"))

    def test_po_needs_quotation_unless_emergency(self):
        req = self.make_req()
        services.submit_requisition(req, self.engineer)
        services.verify_requisition(req, self.qs)
        services.approve_requisition(req, self.pm)
        with self.assertRaises(WorkflowError):
            services.create_po_from_requisition(req, self.buyer)

    def test_verification_blocks_quantity_above_boq(self):
        first = self.make_req("70")
        services.submit_requisition(first, self.engineer)
        services.verify_requisition(first, self.qs)
        second = self.make_req("40")  # 70 + 40 > 100
        services.submit_requisition(second, self.engineer)
        with self.assertRaises(WorkflowError):
            services.verify_requisition(second, self.qs)

    def test_verification_blocks_overspend_of_materials_budget(self):
        Budget.objects.create(project=self.project, category=Category.MATERIALS, amount=D("1000000"))
        req = self.make_req("60")  # estimated 2.28M
        services.submit_requisition(req, self.engineer)
        with self.assertRaises(WorkflowError):
            services.verify_requisition(req, self.qs)

    def test_roles_and_segregation(self):
        req = self.make_req()
        services.submit_requisition(req, self.engineer)
        with self.assertRaises(WorkflowError):
            services.verify_requisition(req, self.engineer)  # no capability
        services.verify_requisition(req, self.qs)
        with self.assertRaises(WorkflowError):
            services.approve_requisition(req, self.engineer)

    def test_emergency_needs_justification_and_skips_verification(self):
        bad = self.make_req(emergency=True)
        with self.assertRaises(WorkflowError):
            services.submit_requisition(bad, self.engineer)
        req = self.make_req(emergency=True, justification="Pump failure, pour must continue",
                            preferred_supplier=self.supplier)
        services.submit_requisition(req, self.engineer)
        services.approve_requisition(req, self.pm)  # no QS verification needed
        po = services.create_po_from_requisition(req, self.buyer)  # no quotation needed
        self.assertEqual(po.supplier, self.supplier)

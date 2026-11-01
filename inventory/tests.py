from decimal import Decimal

from django.test import TestCase, override_settings

from accounts.models import User
from accounts.permissions import WorkflowError
from finance.models import Expense
from inventory import services
from inventory.models import (
    GoodsReceipt, GoodsReceiptLine, Material, MaterialCategory, MaterialIssue, MaterialIssueLine,
    PurchaseOrder, PurchaseOrderLine, StockAdjustment, StockLedger, Supplier, Unit, WastageRecord,
)
from projects.models import Project

D = Decimal
R = User.Role


class StockFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role, **kw: User.objects.create_user(name, password="x" * 12, role=role, **kw)
        cls.storekeeper = mk("store", R.STOREKEEPER)
        cls.procurement = mk("proc", R.PROCUREMENT)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.owner = mk("owner", R.OWNER)
        cls.accountant = mk("acc", R.ACCOUNTANT)
        cls.project = Project.objects.create(name="Test Hotel", code="T-1")
        cat = MaterialCategory.objects.create(name="Cement")
        unit = Unit.objects.create(name="Bag", abbreviation="bag")
        cls.cement = Material.objects.create(code="C1", name="Cement", category=cat, unit=unit, reorder_level=D("10"))
        cls.supplier = Supplier.objects.create(name="Acme Hardware")

    # helpers
    def approved_po(self, qty="100", price="38000"):
        po = PurchaseOrder.objects.create(project=self.project, supplier=self.supplier, created_by=self.procurement)
        line = PurchaseOrderLine.objects.create(po=po, material=self.cement, quantity=D(qty), unit_price=D(price))
        services.submit_po(po, self.procurement)
        services.approve_po(po, self.pm)
        po.refresh_from_db()
        return po, line

    def receive(self, po, line, delivered, accepted):
        grn = GoodsReceipt.objects.create(po=po, created_by=self.storekeeper, delivery_note_no="DN-1")
        GoodsReceiptLine.objects.create(
            receipt=grn, po_line=line, quantity_delivered=D(delivered),
            quantity_accepted=D(accepted), quantity_rejected=D(delivered) - D(accepted),
        )
        return services.post_goods_receipt(grn, self.storekeeper)

    # tests
    def test_receipt_books_stock_and_approved_expense(self):
        po, line = self.approved_po()
        self.receive(po, line, "100", "98")
        self.assertEqual(services.stock_balance(self.project, self.cement), D("98"))
        exp = Expense.objects.get(project=self.project)
        self.assertEqual(exp.amount, D("98") * D("38000"))
        self.assertEqual(exp.status, Expense.Status.APPROVED)
        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.PARTIAL)  # 2 bags rejected, still outstanding

    def test_cannot_over_receive(self):
        po, line = self.approved_po(qty="10")
        with self.assertRaises(WorkflowError):
            self.receive(po, line, "11", "11")

    def test_issue_blocked_when_insufficient_and_allowed_when_available(self):
        po, line = self.approved_po()
        self.receive(po, line, "100", "100")
        issue = MaterialIssue.objects.create(project=self.project, created_by=self.storekeeper)
        MaterialIssueLine.objects.create(issue=issue, material=self.cement, quantity_issued=D("120"))
        with self.assertRaises(WorkflowError):
            services.post_issue(issue, self.storekeeper)
        issue.lines.all().delete()
        MaterialIssueLine.objects.create(issue=issue, material=self.cement, quantity_issued=D("25"))
        services.post_issue(issue, self.storekeeper)
        self.assertEqual(services.stock_balance(self.project, self.cement), D("75"))

    def test_stock_summary_values(self):
        po, line = self.approved_po()
        self.receive(po, line, "100", "100")
        issue = MaterialIssue.objects.create(project=self.project, created_by=self.storekeeper)
        MaterialIssueLine.objects.create(issue=issue, material=self.cement, quantity_issued=D("25"))
        services.post_issue(issue, self.storekeeper)
        rows, totals = services.stock_summary(self.project)
        self.assertEqual(rows[0]["balance"], D("75"))
        self.assertEqual(totals["stock_value"], D("75") * D("38000"))
        self.assertEqual(totals["purchased_value"], D("100") * D("38000"))
        self.assertEqual(totals["consumed_value"], D("25") * D("38000"))

    def test_storekeeper_cannot_approve_po_and_creator_cannot_self_approve(self):
        po = PurchaseOrder.objects.create(project=self.project, supplier=self.supplier, created_by=self.pm)
        PurchaseOrderLine.objects.create(po=po, material=self.cement, quantity=D("1"), unit_price=D("1"))
        services.submit_po(po, self.pm)
        with self.assertRaises(WorkflowError):
            services.approve_po(po, self.storekeeper)
        with self.assertRaises(WorkflowError):
            services.approve_po(po, self.pm)  # created it

    @override_settings(PO_APPROVAL_THRESHOLD_UGX=D("1000000"))
    def test_large_po_needs_owner(self):
        po = PurchaseOrder.objects.create(project=self.project, supplier=self.supplier, created_by=self.procurement)
        PurchaseOrderLine.objects.create(po=po, material=self.cement, quantity=D("100"), unit_price=D("38000"))
        services.submit_po(po, self.procurement)
        with self.assertRaises(WorkflowError):
            services.approve_po(po, self.pm)
        services.approve_po(po, self.owner)

    def test_ledger_is_append_only(self):
        po, line = self.approved_po()
        self.receive(po, line, "10", "10")
        entry = StockLedger.objects.first()
        entry.quantity = D("999")
        with self.assertRaises(Exception):
            entry.save()
        with self.assertRaises(Exception):
            entry.delete()
        with self.assertRaises(Exception):
            StockLedger.objects.all().delete()

    def test_stocktake_and_wastage(self):
        po, line = self.approved_po()
        self.receive(po, line, "100", "100")
        w = WastageRecord.objects.create(project=self.project, material=self.cement, quantity=D("5"),
                                         created_by=self.storekeeper)
        services.post_wastage(w, self.storekeeper)
        w.refresh_from_db()
        self.assertEqual(w.value, D("5") * D("38000"))
        adj = StockAdjustment.objects.create(project=self.project, material=self.cement,
                                             counted_quantity=D("90"), created_by=self.storekeeper)
        with self.assertRaises(WorkflowError):
            services.post_adjustment(adj, self.storekeeper)  # storekeeper lacks adjust_stock
        services.post_adjustment(adj, self.pm)
        adj.refresh_from_db()
        self.assertEqual(adj.system_quantity, D("95"))
        self.assertEqual(adj.difference, D("-5"))
        self.assertEqual(services.stock_balance(self.project, self.cement), D("90"))

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from accounts.permissions import WorkflowError
from finance import supplier
from finance.models import DemandNote, Expense, Payment, PaymentAllocation, Receipt
from inventory import services
from inventory.models import (
    GoodsReceipt, GoodsReceiptLine, Material, MaterialCategory, PurchaseOrder, PurchaseOrderLine, Supplier, Unit,
)
from projects.models import Project

D = Decimal
R = User.Role


class SupplierBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password="x" * 12, role=role)
        cls.acc = mk("acc", R.ACCOUNTANT)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.owner = mk("owner", R.OWNER)
        cls.qs = mk("qs", R.QUANTITY_SURVEYOR)
        cls.proc = mk("proc", R.PROCUREMENT)
        cls.store = mk("store", R.STOREKEEPER)
        cls.project = Project.objects.create(name="Test Hotel", code="S-1")
        for u in (cls.acc, cls.pm, cls.qs, cls.store):
            cls.project.members.add(u)
        cat = MaterialCategory.objects.create(name="Cement")
        unit = Unit.objects.create(name="Bag", abbreviation="bag")
        cls.cement = Material.objects.create(code="C1", name="Cement", category=cat, unit=unit)
        cls.supplier = Supplier.objects.create(name="Acme Hardware")

    def setUp(self):
        # 100 bags at 38,000 = UGX 3,800,000
        self.po = PurchaseOrder.objects.create(project=self.project, supplier=self.supplier, created_by=self.proc)
        self.line = PurchaseOrderLine.objects.create(po=self.po, material=self.cement, quantity=D("100"), unit_price=D("38000"))
        services.submit_po(self.po, self.proc)
        services.approve_po(self.po, self.pm)
        self.po.refresh_from_db()

    def pay(self, amount, by=None, **kw):
        return Payment.objects.create(purchase_order=self.po, amount=D(amount), method="bank",
                                      created_by=by or self.acc, **kw)

    def deliver(self, qty):
        grn = GoodsReceipt.objects.create(po=self.po, created_by=self.store, delivery_note_no="DN-1")
        GoodsReceiptLine.objects.create(receipt=grn, po_line=self.line, quantity_delivered=D(qty),
                                        quantity_accepted=D(qty), quantity_rejected=D("0"))
        services.post_goods_receipt(grn, self.store)

    def account(self):
        return supplier.po_account(PurchaseOrder.objects.get(pk=self.po.pk))


class AdvanceTests(SupplierBase):
    def test_payment_before_delivery_is_an_advance_needing_approval(self):
        pay = self.pay("1000000")
        self.assertEqual(pay.status, Payment.Status.PENDING)
        self.assertEqual(Receipt.objects.filter(payment=pay).count(), 0)  # no receipt until approved
        self.assertEqual(self.account()["advance"], D("0"))  # not yet real money paid
        supplier.approve_payment(pay, self.pm)
        pay.refresh_from_db()
        self.assertEqual(pay.status, Payment.Status.APPROVED)
        acct = self.account()
        self.assertEqual((acct["paid"], acct["advance"], acct["balance_due"]), (D("1000000"), D("1000000"), D("0")))

    def test_advance_receipt_shows_items_not_delivered(self):
        pay = self.pay("1000000")
        supplier.approve_payment(pay, self.pm)
        rc = Receipt.objects.get(payment=pay)
        snap = rc.snapshot
        self.assertEqual((rc.payee_type, rc.purchase_order), ("supplier", self.po))
        self.assertEqual((snap["kind"], snap["tone"]), ("supplier", "advance"))
        self.assertIn("goods have not yet been delivered", snap["status"])
        self.assertEqual(snap["lines"][0]["outstanding"], "100.000")
        self.assertEqual((snap["paid_before"], snap["this_payment"], snap["advance"]), ("0.00", "1000000.00", "1000000.00"))
        self.client.force_login(self.acc)
        res = self.client.get(reverse("receipt_print", args=[rc.pk]))
        self.assertContains(res, "ADVANCE PAYMENT")
        self.assertContains(res, "Cement")

    def test_advance_over_limit_needs_owner(self):
        pay = self.pay("2500000")  # 65% of the order, limit is 50%
        with self.assertRaises(WorkflowError):
            supplier.approve_payment(pay, self.pm)
        supplier.approve_payment(pay, self.owner)
        pay.refresh_from_db()
        self.assertEqual((pay.status, pay.approved_by), (Payment.Status.APPROVED, self.owner))

    def test_cannot_approve_own_advance_and_rejection_works(self):
        mine = self.pay("500000", by=self.pm)
        with self.assertRaises(WorkflowError):
            supplier.approve_payment(mine, self.pm)
        with self.assertRaises(WorkflowError):
            supplier.approve_payment(mine, self.store)  # no capability
        supplier.reject_payment(mine, self.owner)
        mine.refresh_from_db()
        self.assertEqual(mine.status, Payment.Status.REJECTED)
        self.assertEqual(self.account()["paid"], D("0"))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_cannot_pay_more_than_the_order(self):
        self.pay("3000000")
        with self.assertRaises(ValidationError):
            self.pay("1000000")
        with self.assertRaises(ValidationError):
            Payment(purchase_order=self.po, amount=D("1000000")).full_clean()  # same check on the admin form

    def test_cannot_pay_unapproved_order(self):
        draft = PurchaseOrder.objects.create(project=self.project, supplier=self.supplier)
        with self.assertRaises(ValidationError):
            Payment.objects.create(purchase_order=draft, amount=D("1"), created_by=self.acc)

    def test_choose_expense_or_order_not_both_or_neither(self):
        with self.assertRaises(ValidationError):
            Payment(amount=D("10")).full_clean()


class DeliverySyncTests(SupplierBase):
    def test_advance_is_applied_to_delivery_and_balance_updates(self):
        supplier.approve_payment(self.pay("1000000"), self.pm)
        self.deliver("40")  # 40 x 38,000 = 1,520,000
        exp = Expense.objects.get(source_type="inventory.goodsreceipt")
        self.assertEqual(exp.amount, D("1520000"))
        self.assertEqual(exp.paid_amount, D("1000000"))  # the advance was applied automatically
        self.assertEqual(exp.outstanding, D("520000"))
        acct = self.account()
        self.assertEqual((acct["delivered"], acct["advance"], acct["balance_due"]), (D("1520000"), D("0"), D("520000")))
        # paying the balance of what was delivered needs no approval
        pay = self.pay("520000")
        self.assertEqual(pay.status, Payment.Status.APPROVED)
        exp = Expense.objects.get(pk=exp.pk)
        self.assertEqual(exp.outstanding, D("0"))
        self.assertEqual(PaymentAllocation.objects.filter(expense=exp).count(), 2)  # applied from both payments
        rc = Receipt.objects.get(payment=pay)
        self.assertEqual(rc.snapshot["paid_before"], "1000000.00")
        self.assertEqual(rc.snapshot["paid_total"], "1520000.00")
        self.assertEqual(rc.snapshot["lines"][0]["outstanding"], "60.000")

    def test_part_delivery_leaves_part_of_advance_outstanding(self):
        supplier.approve_payment(self.pay("1000000"), self.pm)
        self.deliver("10")  # 380,000
        acct = self.account()
        self.assertEqual((acct["advance"], acct["balance_due"]), (D("620000"), D("0")))
        tone, text = supplier.position_text(acct)
        self.assertEqual(tone, "advance")
        self.assertIn("not yet been delivered", text)

    def test_delivery_without_payment_is_balance_due(self):
        self.deliver("100")
        acct = self.account()
        self.assertEqual((acct["balance_due"], acct["advance"]), (D("3800000"), D("0")))
        pay = self.pay("3800000")
        self.assertEqual(pay.status, Payment.Status.APPROVED)
        acct = self.account()
        self.assertEqual(supplier.position_text(acct)[0], "paid")
        self.assertIn("PAID IN FULL", supplier.position_text(acct)[1])

    def test_goods_expense_cannot_be_paid_directly(self):
        self.deliver("10")
        exp = Expense.objects.get(source_type="inventory.goodsreceipt")
        with self.assertRaises(ValidationError):
            Payment(expense=exp, amount=D("1000")).full_clean()


class DemandNoteTests(SupplierBase):
    def note(self, amount="1000000", ref="INV-77"):
        return DemandNote.objects.create(purchase_order=self.po, supplier_ref=ref, amount=D(amount), created_by=self.acc)

    def test_demand_before_delivery_is_flagged_as_advance_request(self):
        dn = self.note()
        self.assertEqual((dn.number, dn.project), ("DN-%05d" % dn.pk, self.project))
        supplier.verify_demand_note(dn, self.qs)
        dn.refresh_from_db()
        self.assertTrue(dn.is_advance_request)

    def test_demand_within_delivered_value_is_not_advance(self):
        self.deliver("50")  # 1,900,000
        dn = self.note("1500000")
        supplier.verify_demand_note(dn, self.qs)
        dn.refresh_from_db()
        self.assertFalse(dn.is_advance_request)

    def test_workflow_roles_and_segregation(self):
        dn = self.note()
        with self.assertRaises(WorkflowError):
            supplier.approve_demand_note(dn, self.pm)  # must be verified first
        with self.assertRaises(WorkflowError):
            supplier.verify_demand_note(dn, self.store)
        supplier.verify_demand_note(dn, self.qs)
        mine = DemandNote.objects.create(purchase_order=self.po, supplier_ref="X", amount=D("1"), created_by=self.pm)
        supplier.verify_demand_note(mine, self.qs)
        with self.assertRaises(WorkflowError):
            supplier.approve_demand_note(mine, self.pm)  # recorded it
        supplier.approve_demand_note(dn, self.pm)
        dn.refresh_from_db()
        self.assertEqual(dn.status, DemandNote.Status.APPROVED)

    def test_payments_against_demand_note_update_paid_and_balance(self):
        dn = self.note()
        supplier.verify_demand_note(dn, self.qs)
        with self.assertRaises(ValidationError):
            Payment(purchase_order=self.po, demand_note=dn, amount=D("100")).full_clean()  # not approved yet
        supplier.approve_demand_note(dn, self.pm)
        dn.refresh_from_db()
        p1 = self.pay("400000", demand_note=dn)
        supplier.approve_payment(p1, self.pm)
        dn = DemandNote.objects.get(pk=dn.pk)
        self.assertEqual((dn.paid, dn.balance, dn.payment_state), (D("400000"), D("600000"), "Part paid"))
        with self.assertRaises(ValidationError):
            self.pay("700000", demand_note=dn)  # more than is left on the demand
        supplier.approve_payment(self.pay("600000", demand_note=dn), self.pm)
        dn = DemandNote.objects.get(pk=dn.pk)
        self.assertEqual((dn.balance, dn.payment_state), (D("0"), "Paid in full"))
        rc = Receipt.objects.get(payment=p1)
        self.assertEqual(rc.demand_note, dn)
        self.assertEqual(rc.snapshot["demand_note"]["supplier_ref"], "INV-77")
        self.assertEqual(rc.snapshot["demand_note"]["balance"], "600000.00")

    def test_demands_cannot_exceed_order_value(self):
        self.note("3000000")
        with self.assertRaises(ValidationError):
            DemandNote(purchase_order=self.po, supplier_ref="B", amount=D("1000000")).clean()

    def test_rejected_demand_cannot_be_paid(self):
        dn = self.note()
        supplier.reject_demand_note(dn, self.qs)
        dn.refresh_from_db()
        self.assertEqual(dn.status, DemandNote.Status.REJECTED)
        with self.assertRaises(ValidationError):
            Payment(purchase_order=self.po, demand_note=dn, amount=D("100")).full_clean()


class ReportTests(SupplierBase):
    def test_summary_statement_and_advance_alerts(self):
        supplier.approve_payment(self.pay("1000000", date=date(2026, 9, 1)), self.pm)
        self.deliver("10")
        rows = supplier.supplier_summary(self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ordered"], rows[0]["delivered"], rows[0]["paid"], rows[0]["advance"]),
                         (D("3800000"), D("380000"), D("1000000"), D("620000")))
        adv = supplier.advances_overview(self.project, today=date(2026, 11, 1))[0]
        self.assertEqual((adv["advance"], adv["days"], adv["stale"]), (D("620000"), 61, True))
        self.assertEqual(adv["outstanding_lines"][0]["outstanding"], D("90"))
        st = supplier.statement(self.project, self.supplier)
        self.assertEqual(st["supplied_total"], D("380000"))
        self.assertEqual(st["supplied"][0]["material"], "Cement")
        self.assertEqual(len(st["payments"]), 1)

    def test_pages_render(self):
        supplier.approve_payment(self.pay("1000000"), self.pm)
        self.deliver("10")
        self.client.force_login(self.acc)
        res = self.client.get(reverse("supplier_list"), {"project": self.project.pk})
        self.assertContains(res, "Acme Hardware")
        res = self.client.get(reverse("supplier_statement", args=[self.supplier.pk]), {"project": self.project.pk})
        self.assertContains(res, "Register of items supplied")
        self.assertContains(res, "Cement")
        res = self.client.get(reverse("budget_report"), {"project": self.project.pk})
        self.assertContains(res, "Advances paid to suppliers")
        res = self.client.get(reverse("dashboard"), {"project": self.project.pk})
        self.assertEqual(res.status_code, 200)
        self.client.force_login(self.store)
        self.assertEqual(self.client.get(reverse("supplier_list")).status_code, 403)

    def test_dashboard_budget_and_stale_flags_dont_break_when_nothing_paid(self):
        self.client.force_login(self.acc)
        self.assertEqual(self.client.get(reverse("budget_report"), {"project": self.project.pk}).status_code, 200)

    def test_api(self):
        supplier.approve_payment(self.pay("1000000"), self.pm)
        client = APIClient()
        client.force_authenticate(self.acc)
        data = client.get(f"/api/v1/projects/{self.project.pk}/suppliers/").json()
        self.assertEqual((data[0]["supplier"], data[0]["advance_goods_not_delivered"]), ("Acme Hardware", "1000000.00"))
        acct = client.get(f"/api/v1/projects/{self.project.pk}/purchase-orders/{self.po.pk}/account/").json()
        self.assertEqual(acct["tone"], "advance")
        self.assertEqual(acct["items"][0]["outstanding"], "100.000")
        client.force_authenticate(self.store)
        self.assertEqual(client.get(f"/api/v1/projects/{self.project.pk}/suppliers/").status_code, 403)

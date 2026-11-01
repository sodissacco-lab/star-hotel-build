from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from accounts.permissions import WorkflowError
from finance import receipts
from finance.models import Category, Expense, Payment, Receipt
from finance.words import amount_in_words
from inventory.models import Supplier
from projects.models import Project
from workforce import services as wf
from workforce.models import Attendance, Contract, Contractor, PaymentCertificate, Payroll, Worker

D = Decimal
R = User.Role


class WordsTests(TestCase):
    def test_amounts(self):
        self.assertEqual(amount_in_words(D("3800000")), "Three million eight hundred thousand Uganda shillings only")
        self.assertEqual(amount_in_words(D("87500")), "Eighty-seven thousand five hundred Uganda shillings only")
        self.assertEqual(amount_in_words(D("1000050")), "One million and fifty Uganda shillings only")
        self.assertEqual(amount_in_words(D("120")), "One hundred and twenty Uganda shillings only")
        self.assertEqual(amount_in_words(D("2500.50")), "Two thousand five hundred Uganda shillings and fifty cents only")


class ReceiptBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password="x" * 12, role=role)
        cls.acc = mk("acc", R.ACCOUNTANT)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.hr = mk("hr", R.HR)
        cls.store = mk("store", R.STOREKEEPER)
        cls.outsider = mk("outsider", R.ACCOUNTANT)
        cls.project = Project.objects.create(name="Test Hotel", code="R-1")
        for u in (cls.acc, cls.pm, cls.hr, cls.store):
            cls.project.members.add(u)
        cls.supplier = Supplier.objects.create(name="Acme Hardware")

    def expense(self, amount="1000000", **kw):
        kw.setdefault("description", "Cement")
        return Expense.objects.create(project=self.project, category=Category.MATERIALS,
                                      amount=D(amount), status=Expense.Status.APPROVED, **kw)

    def pay(self, expense, amount, **kw):
        return Payment.objects.create(expense=expense, amount=D(amount), method="cash", created_by=self.acc, **kw)


class PaymentReceiptTests(ReceiptBase):
    def test_supplier_payment_gets_numbered_receipt(self):
        pay = self.pay(self.expense(supplier=self.supplier, reference="INV-9"), "400000")
        r = pay.receipts.get()
        self.assertEqual(r.number, f"RCT-{r.pk:05d}")
        self.assertEqual((r.payee_type, r.payee_name, r.amount), ("supplier", "Acme Hardware", D("400000")))
        self.assertEqual(r.supplier, self.supplier)
        self.assertEqual(r.reference, "INV-9")
        self.assertEqual(r.created_by, self.acc)

    def test_part_payments_each_get_a_receipt_with_unique_numbers(self):
        exp = self.expense(supplier=self.supplier)
        self.pay(exp, "300000")
        self.pay(exp, "700000")
        nums = list(Receipt.objects.values_list("number", flat=True))
        self.assertEqual(len(set(nums)), 2)

    def test_contractor_payment_names_the_contractor(self):
        contractor = Contractor.objects.create(name="BuildCo")
        contract = Contract.objects.create(project=self.project, contractor=contractor, reference="C-1", scope="x",
                                           amount=D("10000000"), retention_percent=D("5"), status=Contract.Status.ACTIVE)
        cert = PaymentCertificate.objects.create(contract=contract, description="Slab", gross_amount=D("4000000"),
                                                 created_by=self.hr)
        wf.approve_certificate(cert, self.pm)
        exp = Expense.objects.get(source_type="workforce.paymentcertificate", source_id=cert.pk)
        r = self.pay(exp, "3800000").receipts.get()
        self.assertEqual((r.payee_type, r.payee_name, r.contractor), ("contractor", "BuildCo", contractor))

    def test_payment_without_supplier_still_receipted(self):
        r = self.pay(self.expense(description="Casual transport"), "50000").receipts.get()
        self.assertEqual((r.payee_type, r.payee_name), ("other", "Casual transport"))

    def test_issued_receipt_cannot_be_edited(self):
        r = self.pay(self.expense(supplier=self.supplier), "100000").receipts.get()
        r.amount = D("1")
        with self.assertRaises(ValidationError):
            r.save()


class WorkerReceiptTests(ReceiptBase):
    def approved_payroll(self):
        w1 = Worker.objects.create(project=self.project, full_name="Okello", daily_wage=D("20000"))
        w2 = Worker.objects.create(project=self.project, full_name="Akello", daily_wage=D("10000"))
        d = date(2026, 11, 2)
        for i in range(2):
            Attendance.objects.create(worker=w1, date=d + timedelta(i), hours_worked=D("8"))
        Attendance.objects.create(worker=w2, date=d, hours_worked=D("8"))
        payroll = Payroll.objects.create(project=self.project, period_start=d, period_end=d + timedelta(6), created_by=self.hr)
        wf.fill_payroll(payroll, self.hr)
        wf.approve_payroll(payroll, self.pm)
        return payroll, Expense.objects.get(source_type="workforce.payroll", source_id=payroll.pk)

    def test_wage_payment_is_receipted_per_worker_after_full_payment(self):
        payroll, exp = self.approved_payroll()
        self.assertEqual(exp.amount, D("50000"))
        self.pay(exp, "20000")
        self.assertEqual(Receipt.objects.count(), 0)  # no lump-sum receipt for wages
        with self.assertRaises(WorkflowError):
            receipts.issue_worker_receipts(payroll, self.acc)  # not fully paid yet
        self.pay(exp, "30000")
        receipts.issue_worker_receipts(payroll, self.acc)
        got = {r.payee_name: r.amount for r in Receipt.objects.filter(payroll=payroll)}
        self.assertEqual(got, {"Okello": D("40000"), "Akello": D("10000")})
        with self.assertRaises(WorkflowError):  # nothing new to issue
            receipts.issue_worker_receipts(payroll, self.acc)

    def test_only_authorised_roles_issue_worker_receipts(self):
        payroll, exp = self.approved_payroll()
        self.pay(exp, "50000")
        with self.assertRaises(WorkflowError):
            receipts.issue_worker_receipts(payroll, self.store)

    def test_voided_worker_receipt_can_be_reissued(self):
        payroll, exp = self.approved_payroll()
        self.pay(exp, "50000")
        receipts.issue_worker_receipts(payroll, self.acc)
        r = Receipt.objects.get(payee_name="Akello")
        receipts.void_receipt(r, self.acc, "Wrong spelling of name")
        receipts.issue_worker_receipts(payroll, self.acc)
        self.assertEqual(Receipt.objects.filter(payee_name="Akello", voided=False).count(), 1)
        self.assertEqual(Receipt.objects.filter(payee_name="Akello").count(), 2)


class VoidAndViewTests(ReceiptBase):
    def setUp(self):
        self.receipt = self.pay(self.expense(supplier=self.supplier), "250000").receipts.get()

    def test_void_needs_reason_and_role(self):
        with self.assertRaises(WorkflowError):
            receipts.void_receipt(self.receipt, self.acc, "  ")
        with self.assertRaises(WorkflowError):
            receipts.void_receipt(self.receipt, self.store, "mistake")
        receipts.void_receipt(self.receipt, self.acc, "Entered against wrong supplier")
        self.receipt.refresh_from_db()
        self.assertTrue(self.receipt.voided)
        self.assertEqual(self.receipt.voided_by, self.acc)
        with self.assertRaises(WorkflowError):
            receipts.void_receipt(self.receipt, self.acc, "again")

    def test_print_page(self):
        self.client.force_login(self.acc)
        res = self.client.get(reverse("receipt_print", args=[self.receipt.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.receipt.number)
        self.assertContains(res, "Two hundred and fifty thousand Uganda shillings only")
        self.assertContains(res, "Acme Hardware")

    def test_print_page_hidden_from_non_members_and_unauthorised_roles(self):
        url = reverse("receipt_print", args=[self.receipt.pk])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.store)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_register_filters_and_totals(self):
        other = self.pay(self.expense(description="Sand"), "50000").receipts.get()
        receipts.void_receipt(other, self.acc, "Duplicate")
        self.client.force_login(self.acc)
        res = self.client.get(reverse("receipt_list"), {"project": self.project.pk})
        self.assertEqual(res.context["total"], D("250000"))  # voided receipt excluded from the total
        res = self.client.get(reverse("receipt_list"), {"project": self.project.pk, "q": "acme"})
        self.assertContains(res, self.receipt.number)
        self.assertNotContains(res, other.number)
        res = self.client.get(reverse("receipt_list"), {"project": self.project.pk, "from": "not-a-date"})
        self.assertEqual(res.status_code, 200)

    def test_batch_print(self):
        self.client.force_login(self.acc)
        res = self.client.get(reverse("receipt_batch"), {"ids": str(self.receipt.pk)})
        self.assertContains(res, self.receipt.number)
        self.assertEqual(self.client.get(reverse("receipt_batch"), {"ids": "99999"}).status_code, 404)
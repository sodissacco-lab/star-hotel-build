from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import User
from accounts.permissions import WorkflowError
from finance import receipts
from finance.models import Category, Expense, Payment, Receipt
from projects.models import Project
from workforce import services as wf
from workforce.models import (
    Attendance, Contract, Contractor, ContractorAdvance, PaymentCertificate, Payroll, Worker, WorkerAdvance,
)

D = Decimal
R = User.Role


class AdvanceBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password="x" * 12, role=role)
        cls.acc = mk("acc", R.ACCOUNTANT)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.hr = mk("hr", R.HR)
        cls.qs = mk("qs", R.QUANTITY_SURVEYOR)
        cls.store = mk("store", R.STOREKEEPER)
        cls.project = Project.objects.create(name="Test Hotel", code="A-1")


class ContractorAdvanceTests(AdvanceBase):
    def setUp(self):
        self.contractor = Contractor.objects.create(name="BuildCo")
        self.contract = Contract.objects.create(
            project=self.project, contractor=self.contractor, reference="C-1", scope="Foundation",
            amount=D("10000000"), retention_percent=D("5"), status=Contract.Status.ACTIVE)

    def advance(self, amount="2000000", by=None):
        return ContractorAdvance.objects.create(contract=self.contract, amount=D(amount), created_by=by or self.qs)

    def cert(self, gross):
        return PaymentCertificate.objects.create(contract=self.contract, description="Work", gross_amount=D(gross),
                                                 created_by=self.qs)

    def test_limit_is_a_percentage_of_the_contract(self):
        ContractorAdvance(contract=self.contract, amount=D("2000000")).full_clean()
        with self.assertRaises(ValidationError):
            ContractorAdvance(contract=self.contract, amount=D("2500000")).full_clean()

    def test_limit_counts_earlier_advances(self):
        adv = self.advance("1500000")
        wf.approve_contractor_advance(adv, self.pm)
        with self.assertRaises(ValidationError):
            ContractorAdvance(contract=self.contract, amount=D("600000")).full_clean()

    def test_approval_books_expense_and_checks_roles(self):
        adv = self.advance()
        with self.assertRaises(WorkflowError):
            wf.approve_contractor_advance(adv, self.hr)  # no capability
        adv.created_by = self.pm
        adv.save()
        with self.assertRaises(WorkflowError):
            wf.approve_contractor_advance(adv, self.pm)  # raised it himself
        adv.created_by = self.qs
        adv.save()
        wf.approve_contractor_advance(adv, self.pm)
        exp = Expense.objects.get(source_type="workforce.contractoradvance", source_id=adv.pk)
        self.assertEqual((exp.amount, exp.category, exp.status), (D("2000000"), Category.SUBCONTRACTOR, "approved"))
        with self.assertRaises(WorkflowError):
            wf.approve_contractor_advance(adv, self.pm)  # already approved

    def test_certificates_recover_the_advance_proportionally(self):
        wf.approve_contractor_advance(self.advance(), self.pm)
        c1 = self.cert("4000000")
        wf.approve_certificate(c1, self.pm)
        c1.refresh_from_db()
        # 4,000,000 x 2,000,000 / 10,000,000 = 800,000 recovered; retention 200,000
        self.assertEqual(c1.advance_recovered, D("800000.00"))
        self.assertEqual(c1.net_amount, D("3000000.00"))
        exp = Expense.objects.get(source_type="workforce.paymentcertificate", source_id=c1.pk)
        self.assertEqual(exp.amount, D("3000000.00"))
        self.assertEqual(self.contract.advance_outstanding, D("1200000.00"))
        c2 = self.cert("6000000")
        wf.approve_certificate(c2, self.pm)
        c2.refresh_from_db()
        self.assertEqual(c2.advance_recovered, D("1200000.00"))
        self.assertEqual(self.contract.advance_outstanding, D("0"))

    def test_draft_advance_is_not_recovered(self):
        self.advance()
        c = self.cert("1000000")
        wf.approve_certificate(c, self.pm)
        c.refresh_from_db()
        self.assertEqual(c.advance_recovered, D("0"))

    def test_report_shows_outstanding_advance(self):
        wf.approve_contractor_advance(self.advance(), self.pm)
        wf.approve_certificate(self.cert("4000000"), self.pm)
        row = wf.labour_summary(self.project)["contract_rows"][0]
        self.assertEqual(row["advance_outstanding"], D("1200000.00"))
        self.assertEqual(row["unpaid"], D("3000000.00"))

    def test_advance_payment_gets_a_contractor_receipt(self):
        adv = self.advance()
        wf.approve_contractor_advance(adv, self.pm)
        exp = Expense.objects.get(source_type="workforce.contractoradvance", source_id=adv.pk)
        pay = Payment.objects.create(expense=exp, amount=D("2000000"), method="bank", created_by=self.acc)
        r = pay.receipts.get()
        self.assertEqual((r.payee_type, r.contractor), ("contractor", self.contractor))
        self.assertEqual(r.snapshot["kind"], "contractor_advance")
        self.assertEqual(r.snapshot["outstanding"], "2000000.00")


class WorkerAdvanceTests(AdvanceBase):
    def setUp(self):
        self.worker = Worker.objects.create(project=self.project, full_name="Okello", daily_wage=D("20000"))

    def advance(self, amount="100000"):
        return WorkerAdvance.objects.create(worker=self.worker, amount=D(amount), reason="School fees",
                                            created_by=self.hr)

    def work(self, start, days):
        for i in range(days):
            Attendance.objects.create(worker=self.worker, date=start + timedelta(i), hours_worked=D("8"))

    def payroll(self, start, end):
        p = Payroll.objects.create(project=self.project, period_start=start, period_end=end, created_by=self.hr)
        wf.fill_payroll(p, self.hr)
        return p

    def test_limit_is_days_of_wages(self):
        WorkerAdvance(worker=self.worker, amount=D("520000")).full_clean()
        with self.assertRaises(ValidationError):
            WorkerAdvance(worker=self.worker, amount=D("520001")).full_clean()

    def test_contractor_workers_cannot_get_salary_advances(self):
        w = Worker.objects.create(project=self.project, full_name="Sub", daily_wage=D("15000"),
                                  contractor=Contractor.objects.create(name="BuildCo"))
        with self.assertRaises(ValidationError):
            WorkerAdvance(worker=w, amount=D("10000")).full_clean()

    def test_only_one_open_advance(self):
        adv = self.advance()
        with self.assertRaises(ValidationError):
            WorkerAdvance(worker=self.worker, amount=D("10000")).full_clean()  # first still a draft
        wf.approve_worker_advance(adv, self.pm)
        with self.assertRaises(ValidationError):
            WorkerAdvance(worker=self.worker, amount=D("10000")).full_clean()  # first not repaid

    def test_approval_rules_and_expense(self):
        adv = self.advance()
        with self.assertRaises(WorkflowError):
            wf.approve_worker_advance(adv, self.store)
        wf.approve_worker_advance(adv, self.pm)
        exp = Expense.objects.get(source_type="workforce.workeradvance", source_id=adv.pk)
        self.assertEqual((exp.amount, exp.category), (D("100000"), Category.LABOUR))

    def test_payroll_deducts_up_to_half_of_the_pay(self):
        adv = self.advance("100000")
        wf.approve_worker_advance(adv, self.pm)
        d = date(2026, 11, 2)
        self.work(d, 2)  # gross 40,000 -> deduction capped at 50% = 20,000
        p = self.payroll(d, d + timedelta(6))
        line = p.lines.get()
        self.assertEqual((line.total, line.advance_deducted, line.net), (D("40000.00"), D("20000.00"), D("20000.00")))
        wf.approve_payroll(p, self.pm)
        exp = Expense.objects.get(source_type="workforce.payroll", source_id=p.pk)
        self.assertEqual(exp.amount, D("20000.00"))
        adv.refresh_from_db()
        self.assertEqual(adv.outstanding, D("80000.00"))

    def test_deduction_continues_until_cleared(self):
        adv = self.advance("30000")
        wf.approve_worker_advance(adv, self.pm)
        d = date(2026, 11, 2)
        self.work(d, 2)
        self.payroll(d, d + timedelta(6))  # deducts 20,000
        d2 = date(2026, 11, 9)
        self.work(d2, 2)
        p2 = self.payroll(d2, d2 + timedelta(6))  # only 10,000 left
        self.assertEqual(p2.lines.get().advance_deducted, D("10000.00"))
        adv.refresh_from_db()
        self.assertEqual(adv.outstanding, D("0"))
        d3 = date(2026, 11, 16)
        self.work(d3, 1)
        self.assertEqual(self.payroll(d3, d3 + timedelta(6)).lines.get().advance_deducted, D("0"))

    def test_worker_receipt_shows_deduction_and_balance(self):
        adv = self.advance("100000")
        wf.approve_worker_advance(adv, self.pm)
        d = date(2026, 11, 2)
        self.work(d, 2)
        p = self.payroll(d, d + timedelta(6))
        wf.approve_payroll(p, self.pm)
        exp = Expense.objects.get(source_type="workforce.payroll", source_id=p.pk)
        Payment.objects.create(expense=exp, amount=exp.amount, method="cash", created_by=self.acc)
        receipts.issue_worker_receipts(p, self.acc)
        r = Receipt.objects.get(payroll=p)
        self.assertEqual(r.amount, D("20000.00"))
        self.assertEqual(r.snapshot["gross"], "40000.00")
        self.assertEqual(r.snapshot["advance_deducted"], "20000.00")
        self.assertEqual(r.snapshot["advance_balance"], "80000.00")

    def test_advance_payment_gets_a_worker_receipt(self):
        adv = self.advance()
        wf.approve_worker_advance(adv, self.pm)
        exp = Expense.objects.get(source_type="workforce.workeradvance", source_id=adv.pk)
        r = Payment.objects.create(expense=exp, amount=D("100000"), method="cash", created_by=self.acc).receipts.get()
        self.assertEqual((r.payee_type, r.worker), ("worker", self.worker))
        self.assertEqual(r.snapshot["kind"], "worker_advance")
        self.assertEqual(r.snapshot["outstanding"], "100000.00")

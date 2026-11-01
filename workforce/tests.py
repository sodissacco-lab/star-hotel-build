from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import User
from accounts.permissions import WorkflowError
from finance.models import Category, Expense
from projects.models import Project
from workforce import services
from workforce.models import (
    Attendance, Contract, Contractor, PaymentCertificate, Payroll, Worker,
)

D = Decimal
R = User.Role


class WorkforceTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        mk = lambda name, role: User.objects.create_user(name, password="x" * 12, role=role)
        cls.hr = mk("hr", R.HR)
        cls.pm = mk("pm", R.PROJECT_MANAGER)
        cls.qs = mk("qs", R.QUANTITY_SURVEYOR)
        cls.supervisor = mk("sup", R.SUPERVISOR)
        cls.project = Project.objects.create(name="Test Hotel", code="T-1")


class PayTests(WorkforceTestBase):
    def worker(self, wage="20000", **kw):
        return Worker.objects.create(project=self.project, full_name="Okello", daily_wage=D(wage), **kw)

    def att(self, worker, day, hours="8", overtime="0", status="present"):
        return Attendance.objects.create(worker=worker, date=day, hours_worked=D(hours),
                                         overtime_hours=D(overtime), status=status)

    def test_full_half_and_overtime_pay(self):
        w = self.worker()
        d = date(2026, 11, 2)
        self.assertEqual(services.pay_for(self.att(w, d), w.daily_wage), (D("20000.00"), D("0.00")))
        self.assertEqual(services.pay_for(self.att(w, d + timedelta(1), hours="4"), w.daily_wage)[0], D("10000.00"))
        # 2 overtime hours: 2 x (20000 / 8) x 1.5 = 7500
        self.assertEqual(services.pay_for(self.att(w, d + timedelta(2), overtime="2"), w.daily_wage)[1], D("7500.00"))
        self.assertEqual(services.pay_for(self.att(w, d + timedelta(3), status="absent", hours="0"), w.daily_wage),
                         (D("0"), D("0")))

    def test_hours_above_a_day_do_not_raise_regular_pay(self):
        w = self.worker()
        a = self.att(w, date(2026, 11, 2), hours="10")
        self.assertEqual(services.pay_for(a, w.daily_wage)[0], D("20000.00"))

    def test_absent_attendance_is_cleaned_to_zero_hours(self):
        w = self.worker()
        a = Attendance(worker=w, date=date(2026, 11, 2), status="absent", hours_worked=D("8"))
        a.full_clean()
        self.assertEqual(a.hours_worked, 0)


class PayrollTests(PayTests):
    def make_payroll(self):
        w = self.worker()
        d = date(2026, 11, 2)
        for i in range(3):
            self.att(w, d + timedelta(i))
        self.att(w, d + timedelta(3), overtime="2")
        return w, Payroll.objects.create(project=self.project, period_start=d, period_end=d + timedelta(6),
                                         created_by=self.hr)

    def test_build_and_approve_creates_labour_expense(self):
        w, payroll = self.make_payroll()
        services.fill_payroll(payroll, self.hr)
        self.assertEqual(payroll.total, D("87500"))  # 4 days x 20000 + 7500 overtime
        self.assertEqual(payroll.lines.get().days_present, D("4"))
        services.approve_payroll(payroll, self.pm)
        exp = Expense.objects.get(source_id=payroll.pk, source_type="workforce.payroll")
        self.assertEqual(exp.amount, D("87500"))
        self.assertEqual(exp.category, Category.LABOUR)
        self.assertEqual(exp.status, Expense.Status.APPROVED)

    def test_attendance_cannot_be_paid_twice(self):
        w, payroll = self.make_payroll()
        services.fill_payroll(payroll, self.hr)
        second = Payroll(project=self.project, period_start=payroll.period_start, period_end=payroll.period_end)
        with self.assertRaises(ValidationError):
            second.full_clean()  # nothing unpaid is left

    def test_deleting_draft_payroll_frees_attendance(self):
        w, payroll = self.make_payroll()
        services.fill_payroll(payroll, self.hr)
        payroll.delete()
        self.assertEqual(Attendance.objects.filter(payroll__isnull=True).count(), 4)

    def test_roles_and_segregation(self):
        w, payroll = self.make_payroll()
        with self.assertRaises(WorkflowError):
            services.fill_payroll(payroll, self.supervisor)  # not allowed to build payroll
        services.fill_payroll(payroll, self.hr)
        with self.assertRaises(WorkflowError):
            services.approve_payroll(payroll, self.hr)  # prepared it / no capability

    def test_contractor_workers_are_not_on_payroll(self):
        contractor = Contractor.objects.create(name="BuildCo")
        w = Worker.objects.create(project=self.project, full_name="Opio", contractor=contractor)
        self.att(w, date(2026, 11, 2))
        payroll = Payroll(project=self.project, period_start=date(2026, 11, 2), period_end=date(2026, 11, 8))
        with self.assertRaises(ValidationError):
            payroll.full_clean()


class CertificateTests(WorkforceTestBase):
    def setUp(self):
        self.contractor = Contractor.objects.create(name="BuildCo")
        self.contract = Contract.objects.create(
            project=self.project, contractor=self.contractor, reference="C-001", scope="Foundation",
            amount=D("10000000"), retention_percent=D("5"), status=Contract.Status.ACTIVE,
        )

    def cert(self, gross):
        return PaymentCertificate.objects.create(contract=self.contract, description="Work", gross_amount=D(gross),
                                                 created_by=self.qs)

    def test_approval_books_net_expense_and_tracks_retention(self):
        cert = self.cert("4000000")
        services.approve_certificate(cert, self.pm)
        cert.refresh_from_db()
        self.assertEqual(cert.retention_amount, D("200000.00"))
        self.assertEqual(cert.net_amount, D("3800000.00"))
        exp = Expense.objects.get(source_id=cert.pk, source_type="workforce.paymentcertificate")
        self.assertEqual(exp.amount, D("3800000.00"))
        self.assertEqual(exp.category, Category.SUBCONTRACTOR)
        self.assertEqual(self.contract.retention_held, D("200000.00"))
        self.assertEqual(self.contract.balance, D("6000000"))

    def test_cannot_certify_more_than_contract_amount(self):
        services.approve_certificate(self.cert("7000000"), self.pm)
        with self.assertRaises(WorkflowError):
            services.approve_certificate(self.cert("4000000"), self.pm)

    def test_creator_cannot_approve_and_unauthorised_role_blocked(self):
        cert = PaymentCertificate.objects.create(contract=self.contract, description="x", gross_amount=D("1000"),
                                                 created_by=self.pm)
        with self.assertRaises(WorkflowError):
            services.approve_certificate(cert, self.pm)
        with self.assertRaises(WorkflowError):
            services.approve_certificate(cert, self.hr)

    def test_inactive_contract_cannot_get_certificates(self):
        self.contract.status = Contract.Status.DRAFT
        self.contract.save()
        cert = PaymentCertificate(contract=self.contract, description="x", gross_amount=D("1000"))
        with self.assertRaises(ValidationError):
            cert.full_clean()

    def test_report_runs_and_shows_contract_totals(self):
        services.approve_certificate(self.cert("4000000"), self.pm)
        summary = services.labour_summary(self.project)
        row = summary["contract_rows"][0]
        self.assertEqual(row["certified"], D("4000000"))
        self.assertEqual(row["unpaid"], D("3800000.00"))
        self.assertEqual(len(summary["headcount"]), 14)

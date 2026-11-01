import io
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from boq import importer
from boq.models import BOQItem
from inventory.models import Material, MaterialCategory, Unit
from projects.models import Phase, Project

D = Decimal

HEADER = "code,description,unit,quantity,rate,phase,material\n"


def csv_bytes(body, header=HEADER):
    return (header + body).encode("utf-8")


class BOQImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("qs", password="x" * 12, role=User.Role.QUANTITY_SURVEYOR)
        cls.project = Project.objects.create(name="Test Hotel", code="T-1")
        cls.other_project = Project.objects.create(name="Other", code="T-2")
        cls.bag = Unit.objects.create(name="Bag", abbreviation="bag")
        cls.m3 = Unit.objects.create(name="Cubic metre", abbreviation="m3")
        cls.foundation = Phase.objects.create(project=cls.project, name="Foundation")
        Phase.objects.create(project=cls.other_project, name="Roofing")
        cat = MaterialCategory.objects.create(name="Cement")
        cls.cement = Material.objects.create(code="CEM-TOR", name="Tororo cement", category=cat, unit=cls.bag)

    def run_import(self, body, **kw):
        return importer.import_boq(csv_bytes(body), "boq.csv", self.project, self.user, **kw)

    def test_successful_import_sets_revised_quantity_and_links(self):
        result = self.run_import(
            "2.1,Concrete grade 25,m3,120,520000,Foundation,\n"
            "2.2,Cement for blinding,bag,\"1,300.5\",38000,foundation,cem-tor\n"
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(result.created, 2)
        item = BOQItem.objects.get(project=self.project, code="2.2")
        self.assertEqual(item.original_quantity, D("1300.500"))
        self.assertEqual(item.revised_quantity, D("1300.500"))
        self.assertEqual(item.rate, D("38000.00"))
        self.assertEqual(item.phase, self.foundation)
        self.assertEqual(item.material, self.cement)
        self.assertEqual(item.created_by, self.user)

    def test_heading_rows_and_blank_rows_are_skipped(self):
        result = self.run_import(
            "SECTION 2 - SUBSTRUCTURE,,,,,,\n"
            ",,,,,,\n"
            "2.1,Concrete,m3,10,500000,,\n"
            "Total,,,,,,\n"
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(result.created, 1)
        self.assertEqual(result.headings_skipped, 2)

    def test_any_error_means_nothing_is_saved(self):
        result = self.run_import(
            "1,Good item,m3,10,1000,,\n"
            "2,Bad unit,furlong,10,1000,,\n"
            "3,Bad number,m3,abc,1000,,\n"
            "4,Bad phase,m3,1,1,Nowhere,\n"
            "5,Bad material,m3,1,1,,NOPE\n"
        )
        self.assertEqual(result.created, 0)
        self.assertEqual(BOQItem.objects.count(), 0)
        self.assertEqual(len(result.errors), 4)
        self.assertTrue(result.errors[0].startswith("Row 3:"))  # header is row 1
        self.assertEqual(result.valid_rows, 1)

    def test_duplicates_in_file_and_existing_items_are_rejected(self):
        BOQItem.objects.create(project=self.project, code="1", description="Existing", unit=self.m3,
                               original_quantity=D("5"), rate=D("100"))
        result = self.run_import("1,Again,m3,1,1,,\n2,New,m3,1,1,,\n2,New again,m3,1,1,,\n")
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(BOQItem.objects.count(), 1)

    def test_same_code_is_allowed_in_a_different_project(self):
        BOQItem.objects.create(project=self.other_project, code="1", description="Other", unit=self.m3,
                               original_quantity=D("5"), rate=D("100"))
        result = self.run_import("1,Here,m3,1,1,,\n")
        self.assertEqual(result.errors, [])
        self.assertEqual(result.created, 1)

    def test_dry_run_saves_nothing(self):
        result = self.run_import("1,Item,m3,1,1,,\n", dry_run=True)
        self.assertEqual(result.valid_rows, 1)
        self.assertEqual(result.created, 0)
        self.assertEqual(BOQItem.objects.count(), 0)

    def test_missing_units_can_be_created(self):
        result = self.run_import("1,Fencing,Roll,2,50000,,\n2,More fencing,roll,3,50000,,\n", create_units=True)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.units_created, 1)
        self.assertEqual(Unit.objects.filter(name="Roll").count(), 1)
        self.assertEqual(BOQItem.objects.filter(unit__name="Roll").count(), 2)

    def test_headers_can_use_other_names_and_order(self):
        data = "Item No,Qty,UOM,Unit Rate,Item Description\n7.1,4,m3,2500,Blockwork\n".encode()
        result = importer.import_boq(data, "x.csv", self.project, self.user)
        self.assertEqual(result.errors, [])
        item = BOQItem.objects.get(code="7.1")
        self.assertEqual((item.original_quantity, item.rate, item.description), (D("4"), D("2500"), "Blockwork"))

    def test_file_level_errors(self):
        with self.assertRaises(importer.ImportFileError):
            importer.import_boq(b"a,b,c\n1,2,3\n", "x.csv", self.project, self.user)  # no header
        with self.assertRaises(importer.ImportFileError):
            importer.import_boq(b"whatever", "boq.pdf", self.project, self.user)  # wrong type
        with self.assertRaises(importer.ImportFileError):
            importer.import_boq(csv_bytes(""), "empty.csv", self.project, self.user)  # no rows

    def test_excel_file(self):
        try:
            from openpyxl import Workbook
        except ImportError:
            self.skipTest("openpyxl is not installed")
        wb = Workbook()
        ws = wb.active
        ws.append(["BILL NO. 2", None, None, None, None])
        ws.append(["Code", "Description", "Unit", "Quantity", "Rate (UGX)"])
        ws.append(["2.1", "Concrete grade 25", "m3", 120, 520000.5])
        buf = io.BytesIO()
        wb.save(buf)
        result = importer.import_boq(buf.getvalue(), "boq.xlsx", self.project, self.user)
        self.assertEqual(result.errors, [])
        item = BOQItem.objects.get(code="2.1")
        self.assertEqual(item.original_quantity, D("120.000"))
        self.assertEqual(item.rate, D("520000.50"))

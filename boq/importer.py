"""Import a Bill of Quantities from a CSV or Excel (.xlsx) file.

The whole file is validated first. If any row has a problem nothing is saved, and every
problem is reported with its row number, so the file can be fixed and uploaded again.
Existing BOQ items are never changed by an import (quantities change only through
variation orders), so an item code that already exists in the project is an error.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction

from boq.models import BOQItem
from inventory.models import Material, Unit

MAX_ROWS = 5000
MAX_BYTES = 5 * 1024 * 1024
MAX_ERRORS_SHOWN = 100

ALIASES = {
    "code": {"code", "item", "item code", "item no", "item number", "ref", "reference", "no"},
    "description": {"description", "item description", "details", "particulars", "work item"},
    "unit": {"unit", "uom", "unit of measure"},
    "quantity": {"quantity", "qty", "original quantity"},
    "rate": {"rate", "unit rate", "price", "unit price", "rate ugx"},
    "phase": {"phase", "component", "section", "work section"},
    "material": {"material", "material code", "linked material"},
}
REQUIRED = ["code", "description", "unit", "quantity", "rate"]


class ImportFileError(Exception):
    """The file itself cannot be read (wrong type, no header row, too large ...)."""


@dataclass
class ImportResult:
    dry_run: bool = False
    valid_rows: int = 0
    created: int = 0
    headings_skipped: int = 0
    units_created: int = 0
    errors: list = field(default_factory=list)
    more_errors: int = 0


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def read_rows(data, filename):
    """Return the file as a list of rows of strings."""
    if len(data) > MAX_BYTES:
        raise ImportFileError("The file is larger than 5 MB.")
    name = (filename or "").lower()
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]
    if name.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ImportFileError("Excel import needs the 'openpyxl' package (pip install openpyxl). "
                                  "You can also save the sheet as CSV and upload that.") from exc
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            sheet = wb.worksheets[0]
            rows = [["" if v is None else str(v).strip() for v in row] for row in sheet.iter_rows(values_only=True)]
            wb.close()
            return rows
        except Exception as exc:  # corrupt or password-protected workbook
            raise ImportFileError(f"The Excel file could not be read: {exc}") from exc
    raise ImportFileError("Upload a .csv or .xlsx file.")


def find_header(rows):
    for idx, row in enumerate(rows[:15]):
        cols = {}
        for i, cell in enumerate(row):
            n = _norm(cell)
            for key, names in ALIASES.items():
                if n in names and key not in cols:
                    cols[key] = i
        if all(k in cols for k in REQUIRED):
            return idx, cols
    raise ImportFileError(
        "Could not find the header row. The first rows must contain these column titles: "
        "code, description, unit, quantity, rate (phase and material are optional)."
    )


def parse_decimal(text):
    t = str(text).strip().replace(",", "").replace(" ", "")
    if t in ("", "-"):
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        raise ValueError(f"'{text}' is not a number")


def import_boq(data, filename, project, user, create_units=False, dry_run=False):
    rows = read_rows(data, filename)
    header_idx, cols = find_header(rows)
    body = rows[header_idx + 1:]
    if len(body) > MAX_ROWS:
        raise ImportFileError(f"The file has more than {MAX_ROWS} rows. Split it into smaller files.")

    units = {}
    for u in Unit.objects.all():
        units[u.abbreviation.lower()] = u
        units[u.name.lower()] = u
    phases = {p.name.lower(): p for p in project.phases.all()}
    materials = {m.code.lower(): m for m in Material.objects.all()}
    existing = set(BOQItem.objects.filter(project=project).values_list("code", flat=True))

    result = ImportResult(dry_run=dry_run)
    seen, prepared, new_units = set(), [], {}

    def err(row_no, message):
        if len(result.errors) < MAX_ERRORS_SHOWN:
            result.errors.append(f"Row {row_no}: {message}")
        else:
            result.more_errors += 1

    def cell(row, key):
        i = cols.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    for offset, row in enumerate(body):
        row_no = header_idx + 2 + offset
        if not any(c.strip() for c in row):
            continue
        code, desc = cell(row, "code"), cell(row, "description")
        qty_text, rate_text = cell(row, "quantity"), cell(row, "rate")
        if not qty_text and not rate_text:  # a heading or sub-total line
            result.headings_skipped += 1
            continue

        problems = []
        if not code:
            problems.append("item code is empty")
        elif code in existing:
            problems.append(f"item {code} already exists in this project")
        elif code in seen:
            problems.append(f"item {code} appears more than once in the file")
        if not desc:
            problems.append("description is empty")

        qty = rate = None
        try:
            qty = parse_decimal(qty_text)
            if qty is None:
                problems.append("quantity is empty")
            elif qty < 0 or qty >= Decimal("100000000000"):
                problems.append("quantity must be between 0 and 99,999,999,999")
            else:
                qty = qty.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        except ValueError as exc:
            problems.append(f"quantity: {exc}")
        try:
            rate = parse_decimal(rate_text)
            if rate is None:
                problems.append("rate is empty")
            elif rate < 0 or rate >= Decimal("100000000000000"):
                problems.append("rate must be between 0 and 99,999,999,999,999")
            else:
                rate = rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except ValueError as exc:
            problems.append(f"rate: {exc}")

        unit_text = cell(row, "unit")
        unit = units.get(unit_text.lower()) if unit_text else None
        if not unit_text:
            problems.append("unit is empty")
        elif unit is None:
            if create_units:
                unit = new_units.get(unit_text.lower())
                if unit is None:
                    unit = Unit(name=unit_text[:50], abbreviation=unit_text[:15])
                    new_units[unit_text.lower()] = unit
            else:
                problems.append(f"unit '{unit_text}' is not in the units list (tick 'create missing units' to add it)")

        phase = None
        phase_text = cell(row, "phase")
        if phase_text:
            phase = phases.get(phase_text.lower())
            if phase is None:
                problems.append(f"component '{phase_text}' does not exist in this project")

        material = None
        material_text = cell(row, "material")
        if material_text:
            material = materials.get(material_text.lower())
            if material is None:
                problems.append(f"material code '{material_text}' does not exist")

        if problems:
            err(row_no, "; ".join(problems))
            continue
        seen.add(code)
        prepared.append((code, desc[:300], unit, qty, rate, phase, material, unit_text))

    result.valid_rows = len(prepared)
    if result.errors or dry_run:
        return result
    if not prepared:
        raise ImportFileError("The file has no item rows to import.")

    with transaction.atomic():
        for u in new_units.values():
            u.save()
            units[u.abbreviation.lower()] = u
        result.units_created = len(new_units)
        items = []
        for code, desc, unit, qty, rate, phase, material, unit_text in prepared:
            if unit.pk is None:
                unit = new_units[unit_text.lower()]
            items.append(BOQItem(
                project=project, phase=phase, material=material, code=code, description=desc, unit=unit,
                original_quantity=qty, revised_quantity=qty, rate=rate, created_by=user, updated_by=user,
            ))
        BOQItem.objects.bulk_create(items, batch_size=500)
        result.created = len(items)
    return result


SAMPLE_CSV = (
    "code,description,unit,quantity,rate,phase,material\n"
    "1.1,Excavation to reduce levels,m3,850,18000,Excavation,\n"
    "2.1,Concrete grade 25 in foundations,m3,120,520000,Foundation,\n"
    "2.2,Cement for blinding (50kg bags),bag,300,38000,Foundation,CEM-TOR\n"
)

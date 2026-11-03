"""Turn report tables into an Excel download."""
import io
from decimal import Decimal

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEAD = PatternFill("solid", fgColor="1F3864")


def _cell(value):
    if isinstance(value, Decimal):
        return float(value)
    if value is None:
        return ""
    return value


def workbook_response(filename, title, sheets):
    """sheets: list of (sheet title, [header, ...], [[value, ...], ...]). Returns a download."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, headers, rows in sheets:
        ws = wb.create_sheet(name[:31])
        ws.append([title])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append([])
        ws.append(headers)
        for c in ws[3]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = HEAD
            c.alignment = Alignment(wrap_text=True, vertical="center")
        for row in rows:
            ws.append([_cell(v) for v in row])
        for i, h in enumerate(headers, start=1):
            longest = max([len(str(h))] + [len(str(_cell(r[i - 1]))) for r in rows if i - 1 < len(r)])
            ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 60)
        for row in ws.iter_rows(min_row=4):
            for c in row:
                if isinstance(c.value, float):
                    c.number_format = "#,##0.##"
        ws.freeze_panes = "A4"
    buf = io.BytesIO()
    wb.save(buf)
    response = HttpResponse(buf.getvalue(), content_type=XLSX)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

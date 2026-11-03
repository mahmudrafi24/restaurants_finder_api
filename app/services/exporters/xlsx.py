"""Styled Excel export.

Reproduces the formatting of the original ``python_cafe_scraper.py`` output — teal title banner,
dark header row, banded rows, frozen panes, auto-filter and hyperlinked Maps column — so files
downloaded from the web app are interchangeable with the ones the CLI produced.
"""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Final

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.services.exporters.base import COLUMN_HEADERS, row_values, workbook_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.schemas.place import Place

# ── Palette (unchanged from the CLI) ─────────────────────────────────────────
_INK: Final = "1A1A2E"
_ACCENT: Final = "00D4AA"
_BAND: Final = "F0F8FF"
_WHITE: Final = "FFFFFF"
_LINK: Final = "0563C1"
_GRID: Final = "CCCCCC"

_FONT_NAME: Final = "Arial"
_COLUMN_WIDTHS: Final[tuple[int, ...]] = (30, 14, 18, 40, 18, 30, 25, 12, 12, 45)
_MAPS_COLUMN: Final = 10
_FIRST_DATA_ROW: Final = 3


class XlsxExporter:
    """Renders places as a formatted ``.xlsx`` workbook."""

    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    extension = "xlsx"

    def render(self, places: Sequence[Place], *, city_name: str) -> bytes:
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — a new Workbook always has an active sheet
            worksheet = workbook.create_sheet()
        worksheet.title = "Results"

        thin = Side(style="thin", color=_GRID)
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        last_column = get_column_letter(len(COLUMN_HEADERS))

        # ── Title banner ──
        worksheet.merge_cells(f"A1:{last_column}1")
        title = worksheet["A1"]
        title.value = workbook_text(f"Cafes & Restaurants — {city_name}   ({len(places)} results)")
        title.data_type = "s"
        title.font = Font(bold=True, size=14, color=_INK, name=_FONT_NAME)
        title.fill = PatternFill("solid", fgColor=_ACCENT)
        title.alignment = Alignment(horizontal="center", vertical="center")
        worksheet.row_dimensions[1].height = 30

        # ── Header row ──
        header_font = Font(bold=True, color=_ACCENT, name=_FONT_NAME, size=11)
        header_fill = PatternFill("solid", fgColor=_INK)
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for index, (header, width) in enumerate(
            zip(COLUMN_HEADERS, _COLUMN_WIDTHS, strict=True), start=1
        ):
            cell = worksheet.cell(row=2, column=index, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = border
            worksheet.column_dimensions[get_column_letter(index)].width = width
        worksheet.row_dimensions[2].height = 22

        # ── Data rows ──
        body_font = Font(name=_FONT_NAME, size=10)
        link_font = Font(color=_LINK, underline="single", name=_FONT_NAME, size=10)
        body_alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        band_fill = PatternFill("solid", fgColor=_BAND)
        plain_fill = PatternFill("solid", fgColor=_WHITE)

        for offset, place in enumerate(places):
            row = _FIRST_DATA_ROW + offset
            fill = band_fill if row % 2 == 0 else plain_fill

            for column, value in enumerate(row_values(place), start=1):
                cell = worksheet.cell(row=row, column=column)
                cell.value = workbook_text(value) if isinstance(value, str) else value
                if isinstance(value, str):
                    cell.data_type = "s"
                cell.fill = fill
                cell.border = border
                cell.alignment = body_alignment
                cell.font = body_font

            maps_cell = worksheet.cell(row=row, column=_MAPS_COLUMN)
            maps_cell.hyperlink = place.maps_url
            maps_cell.value = "📍 Open in Maps"
            maps_cell.font = link_font

        # ── Navigation aids ──
        worksheet.freeze_panes = f"A{_FIRST_DATA_ROW}"
        if places:
            worksheet.auto_filter.ref = f"A2:{last_column}{len(places) + 2}"

        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

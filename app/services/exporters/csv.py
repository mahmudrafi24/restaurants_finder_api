"""CSV export.

Written with a UTF-8 BOM: Excel on Windows otherwise interprets a UTF-8 CSV as the local
codepage and mangles accented place names, which is a common complaint with scraped OSM data.
``\\r\\n`` line endings match the CSV spec (RFC 4180).
"""

from __future__ import annotations

import codecs
import csv
import io
from typing import TYPE_CHECKING

from app.services.exporters.base import COLUMN_HEADERS, csv_value, row_values

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.schemas.place import Place


class CsvExporter:
    """Renders places as a UTF-8 (BOM) CSV file."""

    media_type = "text/csv; charset=utf-8"
    extension = "csv"

    def render(self, places: Sequence[Place], *, city_name: str) -> bytes:  # noqa: ARG002
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
        writer.writerow(COLUMN_HEADERS)
        writer.writerows(tuple(csv_value(value) for value in row_values(place)) for place in places)
        return codecs.BOM_UTF8 + buffer.getvalue().encode("utf-8")

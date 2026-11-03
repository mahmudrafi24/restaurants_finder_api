"""Exporter protocol and registry.

Adding a format means writing one :class:`Exporter` and registering it — no route changes. The
registry is the extensibility seam referenced by :mod:`app.api.v1.routes.places`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core.errors import UnsupportedFormatError
from app.schemas.place import ExportFormat

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.schemas.place import Place

#: Column order shared by every tabular exporter, matching the original CLI's spreadsheet so
#: existing downstream consumers keep working.
COLUMN_HEADERS: tuple[str, ...] = (
    "Name",
    "Type",
    "Cuisine",
    "Address",
    "Phone",
    "Website",
    "Opening Hours",
    "Latitude",
    "Longitude",
    "Google Maps",
)

#: Placeholder for missing values, matching the CLI's output.
EMPTY = "N/A"
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def workbook_text(value: str) -> str:
    """Remove text XML cannot represent, including isolated JSON surrogate escapes."""
    return _XML_ILLEGAL.sub("", value)


def csv_value(value: str | float) -> str | float:
    """Keep untrusted text literal in spreadsheet readers (including phone numbers)."""
    if isinstance(value, str):
        probe = re.sub(r"^[\s\x00-\x20]*", "", value)
        if probe.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
    return value


@runtime_checkable
class Exporter(Protocol):
    """Renders places into a downloadable byte payload."""

    #: MIME type sent in the ``Content-Type`` header.
    media_type: str
    #: Filename extension, without a leading dot.
    extension: str

    def render(self, places: Sequence[Place], *, city_name: str) -> bytes:
        """Return the complete file contents."""
        ...


def row_values(place: Place) -> tuple[str | float, ...]:
    """Flatten a place into :data:`COLUMN_HEADERS` order."""
    return (
        place.name,
        place.amenity.label,
        ", ".join(part.replace("_", " ").title() for part in place.cuisine) or EMPTY,
        place.address.formatted or EMPTY,
        place.phone or EMPTY,
        place.website or EMPTY,
        place.opening_hours or EMPTY,
        place.latitude,
        place.longitude,
        place.maps_url,
    )


def build_filename(city_name: str, extension: str) -> str:
    """Build a safe download filename, e.g. ``hamburg_cafes_restaurants.xlsx``.

    Mirrors the CLI's naming so downloads are recognisable alongside earlier exports.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", city_name.casefold()).strip("_") or "results"
    return f"{slug}_cafes_restaurants.{extension}"


def get_exporter(export_format: ExportFormat) -> Exporter:
    """Resolve an :class:`Exporter` for the requested format.

    Raises:
        UnsupportedFormatError: No exporter is registered for the format.
    """
    # Imported here so each exporter module can import helpers from this one without a cycle.
    from app.services.exporters.csv import CsvExporter
    from app.services.exporters.geojson import GeoJsonExporter
    from app.services.exporters.xlsx import XlsxExporter

    registry: dict[ExportFormat, Exporter] = {
        ExportFormat.XLSX: XlsxExporter(),
        ExportFormat.CSV: CsvExporter(),
        ExportFormat.GEOJSON: GeoJsonExporter(),
    }

    exporter = registry.get(export_format)
    if exporter is None:  # pragma: no cover — unreachable while the enum and registry agree
        raise UnsupportedFormatError(str(export_format), [fmt.value for fmt in registry])
    return exporter

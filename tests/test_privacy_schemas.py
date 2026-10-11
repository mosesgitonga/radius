"""No API response may expose precise location.

Section 6.4 of the brief makes this non-negotiable: a response must never carry
another user's coordinates, centroid, fine-grained cell ID, or an exact distance.
Exact distances are included because they are coordinates in disguise — spoof
your own position three times, measure the distance each time, and you have
trilaterated someone's home. Area labels and distance buckets exist so that
cannot be done.

Rather than trusting review, this walks the generated OpenAPI document, collects
every schema reachable from a **response**, and fails if any of them declares a
forbidden field. Request schemas are deliberately exempt: `POST /v1/me/location`
has to accept a latitude and longitude — the rule is that the server converts
them to an H3 cell on arrival and never gives them back.

The test is in place from Phase 0, before there is any location field to catch,
so that the phase which adds one cannot add it quietly.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

# Field names that must never appear in a response model.
FORBIDDEN_RESPONSE_FIELDS: frozenset[str] = frozenset(
    {
        # Raw coordinates
        "lat",
        "latitude",
        "lon",
        "lng",
        "longitude",
        "coords",
        "coordinates",
        "point",
        "geog",
        "geom",
        "geometry",
        "centroid",
        "location_point",
        "raw_location",
        "exact_location",
        # H3 cells. Resolution 9 spans a few hundred metres, which is a house.
        # Coarser parents are only ever exposed through an area label, never as
        # an ID a client could resolve back to a place.
        "h3",
        "h3_cell",
        "h3_index",
        "h3_res9",
        "h3_res8",
        "h3_res7",
        "cell",
        "cell_id",
        "res9",
        "res8",
        "res7",
        # Exact distances enable trilateration. Buckets are the public form.
        "distance",
        "distance_m",
        "distance_km",
        "distance_metres",
        "distance_meters",
        "exact_distance",
    }
)

# Deliberate, reviewed exceptions, as "SchemaName.field_name". Each one needs a
# comment saying why it cannot leak a user's position. Expected members later:
# admin-only area polygon management, and a private event's venue returned to an
# accepted attendee.
ALLOWED_EXCEPTIONS: frozenset[str] = frozenset()


def _resolve_reachable(node: Any, schemas: dict[str, Any], found: set[str]) -> None:
    """Collect the names of every schema reachable from `node` by `$ref`."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name not in found:
                found.add(name)
                _resolve_reachable(schemas.get(name, {}), schemas, found)
        for value in node.values():
            _resolve_reachable(value, schemas, found)
    elif isinstance(node, list):
        for value in node:
            _resolve_reachable(value, schemas, found)


def _declared_field_names(node: Any, found: set[str]) -> None:
    """Collect property names declared anywhere in a schema, including inline
    nested objects, `anyOf` branches, and array item schemas."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            found.update(str(key) for key in properties)
        for value in node.values():
            _declared_field_names(value, found)
    elif isinstance(node, list):
        for value in node:
            _declared_field_names(value, found)


def response_schema_fields(app: FastAPI) -> dict[str, set[str]]:
    """Map each response-reachable schema name to the fields it declares."""
    document = app.openapi()
    schemas: dict[str, Any] = document.get("components", {}).get("schemas", {})

    reachable: set[str] = set()
    for operations in document.get("paths", {}).values():
        for operation in operations.values():
            if isinstance(operation, dict) and "responses" in operation:
                _resolve_reachable(operation["responses"], schemas, reachable)

    result: dict[str, set[str]] = {}
    for name in reachable:
        fields: set[str] = set()
        _declared_field_names(schemas.get(name, {}), fields)
        result[name] = fields
    return result


def test_no_response_schema_exposes_precise_location(app: FastAPI) -> None:
    violations = sorted(
        f"{schema}.{field}"
        for schema, fields in response_schema_fields(app).items()
        for field in fields
        if field.lower() in FORBIDDEN_RESPONSE_FIELDS
        and f"{schema}.{field}" not in ALLOWED_EXCEPTIONS
    )

    assert not violations, (
        "These response fields would expose precise location:\n  "
        + "\n  ".join(violations)
        + "\n\nResponses describe places with an area label and a distance bucket. "
        "If an exception is genuinely required, add it to ALLOWED_EXCEPTIONS with "
        "a comment explaining why it cannot identify where someone lives."
    )


def test_the_scan_actually_inspects_something(app: FastAPI) -> None:
    """Guards against the check passing because it found nothing to check — a
    refactor of the OpenAPI shape must not silently disarm it."""
    fields = response_schema_fields(app)

    assert fields
    assert any(values for values in fields.values())


def test_the_scan_catches_a_violation(app: FastAPI) -> None:
    """Proves the check can fail. A privacy test nobody has seen fail is a
    privacy test nobody should trust."""

    class LeakyNeighbour(BaseModel):
        display_name: str
        latitude: float
        longitude: float

    @app.get("/v1/test/leaky", response_model=LeakyNeighbour)
    async def leaky() -> LeakyNeighbour:  # pragma: no cover — never called
        raise NotImplementedError

    app.openapi_schema = None  # force regeneration now the route exists

    leaked = {
        field
        for fields in response_schema_fields(app).values()
        for field in fields
        if field.lower() in FORBIDDEN_RESPONSE_FIELDS
    }

    assert leaked == {"latitude", "longitude"}


def test_nested_response_models_are_inspected(app: FastAPI) -> None:
    """Most leaks will arrive nested — an author summary inside a post, say —
    not as a top-level field."""

    class Inner(BaseModel):
        centroid: str

    class Outer(BaseModel):
        author: Inner

    @app.get("/v1/test/nested", response_model=Outer)
    async def nested() -> Outer:  # pragma: no cover — never called
        raise NotImplementedError

    app.openapi_schema = None

    leaked = {
        field
        for fields in response_schema_fields(app).values()
        for field in fields
        if field.lower() in FORBIDDEN_RESPONSE_FIELDS
    }

    assert "centroid" in leaked


@pytest.mark.parametrize(
    "field",
    ["area_label", "distance_bucket", "approximate_area", "town", "radius_km"],
)
def test_the_approved_location_vocabulary_is_allowed(field: str) -> None:
    """The fields responses *should* use must not be caught by the denylist.

    `radius_km` is the viewer's own radius, which `GET /v1/me/radius` returns by
    design; it says nothing about anyone else's position.
    """
    assert field.lower() not in FORBIDDEN_RESPONSE_FIELDS

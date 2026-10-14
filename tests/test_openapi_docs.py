"""Every endpoint is documented.

The API is consumed by a separate client team through `/docs`, so an endpoint with
no description is an endpoint someone has to read the server code to use. Checked
by hand until Phase 3, when four new endpoints turned out to be missing one.
"""

from __future__ import annotations

from fastapi import FastAPI

METHODS = {"get", "post", "put", "patch", "delete"}


def test_every_operation_has_a_summary_and_a_description(app: FastAPI) -> None:
    spec = app.openapi()
    undocumented = [
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if method in METHODS and not (operation.get("summary") and operation.get("description"))
    ]
    assert undocumented == []


def test_every_operation_has_a_tag_that_is_described(app: FastAPI) -> None:
    spec = app.openapi()
    described = {tag["name"] for tag in spec.get("tags", [])}
    used = {
        tag
        for operations in spec["paths"].values()
        for method, operation in operations.items()
        if method in METHODS
        for tag in operation.get("tags", [])
    }
    assert used <= described

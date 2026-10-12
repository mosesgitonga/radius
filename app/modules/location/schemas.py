"""Request and response models for location.

The asymmetry here is the whole privacy design, so it is worth stating plainly:
**requests carry coordinates and responses never do.** `LocationIn` has a
latitude and a longitude because a phone has to send them somehow. Nothing that
goes back out has either, because by then the coordinates have been converted to
a cell and discarded.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import RadiusFeature
from app.modules.users.schemas import AreaLabelOut


class LocationIn(BaseModel):
    """Coordinates from the device.

    Converted to an H3 cell on arrival and then discarded: they are not stored,
    not logged, and not returned. This is the only model in the system that holds
    them at all.
    """

    latitude: float = Field(ge=-90, le=90, examples=[-0.4201])
    longitude: float = Field(ge=-180, le=180, examples=[36.9476])
    accuracy_metres: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Device-reported accuracy, if known. Accepted so clients need not "
            "strip it, and currently unused: the cell is already far coarser than "
            "any phone's error."
        ),
    )


class HomeLocationOut(BaseModel):
    """The result of setting a home area."""

    area_label: AreaLabelOut
    is_first_time: bool = Field(
        description="True when this is the first home area the account has had."
    )
    next_change_allowed_at: datetime | None = Field(
        default=None,
        description=(
            "When the home area may next be changed. The cooldown stops someone "
            "hopping between neighbourhoods to spam each in turn."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "area_label": {"label": "Kamakwa", "precision": "neighbourhood"},
                "is_first_time": True,
                "next_change_allowed_at": None,
            }
        }
    )


class ExploreOut(BaseModel):
    """An active explore session.

    Held in Redis for about two hours and never written to the database. Other
    people continue to see the account's home area, and anything posted while
    exploring is still attributed to home.
    """

    area_label: AreaLabelOut
    expires_at: datetime
    note: str = Field(
        default=(
            "Explore mode is temporary and private. Your posts still go to your "
            "home area, and other people only ever see your home area."
        )
    )


class RadiusOut(BaseModel):
    """One feature's current radius."""

    feature: RadiusFeature
    radius_km: float = Field(
        description=(
            "Rounded to the nearest half kilometre. Rounded rather than exact "
            "because a precise radius is a direct read-out of how dense your own "
            "street is."
        ),
        examples=[3.0],
    )
    user_filter_max_km: float | None = Field(
        default=None,
        description=(
            "How far you may widen this feature with a distance filter. Null means "
            "the feature has no filter — the feed deliberately has none, because "
            "'show me everything' is how a local feed stops being local."
        ),
    )
    at_maximum: bool = Field(
        description=(
            "True when the radius is capped. Expected in quiet areas: there simply "
            "are not enough active neighbours nearby to narrow it."
        )
    )
    at_minimum: bool = Field(description="True when the area is dense enough to hit the floor.")


class MyRadiusOut(BaseModel):
    """Every feature's radius, plus what it was measured from."""

    anchor: str = Field(
        description=("`home` normally, or `explore` while an explore session is active."),
        examples=["home"],
    )
    has_location: bool = Field(
        description="False until a home location is set; radii are then defaults."
    )
    active_neighbours_estimate: int = Field(
        description=(
            "Roughly how many active neighbours the feed radius covers. Rounded, "
            "and about the area rather than about any individual."
        )
    )
    radii: list[RadiusOut]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "anchor": "home",
                "has_location": True,
                "active_neighbours_estimate": 500,
                "radii": [
                    {
                        "feature": "feed",
                        "radius_km": 3.0,
                        "user_filter_max_km": None,
                        "at_maximum": False,
                        "at_minimum": False,
                    }
                ],
            }
        }
    )


class AreaOut(BaseModel):
    """A seeded area, for pickers and filters.

    No boundary geometry. The polygons are not secret, but shipping them to every
    client would be a large payload on expensive data for no feature that needs
    it — and a boundary plus a label is a sharper location than a label alone.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str
    town: str | None = None
    county: str | None = None

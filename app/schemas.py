"""Pydantic request/response models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExtractRequest(BaseModel):
    url: str = Field(
        ...,
        min_length=5,
        max_length=2000,
        description="A Google Maps place link (google.com/maps/... or maps.app.goo.gl/...)",
        examples=["https://maps.app.goo.gl/xxxxxxxx"],
    )

    @field_validator("url")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class BusinessOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str | None = None
    website: str | None = None
    has_website: bool = False
    email: str | None = None
    emails_found: list[str] = Field(default_factory=list)
    address: str | None = None
    category: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    place_id: str | None = None
    map_url: str
    source_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("emails_found", mode="before")
    @classmethod
    def _none_to_list(cls, value: list[str] | None) -> list[str]:
        return value or []


class ExtractResponse(BaseModel):
    created: bool = Field(description="True when a new row was inserted, False when updated")
    duration_seconds: float
    business: BusinessOut


class BusinessList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[BusinessOut]


class Stats(BaseModel):
    total: int
    with_website: int
    without_website: int
    with_email: int
    with_phone: int

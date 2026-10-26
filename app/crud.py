"""Database queries for the businesses table."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .extractor import ExtractionResult
from .models import Business


def _payload(result: ExtractionResult) -> dict:
    return {
        "name": result.name,
        "phone": result.phone,
        "website": result.website,
        "has_website": result.has_website,
        "email": result.email,
        "emails_found": result.emails_found or [],
        "address": result.address,
        "category": result.category,
        "rating": result.rating,
        "reviews_count": result.reviews_count,
        "latitude": result.latitude,
        "longitude": result.longitude,
        "place_id": result.place_id,
        "map_url": result.map_url,
        "source_url": result.source_url,
        "raw_data": result.raw or {},
    }


def _find_existing(db: Session, result: ExtractionResult) -> Business | None:
    """Deduplicate on Google's place id first, then on the cleaned map URL."""
    if result.place_id:
        found = db.scalar(select(Business).where(Business.place_id == result.place_id))
        if found:
            return found
    return db.scalar(select(Business).where(Business.map_url == result.map_url))


def upsert_business(db: Session, result: ExtractionResult) -> tuple[Business, bool]:
    """Insert the business or refresh the existing row.

    Returns ``(business, created)``.
    """
    existing = _find_existing(db, result)

    if existing:
        for key, value in _payload(result).items():
            setattr(existing, key, value)
        db.commit()
        db.refresh(existing)
        return existing, False

    business = Business(**_payload(result))
    db.add(business)
    try:
        db.commit()
    except IntegrityError:
        # Another request inserted the same place between our SELECT and INSERT.
        db.rollback()
        existing = _find_existing(db, result)
        if not existing:
            raise
        for key, value in _payload(result).items():
            setattr(existing, key, value)
        db.commit()
        db.refresh(existing)
        return existing, False

    db.refresh(business)
    return business, True


def get_business(db: Session, business_id: int) -> Business | None:
    return db.get(Business, business_id)


def list_businesses(
    db: Session,
    *,
    query: str | None = None,
    has_website: bool | None = None,
    has_email: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[Business]]:
    statement = select(Business)

    if query:
        pattern = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                Business.name.ilike(pattern),
                Business.phone.ilike(pattern),
                Business.email.ilike(pattern),
                Business.website.ilike(pattern),
                Business.address.ilike(pattern),
                Business.category.ilike(pattern),
            )
        )
    if has_website is True:
        statement = statement.where(Business.has_website.is_(True))
    elif has_website is False:
        statement = statement.where(Business.has_website.is_(False))
    if has_email is True:
        statement = statement.where(Business.email.is_not(None))
    elif has_email is False:
        statement = statement.where(Business.email.is_(None))

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = list(
        db.scalars(
            statement.order_by(Business.created_at.desc(), Business.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return total, rows


def stats(db: Session) -> dict:
    row = db.execute(
        select(
            func.count(Business.id),
            func.count(Business.id).filter(Business.has_website.is_(True)),
            func.count(Business.id).filter(Business.has_website.is_(False)),
            func.count(Business.id).filter(Business.email.is_not(None)),
            func.count(Business.id).filter(Business.phone.is_not(None)),
        )
    ).one()
    return {
        "total": row[0] or 0,
        "with_website": row[1] or 0,
        "without_website": row[2] or 0,
        "with_email": row[3] or 0,
        "with_phone": row[4] or 0,
    }


def delete_business(db: Session, business_id: int) -> bool:
    business = db.get(Business, business_id)
    if not business:
        return False
    db.delete(business)
    db.commit()
    return True

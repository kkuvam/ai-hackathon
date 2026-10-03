from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import get_current_insurer
from app.database import get_db
from app.model import tier_to_multiplier
from app.models import Driver, Event, Trip, TripScore
from app.schemas import (
    InsurerDriverDetailResponse,
    InsurerDriverItem,
    InsurerOverviewResponse,
    TripListItem,
)
from app.services.scoring import (
    PASSENGER_SHARE_FLAG,
    calculate_driver_score,
    calculate_passenger_stats,
    is_expired_unknown,
    is_scoreable,
)

router = APIRouter()


@router.get("/insurer/overview", response_model=InsurerOverviewResponse)
def get_insurer_overview(
    _: None = Depends(get_current_insurer),
    db: Session = Depends(get_db),
):
    cutoff = datetime.now(UTC) - timedelta(days=90)

    total_drivers = db.query(func.count(Driver.id)).scalar()

    # Only scoreable trips count toward trip totals and the tier distribution
    rows = (
        db.query(Trip, TripScore)
        .join(TripScore, Trip.id == TripScore.trip_id)
        .filter(Trip.created_at >= cutoff)
        .filter(Trip.status == "done")
        .all()
    )
    scoreable = [(t, s) for t, s in rows if is_scoreable(t)]
    total_trips = len(scoreable)

    tier_distribution = {}
    for _, score in scoreable:
        tier_distribution[score.tier] = tier_distribution.get(score.tier, 0) + 1

    # Average multiplier weighted by the number of trips in each tier
    total_scored = sum(tier_distribution.values())
    average_multiplier = (
        sum(tier_to_multiplier(tier) * count for tier, count in tier_distribution.items())
        / total_scored
        if total_scored > 0
        else 1.0
    )

    return InsurerOverviewResponse(
        total_drivers=total_drivers,
        total_trips_90d=total_trips,
        tier_distribution=tier_distribution,
        average_multiplier=round(average_multiplier, 2),
    )


@router.get("/insurer/drivers", response_model=list[InsurerDriverItem])
def get_insurer_drivers(
    _: None = Depends(get_current_insurer),
    db: Session = Depends(get_db),
    tier: str | None = Query(None),
    sort: str | None = Query("score_desc"),
):
    drivers = db.query(Driver).all()
    items = []

    for driver in drivers:
        score, confidence, tier_val, multiplier, _trend, total_trips, total_distance = (
            calculate_driver_score(db, driver.id)
        )
        if tier and tier_val != tier:
            continue
        items.append(
            InsurerDriverItem(
                driver_id=driver.id,
                score=score,
                confidence=confidence,
                tier=tier_val,
                premium_multiplier=multiplier,
                total_trips_90d=total_trips,
                total_distance_km_90d=total_distance,
            )
        )

    if sort == "score_desc":
        items.sort(key=lambda x: x.score, reverse=True)
    elif sort == "score_asc":
        items.sort(key=lambda x: x.score)
    elif sort == "multiplier_desc":
        items.sort(key=lambda x: x.premium_multiplier, reverse=True)
    elif sort == "multiplier_asc":
        items.sort(key=lambda x: x.premium_multiplier)

    return items


@router.get("/insurer/drivers/{driver_id}", response_model=InsurerDriverDetailResponse)
def get_insurer_driver_detail(
    driver_id: str,
    _: None = Depends(get_current_insurer),
    db: Session = Depends(get_db),
):
    driver = db.query(Driver).filter(Driver.id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    score, confidence, tier, multiplier, _trend, _total_trips, _total_distance = (
        calculate_driver_score(db, driver.id)
    )
    passenger_stats = calculate_passenger_stats(db, driver.id)

    cutoff = datetime.now(UTC) - timedelta(days=90)
    events = (
        db.query(Event.type, func.count(Event.id))
        .join(Trip, Trip.id == Event.trip_id)
        .filter(Trip.driver_id == driver_id)
        .filter(Trip.created_at >= cutoff)
        .group_by(Event.type)
        .all()
    )
    event_rates = {event_type: count for event_type, count in events}

    trips = (
        db.query(Trip, TripScore)
        .outerjoin(TripScore, Trip.id == TripScore.trip_id)
        .filter(Trip.driver_id == driver_id)
        .order_by(Trip.started_at.desc())
        .limit(20)
        .all()
    )
    trip_items = [
        TripListItem(
            trip_id=t.id,
            started_at=t.started_at,
            distance_km=t.features.features.get("distance_km", 0) if t.features else 0,
            score=s.score if s and is_scoreable(t) else None,
            tier=s.tier if s and is_scoreable(t) else None,
            trip_type=t.trip_type,
            needs_confirmation=(
                t.trip_type == "unknown" and t.label_source != "user" and not is_expired_unknown(t)
            ),
            label_source=t.label_source,
            transit_line=t.transit_line,
        )
        for t, s in trips
    ]

    model_version = "unknown"
    if trips and trips[0][1]:
        model_version = trips[0][1].model_version

    return InsurerDriverDetailResponse(
        driver_id=driver.id,
        score=score,
        confidence=confidence,
        tier=tier,
        premium_multiplier=multiplier,
        event_rates=event_rates,
        trips=trip_items,
        model_version=model_version,
        passenger_share=passenger_stats["passenger_share"],
        label_sources=passenger_stats["label_sources"],
        flagged_for_review=passenger_stats["passenger_share"] > PASSENGER_SHARE_FLAG,
    )

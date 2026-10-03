from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import get_current_driver
from app.database import get_db
from app.models import Driver, Trip, TripScore
from app.pipeline import process_trip
from app.schemas import (
    DriverSummaryResponse,
    EventResponse,
    RoutePoint,
    TripDetailResponse,
    TripLabelRequest,
    TripLabelResponse,
    TripListItem,
)
from app.services.scoring import (
    calculate_driver_score,
    generate_explanation,
    is_expired_unknown,
    is_scoreable,
)

router = APIRouter()


@router.get("/me/summary", response_model=DriverSummaryResponse)
def get_my_summary(
    driver: Driver = Depends(get_current_driver),
    db: Session = Depends(get_db),
):
    score, confidence, tier, multiplier, trend, total_trips, total_distance = (
        calculate_driver_score(db, driver.id)
    )
    return DriverSummaryResponse(
        driver_id=driver.id,
        score=score,
        confidence=confidence,
        tier=tier,
        premium_multiplier=multiplier,
        trend=trend,
        total_trips_90d=total_trips,
        total_distance_km_90d=total_distance,
    )


@router.get("/me/trips", response_model=list[TripListItem])
def get_my_trips(
    driver: Driver = Depends(get_current_driver),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    trips = (
        db.query(Trip, TripScore)
        .outerjoin(TripScore, Trip.id == TripScore.trip_id)
        .filter(Trip.driver_id == driver.id)
        .order_by(Trip.started_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = []
    for trip, trip_score in trips:
        # Unconfirmed trips do not count, so their score is not shown yet
        score = trip_score if is_scoreable(trip) else None
        needs_confirmation = (
            trip.trip_type == "unknown"
            and trip.label_source != "user"
            and not is_expired_unknown(trip)
        )
        items.append(
            TripListItem(
                trip_id=trip.id,
                started_at=trip.started_at,
                distance_km=trip.features.features.get("distance_km", 0) if trip.features else 0,
                score=score.score if score else None,
                tier=score.tier if score else None,
                trip_type=trip.trip_type,
                needs_confirmation=needs_confirmation,
                label_source=trip.label_source,
                transit_line=trip.transit_line,
            )
        )
    return items


@router.post("/me/trips/{trip_id}/label", response_model=TripLabelResponse)
def label_trip(
    trip_id: str,
    request: TripLabelRequest,
    background_tasks: BackgroundTasks,
    driver: Driver = Depends(get_current_driver),
    db: Session = Depends(get_db),
):
    # Row lock: a second label request waits here and then sees status "processing"
    trip = (
        db.query(Trip)
        .filter(Trip.id == trip_id, Trip.driver_id == driver.id)
        .with_for_update()
        .first()
    )
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    trip.trip_type = request.trip_type
    trip.label_source = "user"

    status = "relabelled"
    if request.trip_type == "passenger":
        status = "removed_from_score"
    elif request.trip_type == "driver":
        status = "added_to_score"
    # An unscored, finished trip labelled as driver needs processing to produce a score.
    # Trips still uploading/processing pick up the user label when they run.
    needs_run = (
        request.trip_type == "driver" and trip.score is None and trip.status in ("done", "failed")
    )
    if needs_run:
        if trip.features is not None:
            db.delete(trip.features)
        trip.status = "processing"
    db.commit()
    if needs_run:
        background_tasks.add_task(process_trip, trip_id)

    return TripLabelResponse(
        trip_id=trip.id,
        trip_type=request.trip_type,
        status=status,
    )


@router.get("/me/trips/{trip_id}", response_model=TripDetailResponse)
def get_my_trip_detail(
    trip_id: str,
    driver: Driver = Depends(get_current_driver),
    db: Session = Depends(get_db),
):
    trip = db.query(Trip).filter(Trip.id == trip_id, Trip.driver_id == driver.id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    score = trip.score if is_scoreable(trip) else None
    events = trip.events
    features = trip.features.features if trip.features else {}

    route = []
    if trip.features and "route" in features:
        route_points = features["route"]
        step = max(1, len(route_points) // 100)
        route = [RoutePoint(**p) for p in route_points[::step]]

    explanation = generate_explanation(events, features) if score else None

    return TripDetailResponse(
        trip_id=trip.id,
        started_at=trip.started_at,
        ended_at=trip.ended_at,
        distance_km=features.get("distance_km", 0),
        duration_min=features.get("duration_min", 0),
        score=score.score if score else None,
        confidence=score.confidence if score else None,
        tier=score.tier if score else None,
        events=[
            EventResponse(
                type=e.type,
                time=e.time,
                peak_g=e.peak_g,
                lat=e.lat,
                lon=e.lon,
            )
            for e in events
        ],
        route=route,
        explanation=explanation,
    )

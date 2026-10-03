import unittest.mock
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Incident, Trip

client = TestClient(app)


def _register_and_consent():
    response = client.post("/v1/drivers/register", json={})
    data = response.json()
    driver_id = data["driver_id"]
    api_key = data["api_key"]
    client.post(
        "/v1/consent",
        headers={"X-API-Key": api_key},
        json={"version": "1.0"},
    )
    return driver_id, api_key


def _upload_trip(api_key, chunks):
    response = client.post("/v1/trips/start", headers={"X-API-Key": api_key})
    trip_id = response.json()["trip_id"]
    for chunk in chunks:
        client.post(
            f"/v1/trips/{trip_id}/chunks",
            headers={"X-API-Key": api_key},
            json=chunk,
        )
    return trip_id


def test_label_trip_as_passenger_removes_from_score():
    """A passenger label should remove the trip from the driver score."""
    _driver_id, api_key = _register_and_consent()

    # Upload a trip
    chunks = [
        {
            "seq": 0,
            "imu": [
                {
                    "t": 1759986000000 + i * 20,
                    "ax": 0.1,
                    "ay": 0.2,
                    "az": 9.8,
                    "gx": 0.01,
                    "gy": 0.01,
                    "gz": 0.02,
                }
                for i in range(100)
            ],
            "gps": [
                {
                    "t": 1759986000000 + i * 1000,
                    "lat": 22.50 + i * 0.005,
                    "lon": 114.30 + i * 0.005,
                    "speed": 15.0,
                }
                for i in range(60)
            ],
        }
    ]
    trip_id = _upload_trip(api_key, chunks)

    # Label as passenger
    response = client.post(
        f"/v1/me/trips/{trip_id}/label",
        headers={"X-API-Key": api_key},
        json={"trip_type": "passenger"},
    )
    assert response.status_code == 200
    assert response.json()["trip_type"] == "passenger"
    assert response.json()["status"] == "removed_from_score"


def test_label_trip_as_driver_adds_to_score():
    """A driver label should add the trip to the driver score."""
    _driver_id, api_key = _register_and_consent()

    chunks = [
        {
            "seq": 0,
            "imu": [
                {
                    "t": 1759986000000 + i * 20,
                    "ax": 0.1,
                    "ay": 0.2,
                    "az": 9.8,
                    "gx": 0.01,
                    "gy": 0.01,
                    "gz": 0.02,
                }
                for i in range(100)
            ],
            "gps": [
                {
                    "t": 1759986000000 + i * 1000,
                    "lat": 22.50 + i * 0.005,
                    "lon": 114.30 + i * 0.005,
                    "speed": 15.0,
                }
                for i in range(60)
            ],
        }
    ]
    trip_id = _upload_trip(api_key, chunks)

    # While the trip is still processing, the label is stored and picked up by
    # that run; no second pipeline run is scheduled
    with unittest.mock.patch("app.routers.driver.BackgroundTasks.add_task") as mock_add_task:
        response = client.post(
            f"/v1/me/trips/{trip_id}/label",
            headers={"X-API-Key": api_key},
            json={"trip_type": "driver"},
        )
        assert response.status_code == 200
        mock_add_task.assert_not_called()

    # Force the status to "done" so the trip counts as finished without a score
    db = SessionLocal()
    trip = db.query(Trip).filter(Trip.id == trip_id).first()
    assert trip is not None
    trip.status = "done"
    db.commit()
    db.close()

    # Labelling an unscored, finished trip as driver schedules processing
    with unittest.mock.patch("app.routers.driver.BackgroundTasks.add_task") as mock_add_task:
        response = client.post(
            f"/v1/me/trips/{trip_id}/label",
            headers={"X-API-Key": api_key},
            json={"trip_type": "driver"},
        )
        assert response.status_code == 200
        assert response.json()["trip_type"] == "driver"
        assert response.json()["status"] == "added_to_score"
        mock_add_task.assert_called_once()


def test_trip_list_includes_trip_type():
    """Trip list should include trip_type and needs_confirmation."""
    _driver_id, api_key = _register_and_consent()

    chunks = [
        {
            "seq": 0,
            "imu": [
                {
                    "t": 1759986000000 + i * 20,
                    "ax": 0.1,
                    "ay": 0.2,
                    "az": 9.8,
                    "gx": 0.01,
                    "gy": 0.01,
                    "gz": 0.02,
                }
                for i in range(100)
            ],
            "gps": [
                {
                    "t": 1759986000000 + i * 1000,
                    "lat": 22.50 + i * 0.005,
                    "lon": 114.30 + i * 0.005,
                    "speed": 15.0,
                }
                for i in range(60)
            ],
        }
    ]
    _upload_trip(api_key, chunks)

    response = client.get("/v1/me/trips", headers={"X-API-Key": api_key})
    assert response.status_code == 200
    trips = response.json()
    assert len(trips) >= 1
    assert "trip_type" in trips[0]
    assert "needs_confirmation" in trips[0]


def test_register_with_emergency_contact():
    """Registration should accept emergency contact info."""
    response = client.post(
        "/v1/drivers/register",
        json={
            "emergency_contact_name": "Mom",
            "emergency_contact_phone": "+852 9123 4567",
        },
    )
    assert response.status_code == 200
    assert "driver_id" in response.json()


def test_create_and_confirm_incident():
    """Should be able to create and confirm an incident."""
    _driver_id, api_key = _register_and_consent()

    response = client.post(
        "/v1/me/incidents",
        headers={"X-API-Key": api_key},
        json={
            "type": "crash",
            "time": datetime.now(UTC).isoformat(),
            "lat": 22.3193,
            "lon": 114.1694,
            "peak_g": 5.2,
        },
    )
    assert response.status_code == 200
    incident_id = response.json()["id"]

    response = client.post(
        f"/v1/me/incidents/{incident_id}/confirm",
        headers={"X-API-Key": api_key},
        json={"confirmed": "ok"},
    )
    assert response.status_code == 200
    assert response.json()["confirmed"] == "ok"


def _incident_body(trip_id=None):
    body = {
        "type": "crash",
        "time": datetime.now(UTC).isoformat(),
        "lat": 22.3193,
        "lon": 114.1694,
        "peak_g": 5.2,
    }
    if trip_id is not None:
        body["trip_id"] = trip_id
    return body


def test_incident_with_own_trip_id_ok():
    _driver_id, api_key = _register_and_consent()
    trip_id = _upload_trip(api_key, [])

    response = client.post(
        "/v1/me/incidents", headers={"X-API-Key": api_key}, json=_incident_body(trip_id)
    )
    assert response.status_code == 200


def test_incident_without_trip_id_ok():
    _driver_id, api_key = _register_and_consent()

    response = client.post(
        "/v1/me/incidents", headers={"X-API-Key": api_key}, json=_incident_body()
    )
    assert response.status_code == 200


def test_incident_with_other_drivers_trip_id_404():
    _a_id, key_a = _register_and_consent()
    _b_id, key_b = _register_and_consent()
    trip_b = _upload_trip(key_b, [])

    response = client.post(
        "/v1/me/incidents", headers={"X-API-Key": key_a}, json=_incident_body(trip_b)
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Trip not found"
    with SessionLocal() as db:
        assert db.query(Incident).count() == 0


def test_incident_naive_time_treated_as_utc():
    _driver_id, api_key = _register_and_consent()

    response = client.post(
        "/v1/me/incidents",
        headers={"X-API-Key": api_key},
        json={"type": "crash", "time": "2026-01-01T00:00:00"},
    )
    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["time"]) == datetime(2026, 1, 1, tzinfo=UTC)


def test_insurer_detail_includes_passenger_share():
    """Insurer driver detail should include passenger share and label sources."""
    driver_id, api_key = _register_and_consent()

    chunks = [
        {
            "seq": 0,
            "imu": [
                {
                    "t": 1759986000000 + i * 20,
                    "ax": 0.1,
                    "ay": 0.2,
                    "az": 9.8,
                    "gx": 0.01,
                    "gy": 0.01,
                    "gz": 0.02,
                }
                for i in range(100)
            ],
            "gps": [
                {
                    "t": 1759986000000 + i * 1000,
                    "lat": 22.50 + i * 0.005,
                    "lon": 114.30 + i * 0.005,
                    "speed": 15.0,
                }
                for i in range(60)
            ],
        }
    ]
    trip_id = _upload_trip(api_key, chunks)

    # Label as passenger
    client.post(
        f"/v1/me/trips/{trip_id}/label",
        headers={"X-API-Key": api_key},
        json={"trip_type": "passenger"},
    )

    settings = get_settings()
    response = client.get(
        f"/v1/insurer/drivers/{driver_id}",
        headers={"X-API-Key": settings.insurer_api_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert "passenger_share" in data
    assert "label_sources" in data
    assert "flagged_for_review" in data


def test_incident_sensor_snapshot_keeps_extra_and_null_omits_unset():
    _driver_id, api_key = _register_and_consent()

    response = client.post(
        "/v1/me/incidents",
        headers={"X-API-Key": api_key},
        json={
            "type": "crash",
            "time": datetime.now(UTC).isoformat(),
            "sensor_snapshot": {"peak_g": 4.5, "vendor_flag": "x", "imu_samples": None},
        },
    )
    assert response.status_code == 200

    with SessionLocal() as db:
        incident = db.get(Incident, response.json()["id"])
        assert incident is not None
        assert incident.sensor_snapshot == {
            "peak_g": 4.5,
            "vendor_flag": "x",
            "imu_samples": None,
        }


@pytest.mark.parametrize("trip_type", ["transit", "bogus"])
def test_label_trip_rejects_invalid_trip_type(trip_type):
    _driver_id, api_key = _register_and_consent()
    trip_id = _upload_trip(api_key, [])

    response = client.post(
        f"/v1/me/trips/{trip_id}/label",
        headers={"X-API-Key": api_key},
        json={"trip_type": trip_type},
    )
    assert response.status_code == 422


def test_incident_confirm_rejects_invalid_value():
    _driver_id, api_key = _register_and_consent()
    response = client.post(
        "/v1/me/incidents",
        headers={"X-API-Key": api_key},
        json={"type": "crash", "time": datetime.now(UTC).isoformat()},
    )
    incident_id = response.json()["id"]

    response = client.post(
        f"/v1/me/incidents/{incident_id}/confirm",
        headers={"X-API-Key": api_key},
        json={"confirmed": "maybe"},
    )
    assert response.status_code == 422


def test_label_twice_schedules_processing_once():
    _driver_id, api_key = _register_and_consent()
    trip_id = _upload_trip(api_key, [])
    with SessionLocal() as db:
        trip = db.get(Trip, trip_id)
        assert trip is not None
        trip.status = "done"
        db.commit()

    with unittest.mock.patch("app.routers.driver.BackgroundTasks.add_task") as mock_add_task:
        for _ in range(2):
            response = client.post(
                f"/v1/me/trips/{trip_id}/label",
                headers={"X-API-Key": api_key},
                json={"trip_type": "driver"},
            )
            assert response.status_code == 200
        mock_add_task.assert_called_once()

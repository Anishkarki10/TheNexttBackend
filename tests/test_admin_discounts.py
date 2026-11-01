"""Admin discount scheduling API."""
from datetime import datetime, timedelta

import pytest

from app.models import AdminAuditLog, ProductDiscount
from tests.conftest import iso, utcnow


def body_for(product, percent=15, starts=timedelta(minutes=-1), ends=timedelta(days=2), **extra):
    now = utcnow()
    body = {
        "product_id": product.id,
        "percent": percent,
        "starts_at": iso(now + starts),
        "ends_at": None if ends is None else iso(now + ends),
    }
    body.update(extra)
    return body


def test_requires_auth(client, product):
    assert client.get("/api/admin/discounts").status_code == 401
    assert client.post("/api/admin/discounts", json=body_for(product)).status_code == 401
    assert client.put("/api/admin/discounts/1", json={}).status_code == 401
    assert client.delete("/api/admin/discounts/1").status_code == 401


def test_create_active_discount(client, auth, product):
    res = client.post("/api/admin/discounts", headers=auth, json=body_for(product, label="  Dashain Offer  "))
    assert res.status_code == 201
    body = res.get_json()
    assert body["status"] == "active"
    assert body["percent"] == 15.0
    assert body["label"] == "Dashain Offer"  # trimmed
    assert body["product_name"] == product.name
    assert client.get("/api/products").get_json()[0]["variants"][0]["discounted_price_npr"] == 298.0


def test_create_scheduled_open_ended_discount(client, auth, product):
    res = client.post("/api/admin/discounts", headers=auth, json=body_for(product, starts=timedelta(days=1), ends=None))
    assert res.status_code == 201
    assert res.get_json()["status"] == "scheduled"
    assert res.get_json()["ends_at"] is None


def test_nepal_time_offset_is_converted_to_utc(client, auth, product):
    res = client.post(
        "/api/admin/discounts",
        headers=auth,
        json={"product_id": product.id, "percent": 10, "starts_at": "2026-12-05T00:00:00+05:45", "ends_at": "2026-12-10T23:59:00+05:45"},
    )
    assert res.status_code == 201
    body = res.get_json()
    assert body["starts_at"] == "2026-12-04T18:15:00Z"
    assert body["ends_at"] == "2026-12-10T18:14:00Z"
    stored = ProductDiscount.query.get(body["id"])
    assert stored.starts_at == datetime(2026, 12, 4, 18, 15)
    assert stored.starts_at.tzinfo is None


def test_blank_label_is_stored_as_null(client, auth, product):
    res = client.post("/api/admin/discounts", headers=auth, json=body_for(product, label="   "))
    assert res.get_json()["label"] is None


@pytest.mark.parametrize(
    "override, message",
    [
        ({"percent": 0}, "greater than 0"),
        ({"percent": 100}, "greater than 0"),
        ({"percent": -5}, "greater than 0"),
        ({"percent": True}, "must be a number"),
        ({"percent": "abc"}, "must be a number"),
        ({"percent": None}, "must be a number"),
        ({"product_id": 9999}, "existing product"),
        ({"product_id": "1"}, "existing product"),
        ({"starts_at": "2026-11-05T10:00"}, "timezone offset"),
        ({"starts_at": "not a date"}, "not a valid"),
        ({"starts_at": None}, "ISO 8601"),
        ({"ends_at": "2020-01-01T00:00:00Z"}, "after 'starts_at'"),
        ({"label": "x" * 81}, "at most 80"),
    ],
)
def test_create_validation(client, auth, product, override, message):
    res = client.post("/api/admin/discounts", headers=auth, json=body_for(product, **override))
    assert res.status_code == 400
    assert message in res.get_json()["error"]
    assert ProductDiscount.query.count() == 0


def test_ends_equal_to_start_rejected(client, auth, product):
    start = iso(utcnow() + timedelta(days=1))
    res = client.post("/api/admin/discounts", headers=auth, json={"product_id": product.id, "percent": 5, "starts_at": start, "ends_at": start})
    assert res.status_code == 400


@pytest.mark.parametrize(
    "new_starts, new_ends, overlaps",
    [
        (timedelta(days=1), timedelta(days=3), True),  # starts inside existing
        (timedelta(days=-1), timedelta(days=1), True),  # ends inside existing
        (timedelta(hours=1), timedelta(hours=2), True),  # fully contained
        (timedelta(days=-1), timedelta(days=5), True),  # fully covers
        (timedelta(days=-1), None, True),  # open-ended covers
        (timedelta(days=2), timedelta(days=3), False),  # starts exactly when existing ends
        (timedelta(days=3), None, False),  # after existing
        (timedelta(days=-2), timedelta(0), False),  # ends exactly when existing starts
    ],
)
def test_overlap_rules(client, auth, product, new_starts, new_ends, overlaps):
    # Existing discount runs from T to T+2d, with T fixed for both requests.
    base = utcnow().replace(microsecond=0)
    first = {"product_id": product.id, "percent": 10, "starts_at": iso(base), "ends_at": iso(base + timedelta(days=2))}
    assert client.post("/api/admin/discounts", headers=auth, json=first).status_code == 201

    second = {
        "product_id": product.id,
        "percent": 20,
        "starts_at": iso(base + new_starts),
        "ends_at": None if new_ends is None else iso(base + new_ends),
    }
    res = client.post("/api/admin/discounts", headers=auth, json=second)
    assert res.status_code == (409 if overlaps else 201), res.get_json()
    if overlaps:
        assert "overlaps" in res.get_json()["error"]


def test_existing_open_ended_blocks_later_discounts(client, auth, product):
    assert client.post("/api/admin/discounts", headers=auth, json=body_for(product, ends=None)).status_code == 201
    res = client.post("/api/admin/discounts", headers=auth, json=body_for(product, starts=timedelta(days=300), ends=timedelta(days=301)))
    assert res.status_code == 409


def test_same_window_on_different_products_is_fine(client, auth, make_product):
    a, b = make_product(), make_product()
    assert client.post("/api/admin/discounts", headers=auth, json=body_for(a)).status_code == 201
    assert client.post("/api/admin/discounts", headers=auth, json=body_for(b)).status_code == 201


def test_update_does_not_conflict_with_itself(client, auth, product):
    created = client.post("/api/admin/discounts", headers=auth, json=body_for(product)).get_json()
    res = client.put(f"/api/admin/discounts/{created['id']}", headers=auth, json={"percent": 25, "label": "Bigger"})
    assert res.status_code == 200
    assert res.get_json()["percent"] == 25.0
    assert res.get_json()["label"] == "Bigger"


def test_update_into_overlap_is_rejected_and_not_saved(client, auth, product):
    a = client.post("/api/admin/discounts", headers=auth, json=body_for(product, ends=timedelta(days=1))).get_json()
    b = client.post(
        "/api/admin/discounts", headers=auth, json=body_for(product, percent=30, starts=timedelta(days=5), ends=timedelta(days=6))
    ).get_json()
    res = client.put(f"/api/admin/discounts/{b['id']}", headers=auth, json={"starts_at": a["starts_at"]})
    assert res.status_code == 409
    assert ProductDiscount.query.get(b["id"]).starts_at > utcnow() + timedelta(days=4)  # unchanged


def test_invalid_update_does_not_persist_partial_changes(client, auth, product):
    created = client.post("/api/admin/discounts", headers=auth, json=body_for(product)).get_json()
    res = client.put(f"/api/admin/discounts/{created['id']}", headers=auth, json={"percent": 40, "ends_at": "2000-01-01T00:00:00Z"})
    assert res.status_code == 400
    assert float(ProductDiscount.query.get(created["id"]).percent) == 15.0


def test_end_now_via_update(client, auth, product):
    created = client.post("/api/admin/discounts", headers=auth, json=body_for(product, starts=timedelta(hours=-1))).get_json()
    res = client.put(f"/api/admin/discounts/{created['id']}", headers=auth, json={"ends_at": iso(utcnow())})
    assert res.status_code == 200
    assert res.get_json()["status"] == "expired"
    assert client.get("/api/products").get_json()[0]["discount"] is None


def test_update_and_delete_missing_discount(client, auth):
    assert client.put("/api/admin/discounts/999", headers=auth, json={"percent": 5}).status_code == 404
    assert client.delete("/api/admin/discounts/999", headers=auth).status_code == 404


def test_list_and_filter(client, auth, make_product):
    a, b = make_product(), make_product()
    client.post("/api/admin/discounts", headers=auth, json=body_for(a))
    client.post("/api/admin/discounts", headers=auth, json=body_for(a, starts=timedelta(days=10), ends=timedelta(days=11)))
    client.post("/api/admin/discounts", headers=auth, json=body_for(b))
    assert len(client.get("/api/admin/discounts", headers=auth).get_json()) == 3
    only_a = client.get(f"/api/admin/discounts?product_id={a.id}", headers=auth).get_json()
    assert {d["product_id"] for d in only_a} == {a.id}
    assert [d["status"] for d in only_a] == ["scheduled", "active"]  # newest start first


def test_delete_and_audit_trail(client, auth, product):
    created = client.post("/api/admin/discounts", headers=auth, json=body_for(product)).get_json()
    client.put(f"/api/admin/discounts/{created['id']}", headers=auth, json={"percent": 20})
    assert client.delete(f"/api/admin/discounts/{created['id']}", headers=auth).status_code == 204
    actions = [e.action for e in AdminAuditLog.query.order_by(AdminAuditLog.id)]
    assert actions == ["discount.create", "discount.update", "discount.delete"]
    assert all(e.target_id == product.id for e in AdminAuditLog.query)

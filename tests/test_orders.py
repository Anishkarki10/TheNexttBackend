"""Public checkout: validation, pricing (discounts + delivery), stock, lookup."""
import re
from datetime import timedelta

import pytest

from app.models import Order, OrderStatus, PaymentStatus, ProductVariant
from app.services.order_maintenance import release_stale_pending_orders
from tests.conftest import order_body, utcnow


def create(client, *args, **kwargs):
    return client.post("/api/orders", json=order_body(*args, **kwargs))


@pytest.mark.parametrize(
    "zone, charge, total",
    [("inside_valley", 100.0, 800.0), ("outside_valley", 170.0, 870.0)],
)
def test_delivery_charge_added_to_total(client, product, zone, charge, total):
    res = create(client, product.variants[0].id, quantity=2, zone=zone)
    assert res.status_code == 201
    body = res.get_json()
    assert body["subtotal_npr"] == 700.0
    assert body["delivery_zone"] == zone
    assert body["delivery_charge_npr"] == charge
    assert body["total_npr"] == total


def test_delivery_charge_is_per_order_not_per_item(client, make_product):
    a, b = make_product(), make_product(price=200)
    body = order_body(a.variants[0].id, quantity=3)
    body["items"].append({"variant_id": b.variants[0].id, "quantity": 2})
    res = client.post("/api/orders", json=body).get_json()
    assert res["subtotal_npr"] == 1450.0
    assert res["total_npr"] == 1550.0


@pytest.mark.parametrize("zone", [None, "", "mars", "INSIDE_VALLEY", 1])
def test_invalid_delivery_zone_rejected(client, product, zone):
    res = create(client, product.variants[0].id, zone=zone)
    assert res.status_code == 400
    assert "delivery_zone" in res.get_json()["errors"]
    assert Order.query.count() == 0


def test_client_cannot_override_charges_or_prices(client, product):
    body = order_body(product.variants[0].id, delivery_charge_npr=0, total_npr=1, subtotal_npr=1)
    body["items"][0]["unit_price_npr"] = 1
    res = client.post("/api/orders", json=body).get_json()
    assert res["total_npr"] == 450.0


def test_active_discount_applied_and_snapshotted(client, product, make_discount):
    make_discount(product, percent=15)
    res = create(client, product.variants[0].id, quantity=2, zone="outside_valley").get_json()
    item = res["items"][0]
    assert item["unit_price_npr"] == 298.0
    assert item["discount_percent"] == 15.0
    assert item["line_total_npr"] == 596.0
    assert res["subtotal_npr"] == 596.0
    assert res["total_npr"] == 766.0


def test_no_discount_snapshot_is_null(client, product, make_discount):
    make_discount(product, starts=timedelta(days=1), ends=timedelta(days=2))  # scheduled, not active
    item = create(client, product.variants[0].id).get_json()["items"][0]
    assert item["unit_price_npr"] == 350.0
    assert item["discount_percent"] is None


def test_past_order_keeps_its_price_after_discount_changes(client, db, product, make_discount):
    discount = make_discount(product, percent=50)
    order = create(client, product.variants[0].id).get_json()
    db.session.delete(discount)
    db.session.commit()
    again = client.get(f"/api/orders/{order['order_number']}?phone=9800000000").get_json()
    assert again["items"][0]["unit_price_npr"] == 175.0


@pytest.mark.parametrize(
    "customer, field",
    [
        ({"phone": "9800000000", "address": "x"}, "customer.name"),
        ({"name": "A", "address": "x"}, "customer.phone"),
        ({"name": "A", "phone": "12345", "address": "x"}, "customer.phone"),
        ({"name": "A", "phone": "9800000000"}, "customer.address"),
    ],
)
def test_customer_validation(client, product, customer, field):
    res = client.post("/api/orders", json=order_body(product.variants[0].id, customer=customer))
    assert res.status_code == 400
    assert field in res.get_json()["errors"]


@pytest.mark.parametrize("phone", ["9800000000", "+977-980-000-0000", "4412345"])
def test_valid_phone_formats(client, product, phone):
    body = order_body(product.variants[0].id)
    body["customer"]["phone"] = phone
    assert client.post("/api/orders", json=body).status_code == 201


def test_empty_items_rejected(client, product):
    res = client.post("/api/orders", json=order_body(product.variants[0].id, items=[]))
    assert res.status_code == 400
    assert "items" in res.get_json()["errors"]


@pytest.mark.parametrize(
    "items",
    [
        "not-a-list",
        [{"variant_id": "1", "quantity": 1}],
        [{"variant_id": True, "quantity": 1}],
        [{"variant_id": 1, "quantity": 0}],
        [{"variant_id": 1, "quantity": True}],
        [{"variant_id": 1, "quantity": 1.5}],
    ],
)
def test_malformed_items_rejected(client, product, items):
    assert client.post("/api/orders", json=order_body(product.variants[0].id, items=items)).status_code == 400


def test_payment_method_must_be_known_and_enabled(client, product):
    res = create(client, product.variants[0].id, method="bitcoin")
    assert res.status_code == 400
    res = create(client, product.variants[0].id, method="esewa")
    assert res.status_code == 400
    assert "temporarily unavailable" in res.get_json()["errors"]["payment_method"]


def test_cod_order_goes_to_processing(client, product):
    body = create(client, product.variants[0].id).get_json()
    assert body["status"] == "processing"
    assert body["payment_status"] == "unpaid"
    assert re.fullmatch(r"TNP-\d{8}-[0-9A-F]{6}", body["order_number"])


def test_online_order_starts_pending_payment(app, client, product):
    app.config["ENABLED_PAYMENT_METHODS"] = {"cod", "esewa"}
    body = create(client, product.variants[0].id, method="esewa").get_json()
    assert body["status"] == "pending_payment"


def test_stock_is_reserved(client, db, product):
    variant = product.variants[0]
    create(client, variant.id, quantity=30)
    db.session.expire_all()
    assert db.session.get(ProductVariant, variant.id).stock_qty == 70


def test_insufficient_stock_rejected_without_side_effects(client, db, make_product):
    a, b = make_product(stock=10), make_product(stock=1)
    body = order_body(a.variants[0].id, quantity=5)
    body["items"].append({"variant_id": b.variants[0].id, "quantity": 2})
    res = client.post("/api/orders", json=body)
    assert res.status_code == 409
    db.session.expire_all()
    assert db.session.get(ProductVariant, a.variants[0].id).stock_qty == 10  # rolled back
    assert Order.query.count() == 0


def test_enquiry_only_variant_cannot_be_ordered(client, product):
    res = create(client, product.variants[1].id)
    assert res.status_code == 400
    assert "enquiry-only" in res.get_json()["error"]


def test_unknown_or_unavailable_variant(client, make_product):
    hidden = make_product(available=False)
    assert create(client, 9999).status_code == 404
    assert create(client, hidden.variants[0].id).status_code == 404


def test_order_lookup_requires_matching_phone(client, product):
    number = create(client, product.variants[0].id).get_json()["order_number"]
    assert client.get(f"/api/orders/{number}").status_code == 403
    assert client.get(f"/api/orders/{number}?phone=9811111111").status_code == 403
    res = client.get(f"/api/orders/{number}?phone=9800000000")
    assert res.status_code == 200
    assert res.get_json()["delivery_charge_npr"] == 100.0
    assert client.get("/api/orders/TNP-00000000-XXXXXX?phone=9800000000").status_code == 404


# --- stale order release ---------------------------------------------------------


def _pending_order(app, client, db, product, minutes_old):
    app.config["ENABLED_PAYMENT_METHODS"] = {"cod", "esewa"}
    order_id = create(client, product.variants[0].id, quantity=10, method="esewa").get_json()["id"]
    order = db.session.get(Order, order_id)
    order.created_at = utcnow() - timedelta(minutes=minutes_old)
    db.session.commit()
    return order_id


def test_stale_pending_orders_released(app, client, db, product):
    order_id = _pending_order(app, client, db, product, minutes_old=31)
    assert release_stale_pending_orders() == 1
    order = db.session.get(Order, order_id)
    assert order.status == OrderStatus.cancelled
    assert order.payment_status == PaymentStatus.failed
    assert db.session.get(ProductVariant, product.variants[0].id).stock_qty == 100


def test_fresh_pending_and_cod_orders_untouched(app, client, db, product):
    _pending_order(app, client, db, product, minutes_old=5)
    cod_id = create(client, product.variants[0].id).get_json()["id"]
    cod = db.session.get(Order, cod_id)
    cod.created_at = utcnow() - timedelta(hours=5)
    db.session.commit()
    assert release_stale_pending_orders() == 0


def test_order_creation_releases_stale_orders_first(app, client, db, make_product):
    product = make_product(stock=10)
    _pending_order(app, client, db, product, minutes_old=45)  # reserves all 10
    res = create(client, product.variants[0].id, quantity=10)
    assert res.status_code == 201

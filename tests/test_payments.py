"""eSewa / Stripe initiate + callback flows, with the gateways mocked."""
import base64
import hashlib
import hmac
import json

import pytest

from app.models import Order, PaymentStatus
from app.services import esewa, stripe_gateway
from tests.conftest import order_body


@pytest.fixture
def online(app):
    app.config["ENABLED_PAYMENT_METHODS"] = {"cod", "esewa", "stripe"}


def make_order(client, product, method="esewa", zone="outside_valley"):
    res = client.post("/api/orders", json=order_body(product.variants[0].id, quantity=2, zone=zone, method=method))
    assert res.status_code == 201, res.get_json()
    return res.get_json()


def test_disabled_gateways_return_503(client, product):
    assert client.post("/api/payments/esewa/initiate", json={"order_id": 1}).status_code == 503
    assert client.post("/api/payments/stripe/initiate", json={"order_id": 1}).status_code == 503


def test_esewa_initiate_signs_total_including_delivery(app, client, product, online):
    order = make_order(client, product)
    res = client.post("/api/payments/esewa/initiate", json={"order_id": order["id"]})
    assert res.status_code == 200
    fields = res.get_json()
    assert fields["total_amount"] == "870.00"  # 700 items + 170 delivery
    assert fields["amount"] == "870.00"
    assert fields["product_delivery_charge"] == "0"
    message = f"total_amount=870.00,transaction_uuid={order['order_number']},product_code={app.config['ESEWA_MERCHANT_CODE']}"
    expected = base64.b64encode(
        hmac.new(app.config["ESEWA_SECRET_KEY"].encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    assert fields["signature"] == expected
    assert fields["success_url"].startswith("https://shop.test/payment/esewa/return")


def test_esewa_initiate_errors(client, db, product, online):
    assert client.post("/api/payments/esewa/initiate", json={"order_id": 999}).status_code == 404
    order = make_order(client, product)
    o = db.session.get(Order, order["id"])
    o.payment_status = PaymentStatus.paid
    db.session.commit()
    assert client.post("/api/payments/esewa/initiate", json={"order_id": order["id"]}).status_code == 409


def _esewa_data(order_number):
    return base64.b64encode(json.dumps({"transaction_uuid": order_number, "transaction_code": "TX1"}).encode()).decode()


def test_esewa_callback_verified_marks_paid_idempotently(client, product, online, monkeypatch):
    calls = []
    monkeypatch.setattr(esewa, "verify_payment", lambda uuid, amount: calls.append((uuid, float(amount))) or True)
    order = make_order(client, product)
    url = f"/api/payments/esewa/callback?order_number={order['order_number']}&data={_esewa_data(order['order_number'])}"
    body = client.get(url).get_json()
    assert body["payment_status"] == "paid"
    assert body["status"] == "processing"
    assert calls == [(order["order_number"], 870.0)]  # verified against the full total
    assert client.get(url).get_json()["payment_status"] == "paid"


def test_esewa_callback_unverified_marks_failed(client, product, online, monkeypatch):
    monkeypatch.setattr(esewa, "verify_payment", lambda *a: False)
    order = make_order(client, product)
    body = client.get(f"/api/payments/esewa/callback?order_number={order['order_number']}&data={_esewa_data(order['order_number'])}").get_json()
    assert body["payment_status"] == "failed"


def test_esewa_callback_without_data_or_bad_data(client, product, online):
    order = make_order(client, product)
    assert client.get(f"/api/payments/esewa/callback?order_number={order['order_number']}").get_json()["payment_status"] == "failed"
    assert client.get(f"/api/payments/esewa/callback?order_number={order['order_number']}&data=%%%").status_code == 400
    assert client.get("/api/payments/esewa/callback?order_number=nope").status_code == 404


def test_failure_never_downgrades_paid_order(client, db, product, online):
    order = make_order(client, product)
    o = db.session.get(Order, order["id"])
    o.payment_status = PaymentStatus.paid
    db.session.commit()
    assert client.get(f"/api/payments/esewa/callback?order_number={order['order_number']}").get_json()["payment_status"] == "paid"


def test_stripe_not_configured(client, product, online):
    order = make_order(client, product, method="stripe")
    res = client.post("/api/payments/stripe/initiate", json={"order_id": order["id"]})
    assert res.status_code == 503
    assert "STRIPE_SECRET_KEY" in res.get_json()["error"]


def test_stripe_initiate_and_callback(client, product, online, monkeypatch):
    seen = {}

    def fake_create(order):
        seen["amount"] = int(round(float(order.total_npr) * 100))
        return {"id": "cs_test_1", "url": "https://checkout.stripe.test/cs_test_1"}

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", fake_create)
    order = make_order(client, product, method="stripe", zone="inside_valley")
    res = client.post("/api/payments/stripe/initiate", json={"order_id": order["id"]})
    assert res.get_json()["url"].startswith("https://checkout.stripe.test")
    assert seen["amount"] == 80000  # NPR 800.00 in paisa (700 + 100 delivery)

    monkeypatch.setattr(
        stripe_gateway,
        "retrieve_session",
        lambda sid: {"payment_status": "paid", "payment_intent": "pi_1", "metadata": {"order_number": order["order_number"]}},
    )
    body = client.get(f"/api/payments/stripe/callback?order_number={order['order_number']}&session_id=cs_test_1").get_json()
    assert body["payment_status"] == "paid"


def test_stripe_callback_mismatch_and_cancel(client, product, online, monkeypatch):
    order = make_order(client, product, method="stripe")
    monkeypatch.setattr(stripe_gateway, "retrieve_session", lambda sid: {"payment_status": "paid", "metadata": {"order_number": "OTHER"}})
    assert client.get(f"/api/payments/stripe/callback?order_number={order['order_number']}&session_id=x").status_code == 400
    assert client.get(f"/api/payments/stripe/callback?order_number={order['order_number']}").get_json()["payment_status"] == "failed"


def test_initiate_on_cancelled_order_is_gone(client, db, product, online):
    from app.models import OrderStatus

    order = make_order(client, product)
    o = db.session.get(Order, order["id"])
    o.status = OrderStatus.cancelled
    db.session.commit()
    assert client.post("/api/payments/esewa/initiate", json={"order_id": order["id"]}).status_code == 410

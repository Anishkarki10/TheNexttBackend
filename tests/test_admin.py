"""Admin auth, orders, enquiries, dashboard, audit log."""
import pytest

from app.models import AdminAuditLog, Order, PaymentStatus, ProductVariant
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, order_body

ADMIN_ROUTES = [
    ("get", "/api/admin/me"),
    ("post", "/api/admin/logout"),
    ("get", "/api/admin/dashboard/stats"),
    ("get", "/api/admin/products"),
    ("post", "/api/admin/products"),
    ("get", "/api/admin/products/1"),
    ("put", "/api/admin/products/1"),
    ("delete", "/api/admin/products/1"),
    ("post", "/api/admin/products/1/images"),
    ("delete", "/api/admin/products/1/images/1"),
    ("get", "/api/admin/orders"),
    ("get", "/api/admin/orders/1"),
    ("patch", "/api/admin/orders/1/status"),
    ("get", "/api/admin/enquiries"),
    ("get", "/api/admin/enquiries/1"),
    ("patch", "/api/admin/enquiries/1/status"),
    ("get", "/api/admin/audit-log"),
    ("get", "/api/admin/discounts"),
    ("post", "/api/admin/discounts"),
    ("put", "/api/admin/discounts/1"),
    ("delete", "/api/admin/discounts/1"),
]


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_every_admin_route_requires_token(client, method, path):
    res = getattr(client, method)(path)
    assert res.status_code == 401
    assert "error" in res.get_json() or "msg" in res.get_json()


# --- auth -------------------------------------------------------------------


def test_login_success_is_case_insensitive(client, admin):
    res = client.post("/api/admin/login", json={"email": "  ADMIN@test.local ", "password": ADMIN_PASSWORD})
    assert res.status_code == 200
    assert res.get_json()["user"]["email"] == ADMIN_EMAIL


@pytest.mark.parametrize("body", [{"email": ADMIN_EMAIL, "password": "wrong"}, {"email": "x@y.z", "password": ADMIN_PASSWORD}, {}])
def test_login_failure(client, admin, body):
    res = client.post("/api/admin/login", json=body)
    assert res.status_code == 401
    assert res.get_json() == {"error": "Invalid email or password"}


def test_me_and_logout_revokes_token(client, auth):
    assert client.get("/api/admin/me", headers=auth).get_json()["email"] == ADMIN_EMAIL
    assert client.post("/api/admin/logout", headers=auth).status_code == 204
    assert client.get("/api/admin/me", headers=auth).status_code == 401


def test_garbage_token_rejected(client):
    assert client.get("/api/admin/me", headers={"Authorization": "Bearer not.a.jwt"}).status_code in (401, 422)


# --- orders -------------------------------------------------------------------


@pytest.fixture
def order(client, product):
    return client.post("/api/orders", json=order_body(product.variants[0].id, quantity=4)).get_json()


def test_list_orders_and_filter(client, auth, order):
    body = client.get("/api/admin/orders", headers=auth).get_json()
    assert body["total_orders"] == 1
    assert body["orders"][0]["delivery_zone"] == "inside_valley"
    assert "items" not in body["orders"][0]
    assert client.get("/api/admin/orders?status=processing", headers=auth).get_json()["total_orders"] == 1
    assert client.get("/api/admin/orders?status=shipped", headers=auth).get_json()["total_orders"] == 0
    assert client.get("/api/admin/orders?status=bogus", headers=auth).status_code == 400


def test_get_order_detail(client, auth, order):
    body = client.get(f"/api/admin/orders/{order['id']}", headers=auth).get_json()
    assert body["items"][0]["quantity"] == 4
    assert body["delivery_charge_npr"] == 100.0
    assert client.get("/api/admin/orders/999", headers=auth).status_code == 404


def test_cancel_restores_stock_and_cannot_reopen(client, auth, db, product, order):
    variant_id = product.variants[0].id
    res = client.patch(f"/api/admin/orders/{order['id']}/status", headers=auth, json={"status": "cancelled"})
    assert res.status_code == 200
    db.session.expire_all()
    assert db.session.get(ProductVariant, variant_id).stock_qty == 100
    # cancelling again must not double-restore
    client.patch(f"/api/admin/orders/{order['id']}/status", headers=auth, json={"status": "cancelled"})
    db.session.expire_all()
    assert db.session.get(ProductVariant, variant_id).stock_qty == 100
    assert client.patch(f"/api/admin/orders/{order['id']}/status", headers=auth, json={"status": "processing"}).status_code == 409


def test_update_payment_status_and_audit(client, auth, db, order):
    res = client.patch(f"/api/admin/orders/{order['id']}/status", headers=auth, json={"payment_status": "paid", "status": "delivered"})
    assert res.status_code == 200
    assert res.get_json()["payment_status"] == "paid"
    entry = AdminAuditLog.query.filter_by(action="order.status_update").one()
    assert entry.details["payment_status"] == {"from": "unpaid", "to": "paid"}


@pytest.mark.parametrize("body", [{"status": "teleported"}, {"payment_status": "maybe"}, {"status": None}])
def test_invalid_status_values(client, auth, order, body):
    assert client.patch(f"/api/admin/orders/{order['id']}/status", headers=auth, json=body).status_code == 400


# --- dashboard ----------------------------------------------------------------


def test_dashboard_revenue_counts_only_paid_orders(client, auth, db, product):
    first = client.post("/api/orders", json=order_body(product.variants[0].id, zone="outside_valley")).get_json()
    client.post("/api/orders", json=order_body(product.variants[0].id))
    o = db.session.get(Order, first["id"])
    o.payment_status = PaymentStatus.paid
    db.session.commit()
    stats = client.get("/api/admin/dashboard/stats", headers=auth).get_json()
    assert stats["total_orders"] == 2
    assert stats["total_revenue_npr"] == 520.0  # 350 + 170 delivery
    assert stats["orders_by_status"]["processing"] == 2
    assert stats["total_products"] == 1
    assert len(stats["recent_orders"]) == 2


# --- enquiries ----------------------------------------------------------------

VALID_ENQUIRY = {"name": "Ram", "email": "ram@example.com", "enquiry_type": "wholesale", "message": "Hello", "consent": True}


@pytest.mark.parametrize(
    "override, field",
    [
        ({"name": "  "}, "name"),
        ({"email": ""}, "email"),
        ({"email": "not-an-email"}, "email"),
        ({"enquiry_type": ""}, "enquiry_type"),
        ({"message": ""}, "message"),
        ({"consent": False}, "consent"),
    ],
)
def test_enquiry_validation(client, override, field):
    res = client.post("/api/enquiries", json={**VALID_ENQUIRY, **override})
    assert res.status_code == 400
    assert field in res.get_json()["errors"]


def test_enquiry_lifecycle(client, auth):
    created = client.post("/api/enquiries", json={**VALID_ENQUIRY, "name": "  Ram  "})
    assert created.status_code == 201
    enquiry = created.get_json()
    assert enquiry["name"] == "Ram"
    assert enquiry["status"] == "new"

    listing = client.get("/api/admin/enquiries", headers=auth).get_json()
    assert listing["unread_count"] == 1
    assert client.get("/api/admin/enquiries?status=bad", headers=auth).status_code == 400

    # viewing marks it read
    assert client.get(f"/api/admin/enquiries/{enquiry['id']}", headers=auth).get_json()["status"] == "read"
    assert client.get("/api/admin/enquiries", headers=auth).get_json()["unread_count"] == 0

    res = client.patch(f"/api/admin/enquiries/{enquiry['id']}/status", headers=auth, json={"status": "responded"})
    assert res.get_json()["status"] == "responded"
    assert client.patch(f"/api/admin/enquiries/{enquiry['id']}/status", headers=auth, json={"status": "x"}).status_code == 400


# --- audit log ----------------------------------------------------------------


def test_audit_log_lists_newest_first(client, auth, product):
    client.put(f"/api/admin/products/{product.id}", headers=auth, json={"name": "A"})
    client.delete(f"/api/admin/products/{product.id}", headers=auth)
    body = client.get("/api/admin/audit-log", headers=auth).get_json()
    assert body["total_entries"] == 2
    assert {e["action"] for e in body["entries"]} == {"product.update", "product.delete"}
    assert body["entries"][0]["admin_email"] == ADMIN_EMAIL

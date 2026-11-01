from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool

from app import create_app
from app.config import Config
from app.extensions import db as _db
from app.models import AdminUser, Product, ProductDiscount, ProductVariant

ADMIN_EMAIL = "admin@test.local"
ADMIN_PASSWORD = "correct-horse-battery"


class TestConfig(Config):
    TESTING = True
    APP_ENV = "test"
    SECRET_KEY = "test-secret-key-that-is-long-enough-123"
    JWT_SECRET_KEY = "test-jwt-secret-key-that-is-long-enough-123"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    # One shared in-memory connection, so every request sees the same DB.
    SQLALCHEMY_ENGINE_OPTIONS = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    RATELIMIT_ENABLED = False
    ENABLED_PAYMENT_METHODS = {"cod"}
    STRIPE_SECRET_KEY = ""
    FRONTEND_URL = "https://shop.test"


@pytest.fixture
def app(tmp_path):
    config = type("PerTestConfig", (TestConfig,), {"UPLOAD_FOLDER": str(tmp_path / "uploads")})
    app = create_app(config)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def admin(db):
    user = AdminUser(email=ADMIN_EMAIL, name="Test Admin")
    user.set_password(ADMIN_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def auth(client, admin):
    """Authorization headers for a logged-in admin."""
    res = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert res.status_code == 200, res.get_json()
    return {"Authorization": f"Bearer {res.get_json()['access_token']}"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def make_product(db):
    counter = {"n": 0}

    def _make(price=350, stock=100, available=True, enquiry_variant=True, name=None):
        counter["n"] += 1
        n = counter["n"]
        product = Product(slug=f"product-{n}", name=name or f"Product {n}", sku=f"SKU-{n}", is_available=available)
        product.variants = [ProductVariant(label="500 g", pack_size="500g", price_npr=price, stock_qty=stock, sort_order=0)]
        if enquiry_variant:
            product.variants.append(
                ProductVariant(label="1 kg", pack_size="1kg", price_npr=None, is_enquiry_only=True, stock_qty=0, sort_order=1)
            )
        db.session.add(product)
        db.session.commit()
        return product

    return _make


@pytest.fixture
def product(make_product):
    return make_product()


@pytest.fixture
def make_discount(db):
    def _make(product, percent=15, starts=timedelta(minutes=-5), ends=timedelta(days=1), label=None):
        now = utcnow()
        discount = ProductDiscount(
            product_id=product.id,
            percent=percent,
            label=label,
            starts_at=now + starts,
            ends_at=None if ends is None else now + ends,
        )
        db.session.add(discount)
        db.session.commit()
        return discount

    return _make


def iso(dt: datetime) -> str:
    """Naive-UTC datetime -> ISO string with an explicit offset, as the admin UI sends."""
    return dt.replace(tzinfo=timezone.utc).isoformat()


def order_body(variant_id, quantity=1, zone="inside_valley", method="cod", **overrides):
    body = {
        "customer": {"name": "Test Customer", "phone": "9800000000", "address": "Thamel, Kathmandu"},
        "items": [{"variant_id": variant_id, "quantity": quantity}],
        "payment_method": method,
        "delivery_zone": zone,
    }
    body.update(overrides)
    return body

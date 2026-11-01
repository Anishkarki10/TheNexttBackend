"""App-wide behaviour, CLI commands, and migration/model consistency."""
import pytest
from sqlalchemy.pool import StaticPool

from app import create_app
from app.extensions import db
from app.models import AdminUser, Product
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, TestConfig


def test_unknown_route_returns_json_404(client):
    res = client.get("/api/does-not-exist")
    assert res.status_code == 404
    assert "error" in res.get_json()


def test_method_not_allowed_is_json(client):
    res = client.delete("/api/products")
    assert res.status_code == 405
    assert "error" in res.get_json()


def test_security_headers(client):
    res = client.get("/api/products")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "max-age" in res.headers["Strict-Transport-Security"]


def test_cors_allows_configured_origin_only(client):
    ok = client.get("/api/products", headers={"Origin": "http://localhost:8443"})
    assert ok.headers.get("Access-Control-Allow-Origin") == "http://localhost:8443"
    bad = client.get("/api/products", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in bad.headers


def test_production_refuses_insecure_default_secrets():
    class Prod(TestConfig):
        APP_ENV = "production"
        JWT_SECRET_KEY = "dev-jwt-secret-key-change-me"

    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        create_app(Prod)


def test_login_rate_limited_with_json_429(tmp_path):
    class Limited(TestConfig):
        RATELIMIT_ENABLED = True
        UPLOAD_FOLDER = str(tmp_path)

    app = create_app(Limited)
    with app.app_context():
        db.create_all()
        client = app.test_client()
        codes = [client.post("/api/admin/login", json={"email": "a@b.c", "password": "x"}).status_code for _ in range(6)]
        assert codes[:5] == [401] * 5
        assert codes[5] == 429
        assert "Too many requests" in client.post("/api/admin/login", json={}).get_json()["error"]
        db.session.remove()
        db.drop_all()


# --- CLI ----------------------------------------------------------------------


def test_seed_admin_creates_then_updates(app):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["seed-admin", "--email", "Boss@Shop.Test", "--password", "a-long-password"])
    assert "Created admin 'boss@shop.test'" in res.output
    res = runner.invoke(args=["seed-admin", "--email", "boss@shop.test", "--password", "another-long-one"])
    assert "Updated password" in res.output
    user = AdminUser.query.filter_by(email="boss@shop.test").one()
    assert user.check_password("another-long-one")


def test_seed_admin_rejects_short_password(app):
    res = app.test_cli_runner().invoke(args=["seed-admin", "--email", "a@b.c", "--password", "short"])
    assert res.exit_code != 0
    assert AdminUser.query.count() == 0


def test_seed_products_is_idempotent(app):
    runner = app.test_cli_runner()
    runner.invoke(args=["seed-products"])
    res = runner.invoke(args=["seed-products"])
    assert "already exists" in res.output
    assert Product.query.count() == 1


# --- migrations ---------------------------------------------------------------


def test_migrations_apply_cleanly_and_match_models(tmp_path):
    """`flask db upgrade` on an empty DB, then `flask db check` (Alembic's
    autogenerate diff) must find nothing - catches a model change that was
    made without a migration."""

    class MigrationConfig(TestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'migrate.db'}"
        SQLALCHEMY_ENGINE_OPTIONS = {}
        UPLOAD_FOLDER = str(tmp_path)

    app = create_app(MigrationConfig)
    runner = app.test_cli_runner()
    upgrade = runner.invoke(args=["db", "upgrade"])
    assert upgrade.exit_code == 0, upgrade.output
    check = runner.invoke(args=["db", "check"])
    assert check.exit_code == 0, check.output  # non-zero when models and migrations differ
    downgrade = runner.invoke(args=["db", "downgrade", "05a2f3077f9b"])
    assert downgrade.exit_code == 0, downgrade.output


def test_static_pool_fixture_sanity():
    assert TestConfig.SQLALCHEMY_ENGINE_OPTIONS["poolclass"] is StaticPool
    assert ADMIN_EMAIL and len(ADMIN_PASSWORD) >= 10

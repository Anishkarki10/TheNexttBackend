import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.extensions import cors, db, jwt, limiter, migrate

load_dotenv()

_INSECURE_DEFAULTS = {
    "SECRET_KEY": "dev-secret-key-change-me",
    "JWT_SECRET_KEY": "dev-jwt-secret-key-change-me",
}


def _refuse_insecure_production_secrets(app: Flask) -> None:
    """Fail fast at boot rather than silently run with forgeable JWTs / sessions."""
    if app.config.get("APP_ENV") != "production":
        return
    leaked = [key for key, default in _INSECURE_DEFAULTS.items() if app.config.get(key) == default]
    if leaked:
        raise RuntimeError(
            "Refusing to start with APP_ENV=production while "
            f"{', '.join(leaked)} still {'has' if len(leaked) == 1 else 'have'} "
            "the insecure development default value. Set real secret(s) via "
            "environment variables before running in production."
        )


def create_app(config_class: type = Config) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    _refuse_insecure_production_secrets(app)

    proxy_count = app.config["TRUSTED_PROXY_COUNT"]
    if proxy_count:
        # Behind nginx every request arrives from the proxy's own address, so
        # without this the rate limiter (keyed on remote_addr) would put all
        # visitors in one shared bucket - e.g. 5 failed logins from anyone
        # would lock out the real admin. Only the last `proxy_count`
        # X-Forwarded-For hops are trusted, so clients can't spoof their IP.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_count, x_proto=proxy_count, x_host=proxy_count)

    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})
    limiter.init_app(app)

    @jwt.token_in_blocklist_loader
    def _is_token_revoked(_jwt_header, jwt_payload):
        from app.models import RevokedToken

        return RevokedToken.query.filter_by(jti=jwt_payload["jti"]).first() is not None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Browsers ignore this header entirely unless the response was
        # actually served over HTTPS, so it's harmless to always send - it
        # just has no effect until the deployment is HTTPS (which it should be).
        response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response

    from app.api import (
        admin_audit_log,
        admin_auth,
        admin_dashboard,
        admin_discounts,
        admin_enquiries,
        admin_orders,
        admin_products,
        enquiries,
        orders,
        payments,
        products,
    )

    app.register_blueprint(products.bp)
    app.register_blueprint(orders.bp)
    app.register_blueprint(payments.bp)
    app.register_blueprint(enquiries.bp)
    app.register_blueprint(admin_auth.bp)
    app.register_blueprint(admin_products.bp)
    app.register_blueprint(admin_products.uploads_bp)
    app.register_blueprint(admin_discounts.bp)
    app.register_blueprint(admin_orders.bp)
    app.register_blueprint(admin_dashboard.bp)
    app.register_blueprint(admin_enquiries.bp)
    app.register_blueprint(admin_audit_log.bp)

    @app.errorhandler(HTTPException)
    def _http_error_as_json(exc: HTTPException):
        # The frontends read `body.error`; Flask's defaults (404, 405, 413,
        # 429 from the rate limiter...) are HTML pages they can't parse.
        if exc.code == 429:
            return jsonify({"error": "Too many requests - please wait a minute and try again."}), 429
        return jsonify({"error": exc.description}), exc.code

    @app.errorhandler(Exception)
    def _unhandled_error_as_json(exc: Exception):
        db.session.rollback()
        app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        return jsonify({"error": "Internal server error"}), 500

    from app import cli

    cli.register(app)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Flask API backend for "The Next Protein" (TNP), a small e-commerce store in Nepal (prices are NPR). It serves the public storefront and the admin panel. Both frontends live in separate repos and are hosted on Vercel; their origins must be listed in `CORS_ORIGINS`.

Production runs on a Contabo VPS with `docker compose`: Postgres (`db`), gunicorn (`api`), and Caddy, which provides automatic HTTPS for `API_DOMAIN` (`Caddyfile`). `docker-entrypoint.sh` runs `flask db upgrade` and `seed-admin` on every container start. Server settings go in `.env` (see `.env.example`).

There is no test suite and no linter/formatter config in the repo.

## Commands

Backend (run from the repo root; `.env` is loaded through python-dotenv):

```bash
pip install -r requirements.txt
flask --app wsgi db upgrade            # apply migrations (SQLite at instance/app.db by default)
flask --app wsgi db migrate -m "msg"   # autogenerate a migration after model changes
flask --app wsgi seed-admin            # create/update admin; uses ADMIN_EMAIL/ADMIN_PASSWORD env or --email/--password
flask --app wsgi seed-products         # insert the Protein Loaf product (idempotent)
python wsgi.py                         # dev server on :5000 (debug on unless APP_ENV=production)
```

Deploy / update on the server:

```bash
git pull && docker compose up -d --build
docker compose logs -f api
docker compose exec db psql -U "$POSTGRES_USER" "$POSTGRES_DB"
```

## Architecture

**App factory**: `app/__init__.py::create_app` sets up the extensions from `app/extensions.py` (SQLAlchemy, Migrate, JWT, CORS limited to `/api/*`, and Limiter with a default of 200/min, stored in memory unless `RATELIMIT_STORAGE_URI` is set). It also registers every blueprint and adds the security headers. If `APP_ENV=production` and `SECRET_KEY`/`JWT_SECRET_KEY` still hold their dev defaults, it refuses to boot. All configuration comes from env vars in `app/config.py`. `DATABASE_URL` is rewritten to the `postgresql+psycopg://` driver (psycopg 3), and SQLite is the fallback when it's unset. Gunicorn runs a single worker with threads because rate limits are kept in memory per process.

**Blueprints** (`app/api/`): each one defines its own `url_prefix`.
- Public endpoints: `products`, `orders`, `payments`, `enquiries` under `/api/...`.
- Admin endpoints: `admin_*` under `/api/admin/...`. Each route uses `@jwt_required()`, and the JWT identity is the `AdminUser.id` as a string. Logout writes the token's `jti` to `RevokedToken`, and `token_in_blocklist_loader` checks that table. Admins sign up only through the `seed-admin` CLI command.
- `/uploads/<file>` serves product images saved by `services/uploads.py`, which checks magic bytes as well as the file extension.

**Audit logging**: admin routes that change data call `services/audit.py::log_admin_action(...)` *before* `db.session.commit()`. That function only adds the entry to the session and doesn't commit it, so the log entry and the change it records are saved in the same transaction. Keep this pattern in new admin mutations, and call `db.session.flush()` first when you need a new row's id.

**Order/stock lifecycle** (spread across several files):
- `orders.py::create_order` locks each variant row (`with_for_update`) and decrements `ProductVariant.stock_qty` right away to reserve stock. Prices always come from the server-side variant, never from the client. `OrderItem` stores snapshots of the product name, variant label, and price, and its FKs are nullable so editing or deleting variants doesn't break past orders.
- COD orders go straight to `processing`. Online-payment orders start in `pending_payment`.
- No cron or task queue exists. Instead, `services/order_maintenance.py::release_stale_pending_orders()` runs at the start of stock-sensitive endpoints (order creation, payment initiation). It cancels `pending_payment` orders older than 30 minutes and gives their stock back. Call it from any new endpoint that depends on stock.
- `ENABLED_PAYMENT_METHODS` (env, default `cod`) is the only switch for payment methods. Order creation checks it, and so does each payment `initiate` endpoint. eSewa (`services/esewa.py`) and Stripe (`services/stripe_gateway.py`) are fully built but turned off. Their callback endpoints verify the payment with the gateway and are idempotent (`_mark_paid` never downgrades an order that's already paid).
- Public order lookup (`GET /api/orders/<number>`) requires `?phone=` to match the order's phone number.

**Datetimes**: the database stores naive UTC (`db.func.now()`). Serialize with `app/utils.py::to_utc_iso` (appends `Z`), and compare against `datetime.now(timezone.utc).replace(tzinfo=None)`. Order numbers (`TNP-YYYYMMDD-XXXXXX`) use the Asia/Kathmandu date.

**Models**: `app/models/__init__.py` re-exports every model. Import from `app.models`. Product content fields (ingredients, nutrition_facts, faqs, etc.) are JSON columns.


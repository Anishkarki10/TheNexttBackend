from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.extensions import db
from app.models import Product, ProductDiscount
from app.services.audit import log_admin_action

bp = Blueprint("admin_discounts", __name__, url_prefix="/api/admin/discounts")


class _ValidationError(Exception):
    pass


def _parse_datetime(value, field: str) -> datetime:
    """Accepts an ISO 8601 string *with* a UTC offset (e.g. '2026-11-05T10:00+05:45'
    or '...Z') and returns naive UTC, matching how the DB stores datetimes.
    Offset-less input is rejected rather than guessed at."""
    if not isinstance(value, str) or not value:
        raise _ValidationError(f"'{field}' must be an ISO 8601 datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _ValidationError(f"'{field}' is not a valid ISO 8601 datetime")
    if parsed.tzinfo is None:
        raise _ValidationError(f"'{field}' must include a timezone offset")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_percent(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _ValidationError("'percent' must be a number")
    try:
        percent = Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise _ValidationError("'percent' must be a number")
    if not (Decimal(0) < percent < Decimal(100)):
        raise _ValidationError("'percent' must be greater than 0 and less than 100")
    return percent


def _apply_body(discount: ProductDiscount, body: dict, creating: bool) -> None:
    if creating or "product_id" in body:
        product_id = body.get("product_id")
        if not isinstance(product_id, int) or isinstance(product_id, bool) or not db.session.get(Product, product_id):
            raise _ValidationError("'product_id' must be an existing product id")
        discount.product_id = product_id
    if creating or "percent" in body:
        discount.percent = _parse_percent(body.get("percent"))
    if "label" in body:
        label = (body.get("label") or "").strip()
        if len(label) > 80:
            raise _ValidationError("'label' must be at most 80 characters")
        discount.label = label or None
    if creating or "starts_at" in body:
        discount.starts_at = _parse_datetime(body.get("starts_at"), "starts_at")
    if creating or "ends_at" in body:
        ends_at = body.get("ends_at")
        discount.ends_at = _parse_datetime(ends_at, "ends_at") if ends_at else None

    if discount.ends_at is not None and discount.ends_at <= discount.starts_at:
        raise _ValidationError("'ends_at' must be after 'starts_at'")


def _find_overlap(discount: ProductDiscount) -> ProductDiscount | None:
    """Another discount on the same product whose [starts_at, ends_at) window
    intersects this one's (a null ends_at runs forever)."""
    query = ProductDiscount.query.filter(ProductDiscount.product_id == discount.product_id)
    if discount.id is not None:
        query = query.filter(ProductDiscount.id != discount.id)
    if discount.ends_at is not None:
        query = query.filter(ProductDiscount.starts_at < discount.ends_at)
    query = query.filter(db.or_(ProductDiscount.ends_at.is_(None), ProductDiscount.ends_at > discount.starts_at))
    return query.first()


def _save(discount: ProductDiscount, body: dict, creating: bool):
    try:
        with db.session.no_autoflush:
            _apply_body(discount, body, creating)
            overlap = _find_overlap(discount)
    except _ValidationError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    if overlap:
        db.session.rollback()
        return (
            jsonify({"error": f"This overlaps another discount on the same product ({float(overlap.percent):g}% off, id {overlap.id})"}),
            409,
        )
    return None


@bp.get("")
@jwt_required()
def list_discounts():
    query = ProductDiscount.query
    product_id = request.args.get("product_id", type=int)
    if product_id:
        query = query.filter_by(product_id=product_id)
    discounts = query.order_by(ProductDiscount.starts_at.desc()).all()
    return jsonify([d.to_dict() for d in discounts])


@bp.post("")
@jwt_required()
def create_discount():
    body = request.get_json(silent=True) or {}
    discount = ProductDiscount()
    error = _save(discount, body, creating=True)
    if error:
        return error

    db.session.add(discount)
    db.session.flush()  # assigns discount.id before the log entry references it
    log_admin_action(
        "discount.create",
        target_type="product",
        target_id=discount.product_id,
        details={"discount_id": discount.id, "percent": float(discount.percent)},
    )
    db.session.commit()
    return jsonify(discount.to_dict()), 201


@bp.put("/<int:discount_id>")
@jwt_required()
def update_discount(discount_id):
    discount = ProductDiscount.query.get_or_404(discount_id)
    body = request.get_json(silent=True) or {}
    error = _save(discount, body, creating=False)
    if error:
        return error

    log_admin_action(
        "discount.update",
        target_type="product",
        target_id=discount.product_id,
        details={"discount_id": discount.id, "fields": sorted(body.keys())},
    )
    db.session.commit()
    return jsonify(discount.to_dict())


@bp.delete("/<int:discount_id>")
@jwt_required()
def delete_discount(discount_id):
    discount = ProductDiscount.query.get_or_404(discount_id)
    log_admin_action(
        "discount.delete",
        target_type="product",
        target_id=discount.product_id,
        details={"discount_id": discount.id, "percent": float(discount.percent)},
    )
    db.session.delete(discount)
    db.session.commit()
    return "", 204

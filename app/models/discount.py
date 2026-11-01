from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.extensions import db
from app.utils import to_utc_iso


def utc_now_naive() -> datetime:
    """Current time in the same naive-UTC form the DB stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def apply_percent_off(price: Decimal, percent: Decimal) -> Decimal:
    """Rounded to a whole rupee (half up), so customers see fixed prices
    like NPR 298 rather than NPR 297.50."""
    return (price * (Decimal(100) - percent) / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


class ProductDiscount(db.Model):
    """A percentage off every priced variant of a product, valid from
    starts_at until ends_at (exclusive; null = no end date). Windows for the
    same product never overlap - the admin API rejects that - so at most one
    discount is active for a product at any moment."""

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    percent = db.Column(db.Numeric(5, 2), nullable=False)  # 0 < percent < 100
    label = db.Column(db.String(80))  # optional, shown to customers, e.g. 'Dashain Offer'
    starts_at = db.Column(db.DateTime, nullable=False)  # naive UTC
    ends_at = db.Column(db.DateTime, nullable=True)  # naive UTC

    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

    def is_active_at(self, moment: datetime) -> bool:
        return self.starts_at <= moment and (self.ends_at is None or moment < self.ends_at)

    def status(self, moment: datetime | None = None) -> str:
        moment = moment or utc_now_naive()
        if moment < self.starts_at:
            return "scheduled"
        if self.ends_at is not None and moment >= self.ends_at:
            return "expired"
        return "active"

    def to_public_dict(self) -> dict:
        return {
            "percent": float(self.percent),
            "label": self.label,
            "ends_at": to_utc_iso(self.ends_at),
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "percent": float(self.percent),
            "label": self.label,
            "starts_at": to_utc_iso(self.starts_at),
            "ends_at": to_utc_iso(self.ends_at),
            "status": self.status(),
            "created_at": to_utc_iso(self.created_at),
        }

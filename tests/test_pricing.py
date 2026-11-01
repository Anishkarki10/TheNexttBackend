"""Unit tests for pricing helpers and discount model logic (no HTTP)."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.config import _normalize_database_url
from app.models import ProductDiscount, ProductVariant
from app.models.discount import apply_percent_off
from app.utils import to_utc_iso
from tests.conftest import utcnow


@pytest.mark.parametrize(
    "price, percent, expected",
    [
        (350, 15, 298),  # 297.5 rounds half up
        (350, 10, 315),
        (450, 12.5, 394),  # 393.75
        (999, 33, 669),  # 669.33
        (100, 50, 50),
        (1000, 99.99, 0),  # 0.1 rounds down
        (200, 0.25, 200),  # 199.5 rounds up back to full price
    ],
)
def test_apply_percent_off_rounds_to_whole_rupees_half_up(price, percent, expected):
    result = apply_percent_off(Decimal(price), Decimal(str(percent)))
    assert result == Decimal(expected)
    assert result == result.to_integral_value()


def test_discount_window_start_inclusive_end_exclusive():
    start = datetime(2026, 11, 1, 0, 0)
    end = datetime(2026, 11, 2, 0, 0)
    d = ProductDiscount(percent=10, starts_at=start, ends_at=end)
    assert not d.is_active_at(start - timedelta(seconds=1))
    assert d.is_active_at(start)
    assert d.is_active_at(end - timedelta(seconds=1))
    assert not d.is_active_at(end)


def test_discount_without_end_runs_forever():
    d = ProductDiscount(percent=10, starts_at=datetime(2026, 1, 1), ends_at=None)
    assert d.is_active_at(datetime(2099, 1, 1))
    assert d.status(datetime(2099, 1, 1)) == "active"


@pytest.mark.parametrize(
    "offset, expected",
    [(timedelta(hours=-1), "scheduled"), (timedelta(hours=12), "active"), (timedelta(days=3), "expired")],
)
def test_discount_status(offset, expected):
    start = datetime(2026, 11, 1)
    d = ProductDiscount(percent=10, starts_at=start, ends_at=start + timedelta(days=1))
    assert d.status(start + offset) == expected


def test_current_price_without_discount(product):
    variant = product.variants[0]
    assert variant.current_price() == (Decimal(350), None)


def test_current_price_with_active_discount(product, make_discount):
    discount = make_discount(product, percent=15)
    price, applied = product.variants[0].current_price()
    assert price == Decimal(298)
    assert applied.id == discount.id


def test_current_price_ignores_scheduled_and_expired(product, make_discount):
    make_discount(product, starts=timedelta(days=1), ends=timedelta(days=2))
    make_discount(product, starts=timedelta(days=-3), ends=timedelta(days=-2))
    assert product.variants[0].current_price() == (Decimal(350), None)


def test_current_price_treats_fully_rounded_away_discount_as_none(make_product, make_discount):
    product = make_product(price=100)
    make_discount(product, percent=0.4)  # 99.6 -> rounds back up to 100
    assert product.variants[0].current_price() == (Decimal(100), None)


def test_current_price_for_enquiry_only_variant(product, make_discount):
    make_discount(product)
    assert product.variants[1].current_price() == (None, None)


def test_current_price_for_detached_variant():
    assert ProductVariant(label="x", price_npr=Decimal(50)).current_price() == (Decimal(50), None)


def test_to_utc_iso():
    assert to_utc_iso(None) is None
    assert to_utc_iso(datetime(2026, 11, 1, 7, 30)) == "2026-11-01T07:30:00Z"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_normalize_database_url(url, expected):
    assert _normalize_database_url(url) == expected


def test_utcnow_is_naive():
    assert utcnow().tzinfo is None

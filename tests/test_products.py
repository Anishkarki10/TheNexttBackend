"""Public product API and admin product CRUD."""
import io
from datetime import timedelta

from app.models import AdminAuditLog, ProductDiscount, ProductVariant

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


# --- public -----------------------------------------------------------------


def test_list_only_available_products(client, make_product):
    visible = make_product()
    make_product(available=False)
    res = client.get("/api/products")
    assert res.status_code == 200
    assert [p["id"] for p in res.get_json()] == [visible.id]


def test_product_detail_and_404(client, product):
    res = client.get(f"/api/products/{product.slug}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["slug"] == product.slug
    assert "faqs" in body  # detailed fields only on detail endpoint

    res = client.get("/api/products/nope")
    assert res.status_code == 404
    assert res.get_json() == {"error": "Product not found"}


def test_product_without_discount_has_null_discount_fields(client, product):
    body = client.get("/api/products").get_json()[0]
    assert body["discount"] is None
    assert all(v["discounted_price_npr"] is None for v in body["variants"])


def test_product_with_active_discount_exposes_discounted_price(client, product, make_discount):
    make_discount(product, percent=15, label="Dashain Offer")
    body = client.get(f"/api/products/{product.slug}").get_json()
    assert body["discount"]["percent"] == 15.0
    assert body["discount"]["label"] == "Dashain Offer"
    assert body["discount"]["ends_at"].endswith("Z")
    priced, enquiry = body["variants"]
    assert priced["price_npr"] == 350.0  # list price unchanged
    assert priced["discounted_price_npr"] == 298.0
    assert enquiry["discounted_price_npr"] is None


def test_scheduled_discount_not_shown(client, product, make_discount):
    make_discount(product, starts=timedelta(hours=1))
    body = client.get("/api/products").get_json()[0]
    assert body["discount"] is None


# --- admin CRUD ---------------------------------------------------------------


def test_create_product_requires_fields(client, auth):
    for missing in ("slug", "name", "sku"):
        body = {"slug": "s", "name": "n", "sku": "k"}
        del body[missing]
        res = client.post("/api/admin/products", json=body, headers=auth)
        assert res.status_code == 400
        assert missing in res.get_json()["error"]


def test_create_product_with_variants(client, auth, db):
    res = client.post(
        "/api/admin/products",
        headers=auth,
        json={"slug": "new", "name": "New", "sku": "N-1", "variants": [{"label": "250 g", "price_npr": 200, "stock_qty": 5}]},
    )
    assert res.status_code == 201
    body = res.get_json()
    assert body["variants"][0]["price_npr"] == 200.0
    assert AdminAuditLog.query.filter_by(action="product.create", target_id=body["id"]).count() == 1


def test_create_product_duplicate_slug_and_sku(client, auth, product):
    res = client.post("/api/admin/products", headers=auth, json={"slug": product.slug, "name": "x", "sku": "other"})
    assert res.status_code == 409
    res = client.post("/api/admin/products", headers=auth, json={"slug": "other", "name": "x", "sku": product.sku})
    assert res.status_code == 409


def test_update_product_syncs_variants(client, auth, product, db):
    keep = product.variants[0]
    res = client.put(
        f"/api/admin/products/{product.id}",
        headers=auth,
        json={"name": "Renamed", "variants": [{"id": keep.id, "label": "500 g", "price_npr": 400, "stock_qty": 7}, {"label": "2 kg", "price_npr": 1500}]},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["name"] == "Renamed"
    assert [(v["label"], v["price_npr"]) for v in body["variants"]] == [("500 g", 400.0), ("2 kg", 1500.0)]
    assert ProductVariant.query.count() == 2  # the enquiry-only variant was removed


def test_update_product_conflicting_slug(client, auth, make_product):
    a, b = make_product(), make_product()
    res = client.put(f"/api/admin/products/{a.id}", headers=auth, json={"slug": b.slug})
    assert res.status_code == 409


def test_get_and_list_admin_products_include_unavailable(client, auth, make_product):
    hidden = make_product(available=False)
    assert hidden.id in [p["id"] for p in client.get("/api/admin/products", headers=auth).get_json()]
    assert client.get(f"/api/admin/products/{hidden.id}", headers=auth).status_code == 200
    assert client.get("/api/admin/products/9999", headers=auth).status_code == 404


def test_delete_product_cascades_discounts(client, auth, product, make_discount):
    make_discount(product)
    res = client.delete(f"/api/admin/products/{product.id}", headers=auth)
    assert res.status_code == 204
    assert ProductDiscount.query.count() == 0
    assert client.get(f"/api/products/{product.slug}").status_code == 404


def test_upload_valid_image_and_serve_it(client, auth, product):
    res = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth,
        data={"file": (io.BytesIO(PNG_BYTES), "photo.png")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 201
    url = res.get_json()["url"]
    assert url.startswith("/uploads/") and url.endswith(".png")
    assert client.get(url).data == PNG_BYTES


def test_upload_rejects_spoofed_and_unsupported_files(client, auth, product):
    cases = [
        (io.BytesIO(b"<?php echo 1; ?>"), "evil.png"),  # wrong magic bytes
        (io.BytesIO(PNG_BYTES), "script.svg"),  # extension not allowed
    ]
    for stream, name in cases:
        res = client.post(
            f"/api/admin/products/{product.id}/images",
            headers=auth,
            data={"file": (stream, name)},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400, name
    res = client.post(f"/api/admin/products/{product.id}/images", headers=auth, data={}, content_type="multipart/form-data")
    assert res.status_code == 400


def test_delete_image(client, auth, product):
    image = client.post(
        f"/api/admin/products/{product.id}/images",
        headers=auth,
        data={"file": (io.BytesIO(PNG_BYTES), "a.png")},
        content_type="multipart/form-data",
    ).get_json()
    assert client.delete(f"/api/admin/products/{product.id}/images/{image['id']}", headers=auth).status_code == 204
    assert client.delete(f"/api/admin/products/{product.id}/images/{image['id']}", headers=auth).status_code == 404

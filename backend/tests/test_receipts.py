"""Comprobantes de ventas y gastos: privados, una imagen o PDF por registro."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from test_sales import finished, sale


def img(fmt="JPEG", color=(200, 190, 170)):
    b = io.BytesIO()
    Image.new("RGB", (300, 500), color).save(b, fmt)
    return b.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def up(client, path, data, name="c.jpg", ctype="image/jpeg"):
    return client.post(path, files={"file": (name, data, ctype)})


def nueva_venta(client):
    p = finished(client)
    r = sale(client, [{"product_id": p["id"], "quantity": 1, "unit_price": 75000}])
    assert r.status_code == 201, r.text
    return r.json()


def nuevo_gasto(client):
    r = client.post("/expenses", json={"expense_date": "2021-09-05", "category": "shipping",
                                       "description": "Domicilio", "amount": 9000})
    assert r.status_code == 201, r.text
    return r.json()


def test_venta_con_comprobante(user, admin):
    s = nueva_venta(user)
    assert s["receipt"] is None
    assert user.get(f"/sales/{s['id']}/receipt").status_code == 404
    r = up(user, f"/sales/{s['id']}/receipt", img())
    assert r.status_code == 200, r.text
    d = user.get(f"/sales/{s['id']}").json()["receipt"]
    assert d and d["content_type"] == "image/jpeg" and not d["is_pdf"] and d["uploaded_by"]
    g = user.get(d["url"].removeprefix("/api"))
    assert g.status_code == 200 and g.headers["content-type"] == "image/jpeg"
    assert g.headers["cache-control"].startswith("private")
    # aparece también en el listado
    listed = next(x for x in user.get("/sales").json()["items"] if x["id"] == s["id"])
    assert listed["receipt"]["url"] == d["url"]
    # reemplazar por un PDF cambia la versión
    assert up(user, f"/sales/{s['id']}/receipt", PDF, "c.pdf", "application/pdf").status_code == 200
    d2 = user.get(f"/sales/{s['id']}").json()["receipt"]
    assert d2["is_pdf"] and d2["url"] != d["url"]
    # solo un administrador lo elimina
    assert user.delete(f"/sales/{s['id']}/receipt").status_code == 403
    assert admin.delete(f"/sales/{s['id']}/receipt").status_code == 200
    assert user.get(f"/sales/{s['id']}").json()["receipt"] is None
    assert admin.delete(f"/sales/{s['id']}/receipt").status_code == 404


def test_gasto_con_comprobante(user):
    e = nuevo_gasto(user)
    assert e["receipt"] is None
    assert up(user, f"/expenses/{e['id']}/receipt", img("PNG"), "c.png", "image/png").status_code == 200
    items = user.get("/expenses?month=2021-09").json()["items"]
    rec = next(x for x in items if x["id"] == e["id"])["receipt"]
    assert rec["content_type"] == "image/png"
    assert user.get(rec["url"].removeprefix("/api")).status_code == 200


def test_validaciones(user, admin):
    s = nueva_venta(user)
    path = f"/sales/{s['id']}/receipt"
    assert up(user, path, b"hola, no soy una imagen", "c.jpg").status_code == 422
    assert up(user, path, b"\xff\xd8\xff" + b"0" * 1_600_000).status_code == 413
    assert up(user, "/sales/999999999/receipt", img()).status_code == 404
    assert up(user, "/expenses/999999999/receipt", img()).status_code == 404
    # un registro anulado no cambia de comprobante
    admin.post(f"/sales/{s['id']}/void", json={"reason": "Prueba de anulación"})
    assert up(user, path, img()).status_code == 409
    e = nuevo_gasto(user)
    admin.post(f"/expenses/{e['id']}/void", json={"reason": "Prueba"})
    assert up(user, f"/expenses/{e['id']}/receipt", img()).status_code == 409


def test_privado(user, app_ready):
    from app.main import app
    s = nueva_venta(user)
    up(user, f"/sales/{s['id']}/receipt", img())
    anon = TestClient(app, base_url="https://testserver")
    assert anon.get(f"/sales/{s['id']}/receipt").status_code == 401
    assert anon.post(f"/sales/{s['id']}/receipt", files={"file": ("c.jpg", img(), "image/jpeg")}).status_code == 401

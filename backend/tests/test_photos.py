import io
from decimal import Decimal

from PIL import Image

from conftest import unique


def img(fmt="WEBP", size=(400, 300), color=(80, 120, 100)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, fmt)
    return b.getvalue()


def forro(client, size="S", quantity=1, sale_price=75000):
    r = client.post("/inventory", json={"sku": unique("FOR-S"), "name": unique("Forro S salvia"), "item_type": "finished_product",
                                        "unit": "unidad", "quantity": quantity, "unit_cost": 30000, "sale_price": sale_price,
                                        "size_code": size})
    assert r.status_code == 201, r.text
    return r.json()


def up(client, pid, kind, data, name="foto.webp", ctype="image/webp"):
    return client.post(f"/inventory/{pid}/photos/{kind}", files={"file": (name, data, ctype)})


def test_talla_solo_en_productos_terminados(user):
    f = forro(user, size="XL")
    assert f["size_code"] == "XL" and f["photos"] == {}
    r = user.post("/inventory", json={"sku": unique("TEL"), "name": "Tela", "item_type": "raw_material", "unit": "metro",
                                      "quantity": 1, "unit_cost": 1000, "size_code": "M"})
    assert r.status_code == 201 and r.json()["size_code"] is None
    bad = user.post("/inventory", json={"sku": unique("X"), "name": "X", "item_type": "finished_product", "unit": "unidad",
                                        "quantity": 1, "unit_cost": 1, "sale_price": 1, "size_code": "XXL"})
    assert bad.status_code == 422
    r = user.patch(f"/inventory/{f['id']}", json={**{k: f[k] for k in ("sku", "name", "item_type", "category", "unit", "min_stock", "unit_cost", "sale_price", "notes")}, "size_code": "M"})
    assert r.status_code == 200 and r.json()["size_code"] == "M"


def test_subir_ver_reemplazar_y_borrar(user, app_ready):
    f = forro(user)
    r = up(user, f["id"], "closed", img())
    assert r.status_code == 200, r.text
    url = r.json()["photos"]["closed"]
    assert url.startswith(f"/api/public/photos/{f['id']}/closed?v=")
    r = up(user, f["id"], "open", img("JPEG"), "abierta.jpg", "image/jpeg")
    assert set(r.json()["photos"]) == {"closed", "open"}
    # La foto se sirve sin sesión.
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app, base_url="https://testserver")
    g = anon.get(f"/public/photos/{f['id']}/open")
    assert g.status_code == 200 and g.headers["content-type"] == "image/jpeg" and g.content[:3] == b"\xff\xd8\xff"
    # Reemplazar con PNG cambia el tipo.
    up(user, f["id"], "open", img("PNG"), "a.png", "image/png")
    assert anon.get(f"/public/photos/{f['id']}/open").headers["content-type"] == "image/png"
    assert user.delete(f"/inventory/{f['id']}/photos/open").status_code == 200
    assert anon.get(f"/public/photos/{f['id']}/open").status_code == 404
    assert user.delete(f"/inventory/{f['id']}/photos/open").status_code == 404
    # Sin sesión no se puede subir.
    assert anon.post(f"/inventory/{f['id']}/photos/open", files={"file": ("a.webp", img(), "image/webp")}).status_code == 401


def test_rechaza_archivos_no_validos(user):
    f = forro(user)
    assert up(user, f["id"], "closed", b"no soy una imagen", "x.txt", "text/plain").status_code == 422
    assert up(user, f["id"], "closed", b"%PDF-1.4 falso", "x.webp", "image/webp").status_code == 422
    assert up(user, f["id"], "lateral", img()).status_code == 422
    big = b"\xff\xd8\xff" + b"0" * 1_600_000
    assert up(user, f["id"], "closed", big, "g.jpg", "image/jpeg").status_code == 413
    tela = user.post("/inventory", json={"sku": unique("TEL"), "name": "Tela", "item_type": "raw_material", "unit": "metro",
                                         "quantity": 1, "unit_cost": 1000}).json()
    assert up(user, tela["id"], "closed", img()).status_code == 422
    assert up(user, 999999, "closed", img()).status_code == 404


def test_catalogo_publico_por_talla(user, app_ready):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app, base_url="https://testserver")
    disponible = forro(user, size="M", quantity=1)
    up(user, disponible["id"], "closed", img())
    vendido = forro(user, size="M", quantity=0)
    items = {i["id"]: i for i in anon.get("/public/catalog").json()["items"]}
    a = items[disponible["id"]]
    assert a["size"] == "M" and a["available"] is True and a["ref"] == disponible["sku"]
    assert set(a["photos"]) == {"closed"} and Decimal(str(a["sale_price"])) == Decimal(75000)
    assert items[vendido["id"]]["available"] is False
    for i in items.values():
        assert "unit_cost" not in i and "quantity" not in i and "notes" not in i

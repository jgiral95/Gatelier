"""Unidades: cada pieza única de una referencia terminada, con sus dos fotos."""
import io
from decimal import Decimal

import psycopg
from fastapi.testclient import TestClient
from PIL import Image

from conftest import TEST_URL, unique


def D(v):
    return Decimal(str(v))


def img(fmt="WEBP", color=(80, 120, 100)):
    b = io.BytesIO()
    Image.new("RGB", (400, 500), color).save(b, fmt)
    return b.getvalue()


def ref(client, size="S", quantity=3, sale_price=75000):
    r = client.post("/inventory", json={"sku": unique("FOR-S"), "name": "Forro talla " + size, "item_type": "finished_product",
                                        "unit": "unidad", "quantity": quantity, "unit_cost": 30000, "sale_price": sale_price,
                                        "size_code": size})
    assert r.status_code == 201, r.text
    return r.json()


def add_unit(client, pid, name=""):
    return client.post(f"/inventory/{pid}/units", json={"name": name})


def up(client, uid, kind, data=None, name="f.webp", ctype="image/webp"):
    return client.post(f"/units/{uid}/photos/{kind}", files={"file": (name, data or img(), ctype)})


def anon():
    from app.main import app
    return TestClient(app, base_url="https://testserver")


def test_fichas_solo_hasta_las_existencias(user):
    p = ref(user, quantity=2)
    r1 = add_unit(user, p["id"], "salvia y crudo")
    assert r1.status_code == 201, r1.text
    u = r1.json()["units"][0]
    assert u["code"] == f"{p['sku']}-001" and u["name"] == "salvia y crudo" and u["status"] == "available"
    assert add_unit(user, p["id"]).json()["units"][0]["code"] == f"{p['sku']}-002"
    r3 = add_unit(user, p["id"])
    assert r3.status_code == 409                 # las 2 existencias ya tienen ficha
    d = user.get(f"/inventory/{p['id']}/units").json()
    assert d["product"]["units_available"] == 2 and len(d["units"]) == 2
    tela = user.post("/inventory", json={"sku": unique("T"), "name": "Tela", "item_type": "raw_material", "unit": "metro",
                                         "quantity": 5, "unit_cost": 1}).json()
    assert add_unit(user, tela["id"]).status_code == 422


def test_fotos_por_unidad_y_validaciones(user, app_ready):
    p = ref(user, quantity=1)
    uid = add_unit(user, p["id"]).json()["units"][0]["id"]
    r = up(user, uid, "closed")
    assert r.status_code == 200, r.text
    unit = r.json()["units"][0]
    assert unit["photos"]["closed"].startswith(f"/api/public/unit-photos/{uid}/closed?v=")
    assert r.json()["product"]["thumb"] and r.json()["product"]["units_with_photo"] == 1
    up(user, uid, "open", img("JPEG"), "a.jpg", "image/jpeg")
    a = anon()
    g = a.get(f"/public/unit-photos/{uid}/open")
    assert g.status_code == 200 and g.headers["content-type"] == "image/jpeg"
    assert up(user, uid, "closed", b"texto", "x.txt", "text/plain").status_code == 422
    assert up(user, uid, "lateral").status_code == 422
    assert up(user, uid, "closed", b"\xff\xd8\xff" + b"0" * 1_600_000, "g.jpg", "image/jpeg").status_code == 413
    assert a.post(f"/units/{uid}/photos/open", files={"file": ("a.webp", img(), "image/webp")}).status_code == 401
    assert user.delete(f"/units/{uid}/photos/open").status_code == 200
    assert a.get(f"/public/unit-photos/{uid}/open").status_code == 404


def test_tienda_muestra_solo_unidades_disponibles_con_foto(user, app_ready):
    p = ref(user, size="M", quantity=3, sale_price=80000)
    u1 = add_unit(user, p["id"], "ocre").json()["units"][0]["id"]
    u2 = add_unit(user, p["id"], "petróleo").json()["units"][0]["id"]
    add_unit(user, p["id"], "sin foto")
    up(user, u1, "closed"); up(user, u1, "open"); up(user, u2, "closed")
    items = {i["id"]: i for i in anon().get("/public/catalog").json()["items"]}
    styles = items[p["id"]]["styles"]
    assert [s["id"] for s in styles] == [u1, u2]
    assert set(styles[0]["photos"]) == {"closed", "open"} and styles[0]["name"] == "ocre"
    for i in items.values():
        assert "unit_cost" not in i and "quantity" not in i and "notes" not in i


def test_venta_por_unidad_y_anulacion(user, admin):
    p = ref(user, quantity=2)
    u1 = add_unit(user, p["id"], "uno").json()["units"][0]["id"]
    u2 = add_unit(user, p["id"], "dos").json()["units"][0]["id"]
    base = {"channel": "whatsapp", "payment_method": "transfer"}
    # Si todas las piezas tienen ficha, hay que decir cuál se vende.
    r = user.post("/sales", json={**base, "items": [{"product_id": p["id"], "quantity": 1, "unit_price": 75000}]})
    assert r.status_code == 422
    r = user.post("/sales", json={**base, "items": [{"product_id": p["id"], "quantity": 1, "unit_price": 75000, "unit_ids": [u2]}]})
    assert r.status_code == 201, r.text
    sale = r.json()
    assert sale["items"][0]["units"][0]["code"].endswith("-002")
    d = user.get(f"/inventory/{p['id']}/units").json()
    st = {u["id"]: u["status"] for u in d["units"]}
    assert st[u2] == "sold" and st[u1] == "available" and D(d["product"]["quantity"]) == D(1)
    # No se puede vender una unidad ya vendida ni una de otra referencia.
    assert user.post("/sales", json={**base, "items": [{"product_id": p["id"], "quantity": 1, "unit_price": 1, "unit_ids": [u2]}]}).status_code == 409
    otra = ref(user, quantity=1)
    assert user.post("/sales", json={**base, "items": [{"product_id": otra["id"], "quantity": 1, "unit_price": 1, "unit_ids": [u1]}]}).status_code == 422
    # Anular devuelve la unidad a la tienda.
    assert admin.post(f"/sales/{sale['id']}/void", json={"reason": "Cliente canceló"}).status_code == 200
    st = {u["id"]: u["status"] for u in user.get(f"/inventory/{p['id']}/units").json()["units"]}
    assert st[u2] == "available"


def test_piezas_sin_ficha_se_venden_sin_elegir(user):
    p = ref(user, quantity=3)
    add_unit(user, p["id"])
    r = user.post("/sales", json={"channel": "in_person", "payment_method": "cash",
                                  "items": [{"product_id": p["id"], "quantity": 2, "unit_price": 70000}]})
    assert r.status_code == 201, r.text      # quedan 1 existencia y 1 ficha: coherente
    r = user.post("/sales", json={"channel": "in_person", "payment_method": "cash",
                                  "items": [{"product_id": p["id"], "quantity": 1, "unit_price": 70000}]})
    assert r.status_code == 422


def test_ajuste_y_baja_respetan_las_fichas(user):
    p = ref(user, quantity=2)
    u1 = add_unit(user, p["id"]).json()["units"][0]["id"]
    add_unit(user, p["id"])
    r = user.post(f"/inventory/{p['id']}/adjust", json={"delta": 1, "direction": "remove", "note": "Se dañó"})
    assert r.status_code == 409
    r = user.post(f"/units/{u1}/remove", json={"reason": "Se manchó con tinta"})
    assert r.status_code == 200, r.text
    assert D(r.json()["product"]["quantity"]) == D(1)
    movs = user.get(f"/movements?product_id={p['id']}").json()["items"]
    assert "Se manchó con tinta" in movs[0]["note"]
    assert user.post(f"/units/{u1}/remove", json={"reason": "Otra vez"}).status_code == 409
    assert user.delete(f"/units/{u1}").status_code == 409          # la dada de baja se conserva
    # Eliminar una ficha disponible no cambia existencias.
    u2 = [u for u in user.get(f"/inventory/{p['id']}/units").json()["units"] if u["status"] == "available"][0]["id"]
    r = user.delete(f"/units/{u2}")
    assert r.status_code == 200 and D(r.json()["product"]["quantity"]) == D(1)


def test_no_quitar_tipo_terminado_con_fichas(user):
    p = ref(user, quantity=1)
    add_unit(user, p["id"])
    body = {k: p[k] for k in ("sku", "name", "category", "unit", "min_stock", "unit_cost", "notes")}
    r = user.patch(f"/inventory/{p['id']}", json={**body, "item_type": "work_in_progress"})
    assert r.status_code == 409


def test_migra_fotos_de_referencia_a_una_unidad(user, app_ready):
    p = ref(user, quantity=1)
    with psycopg.connect(TEST_URL) as c:
        c.execute("INSERT INTO product_photos(product_id,kind,content_type,data) VALUES(%s,'closed','image/webp',%s)", (p["id"], img()))
    from app.main import app
    with TestClient(app, base_url="https://testserver"):   # vuelve a ejecutar el arranque (migración)
        pass
    d = user.get(f"/inventory/{p['id']}/units").json()
    assert len(d["units"]) == 1 and d["units"][0]["code"] == f"{p['sku']}-001" and "closed" in d["units"][0]["photos"]
    with psycopg.connect(TEST_URL) as c:
        assert c.execute("SELECT COUNT(*) FROM product_photos WHERE product_id=%s", (p["id"],)).fetchone()[0] == 0

from datetime import date, timedelta
from decimal import Decimal

from conftest import unique


def D(v):
    return Decimal(str(v))


def finished(client, quantity=5, unit_cost=30000, sale_price=75000, unit="unidad"):
    r = client.post("/inventory", json={"sku": unique("FOR"), "name": unique("Forro"), "item_type": "finished_product",
                                        "unit": unit, "quantity": quantity, "unit_cost": unit_cost,
                                        "sale_price": sale_price, "min_stock": 0})
    assert r.status_code == 201, r.text
    return r.json()


def stock(client, pid):
    p = next(p for p in client.get("/inventory").json()["items"] if p["id"] == pid)
    return D(p["quantity"]), D(p["unit_cost"])


def sale(client, items, **extra):
    body = {"channel": "instagram", "payment_method": "transfer", "customer_name": "Laura",
            "customer_contact": "@laura.lee", "items": items, **extra}
    return client.post("/sales", json=body)


def test_venta_descuenta_stock_y_guarda_coste(user):
    xs = finished(user, quantity=5, unit_cost=30000)
    m = finished(user, quantity=2, unit_cost=40000, sale_price=80000)
    r = sale(user, [{"product_id": xs["id"], "quantity": 2, "unit_price": 70000},
                    {"product_id": m["id"], "quantity": 1, "unit_price": 80000}], discount=10000, shipping=12000)
    assert r.status_code == 201, r.text
    s = r.json()
    assert D(s["subtotal"]) == D(220000)
    assert D(s["net_revenue"]) == D(210000)          # sin el envío
    assert D(s["total"]) == D(222000)                # lo que pagó el cliente
    assert D(s["cost_total"]) == D(100000)           # 2×30.000 + 40.000
    assert D(s["margin"]) == D(110000)
    assert s["channel_label"] == "Instagram" and s["created_by"] == "taller_pruebas"
    assert stock(user, xs["id"])[0] == D(3)
    assert stock(user, m["id"])[0] == D(1)
    movs = user.get(f"/movements?product_id={xs['id']}").json()["items"]
    assert movs[0]["type"] == "sale" and D(movs[0]["delta"]) == D(-2) and f"Venta #{s['id']}" in movs[0]["note"]


def test_coste_historico_no_cambia(user, admin):
    p = finished(user, quantity=4, unit_cost=30000)
    s = sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 75000}]).json()
    # Una entrada posterior más cara cambia el promedio del producto, pero no la venta ya hecha.
    admin.post(f"/inventory/{p['id']}/receive", json={"quantity": 3, "unit_cost": 50000})
    again = user.get(f"/sales/{s['id']}").json()
    assert D(again["cost_total"]) == D(30000)
    assert D(again["items"][0]["unit_cost"]) == D(30000)


def test_no_permite_stock_negativo_ni_cambios_parciales(user):
    a = finished(user, quantity=3)
    b = finished(user, quantity=1)
    r = sale(user, [{"product_id": a["id"], "quantity": 2, "unit_price": 70000},
                    {"product_id": b["id"], "quantity": 2, "unit_price": 70000}])
    assert r.status_code == 409
    # Ninguno de los dos se descontó.
    assert stock(user, a["id"])[0] == D(3)
    assert stock(user, b["id"])[0] == D(1)


def test_validaciones(user):
    p = finished(user, quantity=3)
    assert sale(user, [{"product_id": p["id"], "quantity": 1.5, "unit_price": 70000}]).status_code == 422
    assert sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 70000}], discount=80000).status_code == 422
    assert sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 70000},
                       {"product_id": p["id"], "quantity": 1, "unit_price": 70000}]).status_code == 422
    assert sale(user, []).status_code == 422
    assert sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 1}], channel="tiktok").status_code == 422
    future = (date.today() + timedelta(days=3)).isoformat()
    assert sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 1}], sale_date=future).status_code == 422
    mat = user.post("/inventory", json={"sku": unique("TEL"), "name": "Tela", "item_type": "raw_material",
                                        "unit": "metro", "quantity": 10, "unit_cost": 1000}).json()
    assert sale(user, [{"product_id": mat["id"], "quantity": 1, "unit_price": 1}]).status_code == 422
    assert stock(user, p["id"])[0] == D(3)


def test_doble_envio_no_duplica(user):
    p = finished(user, quantity=3)
    ref = "venta-" + unique("x").replace("_", "-")
    r1 = sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 70000}], client_ref=ref)
    r2 = sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 70000}], client_ref=ref)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"] and r2.json()["duplicate"] is True
    assert stock(user, p["id"])[0] == D(2)


def test_anular_devuelve_stock_y_conserva_registro(user, admin):
    p = finished(user, quantity=3, unit_cost=30000)
    s = sale(user, [{"product_id": p["id"], "quantity": 2, "unit_price": 70000}]).json()
    assert user.post(f"/sales/{s['id']}/void", json={"reason": "Cliente canceló"}).status_code == 403
    assert admin.post(f"/sales/{s['id']}/void", json={"reason": "x"}).status_code == 422
    r = admin.post(f"/sales/{s['id']}/void", json={"reason": "Cliente canceló"})
    assert r.status_code == 200, r.text
    q, cost = stock(user, p["id"])
    assert q == D(3) and cost == D(30000)
    detail = user.get(f"/sales/{s['id']}").json()
    assert detail["status"] == "voided" and detail["void_reason"] == "Cliente canceló" and detail["voided_by"] == "admin_pruebas"
    assert admin.post(f"/sales/{s['id']}/void", json={"reason": "Otra vez"}).status_code == 409
    assert stock(user, p["id"])[0] == D(3)
    types = [m["type"] for m in user.get(f"/movements?product_id={p['id']}").json()["items"]]
    assert types[:2] == ["sale_void", "sale"]


def test_resumen_del_mes_excluye_anuladas(user, admin):
    p = finished(user, quantity=10, unit_cost=20000)
    month = "2026-08"
    ok = sale(user, [{"product_id": p["id"], "quantity": 2, "unit_price": 70000}], sale_date="2026-08-10",
              channel="whatsapp", shipping=10000).json()
    bad = sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 70000}], sale_date="2026-08-11").json()
    admin.post(f"/sales/{bad['id']}/void", json={"reason": "Error de registro"})
    sale(user, [{"product_id": p["id"], "quantity": 1, "unit_price": 70000}], sale_date="2026-07-30")
    d = user.get(f"/sales?month={month}").json()
    assert {s["id"] for s in d["items"]} == {ok["id"], bad["id"]}
    sm = d["summary"]
    assert sm["sales"] == 1 and D(sm["net_revenue"]) == D(140000) and D(sm["cost"]) == D(40000)
    assert D(sm["margin"]) == D(100000) and D(sm["units"]) == D(2) and D(sm["shipping"]) == D(10000)
    assert sm["by_channel"][0]["channel"] == "whatsapp"
    only_voided = user.get(f"/sales?month={month}&status=voided").json()["items"]
    assert [s["id"] for s in only_voided] == [bad["id"]]
    found = user.get(f"/sales?month={month}&search={p['sku']}").json()["items"]
    assert len(found) == 2


def test_opciones_solo_productos_terminados(user):
    p = finished(user, quantity=1)
    d = user.get("/sales/options").json()
    assert any(x["id"] == p["id"] for x in d["products"])
    assert set(d["channels"]) == {"whatsapp", "instagram", "in_person"}
    assert set(d["payment_methods"]) == {"cash", "transfer"}

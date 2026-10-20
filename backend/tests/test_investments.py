"""Inversiones y depreciación. Usa meses de 2023 para no cruzarse con otras pruebas."""
from decimal import Decimal

from conftest import unique


def D(v):
    return Decimal(str(v))


def inv(client, **kw):
    body = {"name": unique("Máquina"), "category": "equipment", "purchase_date": "2023-01-15",
            "amount": 1200000, "useful_life_months": 60, **kw}
    return client.post("/investments", json=body)


def find(client, iid):
    return next(i for i in client.get("/investments").json()["items"] if i["id"] == iid)


def test_solo_admin_registra(user, admin):
    assert inv(user).status_code == 403
    r = inv(admin)
    assert r.status_code == 201, r.text
    d = r.json()
    assert D(d["monthly_depreciation"]) == D(20000)
    assert d["last_month"] == "2027-12"
    assert user.get("/investments").status_code == 200


def test_validaciones(admin):
    assert inv(admin, salvage_value=1200000).status_code == 422
    assert inv(admin, useful_life_months=0).status_code == 422
    assert inv(admin, amount=0).status_code == 422
    assert inv(admin, purchase_date="2999-01-01").status_code == 422
    assert inv(admin, category="carro").status_code == 422


def test_depreciacion_en_el_gasto_mensual(admin, db):
    # Aísla 2023: borra inversiones de otras pruebas en ese año.
    db.execute("DELETE FROM investments WHERE purchase_date>='2023-01-01' AND purchase_date<'2024-01-01'")
    db.commit()
    # Máquina 1.200.000 en 60 meses = 20.000/mes. Plancha 300.000, reventa 60.000, 12 meses = 20.000/mes.
    m = inv(admin, purchase_date="2023-03-10").json()
    p = inv(admin, name="Plancha", category="tools", purchase_date="2023-05-02", amount=300000,
            salvage_value=60000, useful_life_months=12).json()
    get = lambda month: admin.get(f"/production/overheads/{month}").json()
    assert D(get("2023-02")["depreciation"]) == D(0)            # antes de comprar
    assert D(get("2023-03")["depreciation"]) == D(20000)        # desde el mes de compra
    assert D(get("2023-05")["depreciation"]) == D(40000)
    assert D(get("2024-04")["depreciation"]) == D(40000)        # último mes de la plancha
    assert D(get("2024-05")["depreciation"]) == D(20000)        # la plancha ya terminó
    # Con gasto fijo configurado, el coste por unidad incluye la depreciación.
    admin.put("/production/overheads/2023-06", json={"amount": 360000, "planned_units": 40})
    o = get("2023-06")
    assert D(o["amount"]) == D(360000) and D(o["total_amount"]) == D(400000) and D(o["cost_per_unit"]) == D(10000)
    # Dar de baja la máquina en septiembre: deja de sumar desde ese mes.
    assert admin.post(f"/investments/{m['id']}/retire", json={"retired_date": "2023-09-20", "reason": "Se vendió"}).status_code == 200
    assert D(get("2023-08")["depreciation"]) == D(40000)
    assert D(get("2023-09")["depreciation"]) == D(20000)
    assert admin.post(f"/investments/{m['id']}/retire", json={"retired_date": "2023-10-01", "reason": "Otra vez"}).status_code == 409
    mm = find(admin, m["id"])
    assert mm["status"] == "retired" and mm["months_charged"] == 6     # marzo a agosto
    assert D(mm["accumulated_depreciation"]) == D(120000)
    pp = find(admin, p["id"])
    assert pp["status"] == "fully_depreciated" and D(pp["book_value"]) == D(60000)
    # El informe de costes separa el gasto fijo de la depreciación.
    oh = admin.get("/costs/report?month=2023-06").json()["overhead"]
    assert D(oh["fixed_amount"]) == D(360000) and D(oh["depreciation"]) == D(40000) and D(oh["amount"]) == D(400000)


def test_orden_nueva_incluye_depreciacion(admin, db):
    db.execute("DELETE FROM investments WHERE purchase_date>='2023-01-01' AND purchase_date<'2024-01-01'")
    db.commit()
    inv(admin, purchase_date="2023-11-05", amount=240000, useful_life_months=24)          # 10.000/mes
    admin.put("/production/overheads/2023-11", json={"amount": 90000, "planned_units": 20})  # (90.000+10.000)/20 = 5.000
    mat = admin.post("/inventory", json={"sku": unique("M"), "name": "Tela", "item_type": "raw_material", "unit": "metro",
                                         "quantity": 10, "unit_cost": 1000}).json()
    fin = admin.post("/inventory", json={"sku": unique("F"), "name": "Forro", "item_type": "finished_product",
                                         "unit": "unidad", "quantity": 0, "sale_price": 70000}).json()
    rid = admin.post("/production/recipes", json={"finished_product_id": fin["id"], "name": unique("R"),
                                                   "materials": [{"product_id": mat["id"], "quantity_per_unit": 1}]}).json()["id"]
    o = admin.post("/production/orders", json={"recipe_id": rid, "quantity": 2, "monthly_period": "2023-11"}).json()
    assert D(o["monthly_overhead_total"]) == D(10000)


def test_editar_no_cambia_ordenes_anteriores(admin, db):
    db.execute("DELETE FROM investments WHERE purchase_date>='2023-01-01' AND purchase_date<'2024-01-01'")
    db.commit()
    i = inv(admin, purchase_date="2023-12-01", amount=120000, useful_life_months=12).json()   # 10.000/mes
    admin.put("/production/overheads/2023-12", json={"amount": 0, "planned_units": 10})
    mat = admin.post("/inventory", json={"sku": unique("M"), "name": "Tela", "item_type": "raw_material", "unit": "metro",
                                         "quantity": 10, "unit_cost": 1000}).json()
    fin = admin.post("/inventory", json={"sku": unique("F"), "name": "Forro", "item_type": "finished_product",
                                         "unit": "unidad", "quantity": 0, "sale_price": 70000}).json()
    rid = admin.post("/production/recipes", json={"finished_product_id": fin["id"], "name": unique("R"),
                                                   "materials": [{"product_id": mat["id"], "quantity_per_unit": 1}]}).json()["id"]
    o1 = admin.post("/production/orders", json={"recipe_id": rid, "quantity": 1, "monthly_period": "2023-12"}).json()
    assert D(o1["monthly_overhead_total"]) == D(1000)
    r = admin.patch(f"/investments/{i['id']}", json={"name": "Máquina corregida", "category": "equipment",
                                                     "purchase_date": "2023-12-01", "amount": 240000, "useful_life_months": 12})
    assert r.status_code == 200 and D(r.json()["monthly_depreciation"]) == D(20000)
    orders = admin.get("/production/orders").json()["items"]
    assert D(next(x for x in orders if x["id"] == o1["id"])["monthly_overhead_total"]) == D(1000)
    o2 = admin.post("/production/orders", json={"recipe_id": rid, "quantity": 1, "monthly_period": "2023-12"}).json()
    assert D(o2["monthly_overhead_total"]) == D(2000)


def test_resumen_y_recuperacion(user):
    d = user.get("/investments").json()
    assert d["summary"]["count"] >= 1
    assert d["recovery"] is not None and D(d["recovery"]["invested"]) > 0
    assert len(d["summary"]["month"]) == 7

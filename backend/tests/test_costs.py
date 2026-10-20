"""Informe de costes con un escenario de cifras conocidas, aislado en marzo de 2025."""
from decimal import Decimal

from conftest import unique

MONTH = "2025-03"


def D(v):
    return Decimal(str(v))


def item(client, item_type, unit, quantity, unit_cost, sale_price=None):
    body = {"sku": unique("C"), "name": unique("Item"), "item_type": item_type, "unit": unit,
            "quantity": quantity, "unit_cost": unit_cost, "min_stock": 0}
    if sale_price is not None:
        body["sale_price"] = sale_price
    r = client.post("/inventory", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_informe_del_mes(admin, user, db):
    # Producción: 4 forros previstos; 2 h/unidad a 10.000; lote 4.000; por unidad 1.000.
    tela = item(admin, "recovered_material", "metro", 20, 10000)     # 0,5 m por forro
    parche = item(admin, "raw_material", "unidad", 20, 2000)         # 1 por forro
    forro = item(admin, "finished_product", "unidad", 0, 0, sale_price=80000)
    admin.put(f"/production/overheads/{MONTH}", json={"amount": 100000, "planned_units": 10})   # 10.000 por unidad
    rid = admin.post("/production/recipes", json={
        "finished_product_id": forro["id"], "name": unique("Ficha"), "labor_hours_per_unit": 2, "hourly_rate": 10000,
        "materials": [{"product_id": tela["id"], "quantity_per_unit": 0.5}, {"product_id": parche["id"], "quantity_per_unit": 1}]}).json()["id"]
    oid = admin.post("/production/orders", json={"recipe_id": rid, "quantity": 4, "monthly_period": MONTH,
                                                "batch_overhead": 4000, "unit_overhead": 1000}).json()["id"]
    # Previsto: mat. 4×(5.000+2.000)=28.000 · MO 8 h=80.000 · lote 4.000 · unidad 4.000 · mensual 40.000 → 156.000
    r = admin.post(f"/production/orders/{oid}/complete", json={
        "good_quantity": 3, "defective_quantity": 1, "labor_hours": 9,
        "materials": [{"product_id": tela["id"], "quantity": 2.5}, {"product_id": parche["id"], "quantity": 4}]})
    assert r.status_code == 200, r.text
    # Real: tela 25.000 (recuperado) + parches 8.000 (materia prima) + MO 90.000 + lote 4.000 + unidad 4.000 + mensual 40.000 = 171.000
    assert D(r.json()["actual_total_cost"]) == D(171000)
    db.execute("UPDATE production_orders SET completed_at='2025-03-20 15:00-05' WHERE id=%s", (oid,))
    db.commit()

    # Ventas del mes: forro a 57.000 de coste (171.000 / 3 buenas).
    s1 = user.post("/sales", json={"sale_date": "2025-03-21", "channel": "instagram", "payment_method": "transfer",
                                   "items": [{"product_id": forro["id"], "quantity": 2, "unit_price": 80000}],
                                   "discount": 10000, "shipping": 12000}).json()
    s2 = user.post("/sales", json={"sale_date": "2025-03-25", "channel": "in_person", "payment_method": "cash",
                                   "items": [{"product_id": forro["id"], "quantity": 1, "unit_price": 80000}]}).json()
    admin.post(f"/sales/{s2['id']}/void", json={"reason": "Prueba de anulación"})
    assert s1["id"]

    d = user.get(f"/costs/report?month={MONTH}").json()

    sales = d["sales"]
    assert sales["count"] == 1
    assert D(sales["net_revenue"]) == D(150000)          # 160.000 − 10.000
    assert D(sales["cost"]) == D(114000)                  # 2 × 57.000
    assert D(sales["margin"]) == D(36000)
    assert D(sales["margin_pct"]) == D("24.0")
    assert D(sales["shipping"]) == D(12000)
    assert D(sales["units"]) == D(2)

    p = d["products"][0]
    assert p["product_id"] == forro["id"]
    assert D(p["discount"]) == D(10000) and D(p["net_revenue"]) == D(150000)
    assert D(p["avg_price"]) == D(75000) and D(p["unit_cost"]) == D(57000)

    prod = d["production"]
    assert prod["orders"] == 1
    assert D(prod["good_units"]) == D(3) and D(prod["defective_units"]) == D(1)
    assert D(prod["recovered_material"]) == D(25000)
    assert D(prod["raw_material"]) == D(8000)
    assert D(prod["labor"]) == D(90000)
    assert D(prod["batch_overhead"]) == D(4000)
    assert D(prod["unit_overhead"]) == D(4000)
    assert D(prod["monthly_overhead"]) == D(40000)
    assert D(prod["total"]) == D(171000) and D(prod["planned_total"]) == D(156000)
    assert D(prod["defect_rate"]) == D("25.0")
    assert D(prod["unit_cost"]) == D(57000)
    made = d["produced"][0]
    assert D(made["actual_unit_cost"]) == D(57000) and D(made["planned_unit_cost"]) == D(39000)

    oh = d["overhead"]
    assert oh["configured"] is True
    assert D(oh["amount"]) == D(100000) and D(oh["absorbed"]) == D(40000)
    assert D(oh["unabsorbed"]) == D(60000)                # se produjeron 4 de 10 unidades previstas
    assert D(oh["units_charged"]) == D(4)

    assert [t["month"] for t in d["trend"]] == ["2024-10", "2024-11", "2024-12", "2025-01", "2025-02", "2025-03"]
    assert D(d["trend"][-1]["margin"]) == D(36000) and d["trend"][0]["sales"] == 0


def test_mes_vacio_y_validacion(user):
    d = user.get("/costs/report?month=2019-01").json()
    assert d["sales"]["count"] == 0 and d["sales"]["margin_pct"] is None
    assert d["production"]["orders"] == 0 and d["production"]["unit_cost"] is None
    assert d["overhead"]["configured"] is False and d["products"] == []
    assert user.get("/costs/report?month=2019-13").status_code == 422
    assert user.get("/costs/report").status_code == 422

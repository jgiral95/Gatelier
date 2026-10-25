from decimal import Decimal

from conftest import unique


def D(v):
    return Decimal(str(v))


def make_product(client, item_type, unit="unidad", quantity=0, unit_cost=0, sale_price=None):
    body = {"sku": unique("SKU"), "name": unique("Artículo"), "item_type": item_type, "unit": unit,
            "quantity": quantity, "unit_cost": unit_cost, "min_stock": 0}
    if sale_price is not None:
        body["sale_price"] = sale_price
    r = client.post("/inventory", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def product(client, pid):
    items = client.get("/inventory").json()["items"]
    return next(p for p in items if p["id"] == pid)


def setup_order(client, destination="stock", quantity=4):
    """Forro de prueba: 1,5 m de tela (10.000/m) + 1 parche (2.000) por unidad, 2 h a 10.000/h."""
    tela = make_product(client, "recovered_material", unit="metro", quantity=20, unit_cost=10000)
    parche = make_product(client, "raw_material", unit="unidad", quantity=10, unit_cost=2000)
    forro = make_product(client, "finished_product", quantity=0, unit_cost=0, sale_price=75000)
    r = client.post("/production/recipes", json={
        "finished_product_id": forro["id"], "name": unique("Ficha"), "labor_hours_per_unit": 2, "hourly_rate": 10000,
        "materials": [{"product_id": tela["id"], "quantity_per_unit": 1.5, "waste_percent": 0},
                      {"product_id": parche["id"], "quantity_per_unit": 1, "waste_percent": 0}]})
    assert r.status_code == 201, r.text
    r = client.post("/production/orders", json={"recipe_id": r.json()["id"], "quantity": quantity,
                                               "destination_mode": destination, "monthly_period": "2026-10",
                                               "batch_overhead": 4000, "unit_overhead": 1000})
    assert r.status_code == 201, r.text
    return r.json()["id"], tela, parche, forro


# ----------------------------- Permisos -----------------------------

def test_categorias_solo_admin(user, admin):
    assert user.get("/categories").status_code == 200
    assert user.post("/categories", json={"name": unique("Cat")}).status_code == 403
    r = admin.post("/categories", json={"name": unique("Cat")})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert user.patch(f"/categories/{cid}", json={"name": unique("Otra")}).status_code == 403
    assert user.delete(f"/categories/{cid}").status_code == 403
    assert admin.delete(f"/categories/{cid}").status_code == 200


def test_gastos_mensuales_solo_admin(user, admin):
    body = {"amount": 600000, "planned_units": 60, "note": "Arriendo"}
    assert user.put("/production/overheads/2026-09", json=body).status_code == 403
    assert user.get("/production/overheads/2026-09").status_code == 200
    r = admin.put("/production/overheads/2026-09", json=body)
    assert r.status_code == 200
    assert D(r.json()["cost_per_unit"]) == D("10000.00")


# ----------------------------- Roles -----------------------------

def test_admin_cambia_roles(admin, user):
    me = user.get("/auth/me").json()
    assert user.patch(f"/admin/users/{me['id']}/role", json={"role": "admin"}).status_code == 403
    r = admin.patch(f"/admin/users/{me['id']}/role", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # El cambio aplica de inmediato, sin volver a iniciar sesión.
    assert user.get("/admin/users").status_code == 200
    r = admin.patch(f"/admin/users/{me['id']}/role", json={"role": "user"})
    assert r.status_code == 200 and r.json()["role"] == "user"
    assert user.get("/admin/users").status_code == 403
    assert admin.patch(f"/admin/users/{me['id']}/role", json={"role": "jefe"}).status_code == 422


def test_admin_no_se_quita_su_propio_permiso(admin):
    me = admin.get("/auth/me").json()
    r = admin.patch(f"/admin/users/{me['id']}/role", json={"role": "user"})
    assert r.status_code == 400
    assert admin.get("/auth/me").json()["role"] == "admin"


def test_no_se_puede_quitar_el_ultimo_admin(admin, user, db):
    # Deja a admin_pruebas como único administrador activo.
    db.execute("UPDATE app_users SET is_active=FALSE WHERE role='admin' AND username<>'admin_pruebas'")
    db.commit()
    me = user.get("/auth/me").json()
    admin.patch(f"/admin/users/{me['id']}/role", json={"role": "admin"})
    # Ese segundo admin intenta quitarle el permiso al primero: permitido porque quedan dos.
    first = admin.get("/auth/me").json()
    r = user.patch(f"/admin/users/{first['id']}/role", json={"role": "user"})
    assert r.status_code == 200
    # Ahora el único admin es taller_pruebas; nadie puede degradarlo (él mismo tampoco).
    assert user.patch(f"/admin/users/{me['id']}/role", json={"role": "user"}).status_code == 400
    db.execute("UPDATE app_users SET role='admin' WHERE username='admin_pruebas'")
    db.execute("UPDATE app_users SET role='user' WHERE username='taller_pruebas'")
    db.commit()


# ----------------------------- Producción -----------------------------

def test_iniciar_orden(admin):
    oid, *_ = setup_order(admin)
    r = admin.post(f"/production/orders/{oid}/start")
    assert r.status_code == 200 and r.json()["status"] == "in_progress"
    assert admin.post(f"/production/orders/{oid}/start").status_code == 409


def test_completar_sin_datos_equivale_a_lo_previsto(admin):
    oid, tela, parche, forro = setup_order(admin)
    r = admin.post(f"/production/orders/{oid}/complete")
    assert r.status_code == 200, r.text
    assert D(product(admin, tela["id"])["quantity"]) == D(14)      # 20 - 4×1,5
    assert D(product(admin, parche["id"])["quantity"]) == D(6)      # 10 - 4
    assert D(product(admin, forro["id"])["quantity"]) == D(4)
    detail = admin.get(f"/production/orders/{oid}").json()
    assert D(detail["actual_total_cost"]) == D(detail["total_cost"])


def test_completar_con_consumos_reales_y_defectuosas(admin):
    oid, tela, parche, forro = setup_order(admin)
    admin.post(f"/production/orders/{oid}/start")
    r = admin.post(f"/production/orders/{oid}/complete", json={
        "good_quantity": 3, "defective_quantity": 1, "labor_hours": 10,
        "materials": [{"product_id": tela["id"], "quantity": 7}, {"product_id": parche["id"], "quantity": 4}],
        "note": "Una costura salió torcida"})
    assert r.status_code == 200, r.text
    d = r.json()
    # Materiales: 7 m × 10.000 + 4 × 2.000 = 78.000. Mano de obra: 10 h × 10.000 = 100.000.
    # Gastos: lote 4.000 + por unidad 1.000 × 4 procesadas = 8.000. Total 186.000 / 3 buenas = 62.000.
    assert D(d["actual_material_cost"]) == D(78000)
    assert D(d["actual_labor_cost"]) == D(100000)
    assert D(d["actual_overhead_cost"]) == D(8000)
    assert D(d["actual_total_cost"]) == D(186000)
    assert D(d["actual_unit_cost"]) == D(62000)
    assert D(product(admin, tela["id"])["quantity"]) == D(13)
    f = product(admin, forro["id"])
    assert D(f["quantity"]) == D(3)
    assert D(f["unit_cost"]) == D(62000)
    detail = admin.get(f"/production/orders/{oid}").json()
    # Lo previsto se conserva separado de lo real.
    assert D(detail["total_cost"]) == D(156000)  # 68.000 mat. + 80.000 MO + 8.000 gastos
    assert D(detail["defective_quantity"]) == D(1)
    assert detail["completion_note"] == "Una costura salió torcida"
    assert detail["completed_by"] == "admin_pruebas"


def test_completar_valida_stock_y_unidades(admin):
    oid, tela, parche, _ = setup_order(admin)
    r = admin.post(f"/production/orders/{oid}/complete", json={"materials": [{"product_id": tela["id"], "quantity": 99}]})
    assert r.status_code == 409
    r = admin.post(f"/production/orders/{oid}/complete", json={"good_quantity": 2.5})
    assert r.status_code == 422
    r = admin.post(f"/production/orders/{oid}/complete", json={"good_quantity": 0, "defective_quantity": 0})
    assert r.status_code == 422
    r = admin.post(f"/production/orders/{oid}/complete", json={"materials": [{"product_id": 999999, "quantity": 1}]})
    assert r.status_code == 422
    # Nada de lo anterior tocó el inventario.
    assert D(product(admin, tela["id"])["quantity"]) == D(20)
    assert admin.get(f"/production/orders/{oid}").json()["status"] == "planned"


def test_completar_dos_veces_no_duplica_movimientos(admin):
    oid, tela, _, forro = setup_order(admin)
    assert admin.post(f"/production/orders/{oid}/complete").status_code == 200
    assert admin.post(f"/production/orders/{oid}/complete").status_code == 409
    assert D(product(admin, forro["id"])["quantity"]) == D(4)
    assert D(product(admin, tela["id"])["quantity"]) == D(14)


def test_encargo_no_suma_stock(admin):
    oid, _, _, forro = setup_order(admin, destination="customer_order")
    r = admin.post(f"/production/orders/{oid}/complete")
    assert r.status_code == 200
    assert D(product(admin, forro["id"])["quantity"]) == D(0)


def test_reabrir_revierte_todo(admin, user):
    oid, tela, parche, forro = setup_order(admin)
    admin.post(f"/production/orders/{oid}/complete", json={"good_quantity": 3, "defective_quantity": 1})
    assert user.post(f"/production/orders/{oid}/reopen").status_code == 403
    r = admin.post(f"/production/orders/{oid}/reopen")
    assert r.status_code == 200, r.text
    assert D(product(admin, tela["id"])["quantity"]) == D(20)
    assert D(product(admin, parche["id"])["quantity"]) == D(10)
    assert D(product(admin, forro["id"])["quantity"]) == D(0)
    detail = admin.get(f"/production/orders/{oid}").json()
    assert detail["status"] == "in_progress" and detail["reopen_count"] == 1
    assert detail["actual_total_cost"] is None
    # La reversión queda en el historial; no se borra nada.
    movs = admin.get(f"/movements?product_id={tela['id']}").json()["items"]
    assert [m["type"] for m in movs][:2] == ["production_reversal", "production_consumption"]
    # Reabrir dos veces no duplica la devolución.
    assert admin.post(f"/production/orders/{oid}/reopen").status_code == 409
    assert D(product(admin, tela["id"])["quantity"]) == D(20)
    # Se puede volver a cerrar con otros datos.
    r = admin.post(f"/production/orders/{oid}/complete", json={"good_quantity": 4})
    assert r.status_code == 200
    assert D(product(admin, forro["id"])["quantity"]) == D(4)


def test_reabrir_bloqueado_si_ya_no_hay_unidades(admin):
    oid, _, _, forro = setup_order(admin)
    admin.post(f"/production/orders/{oid}/complete")
    r = admin.post(f"/inventory/{forro['id']}/adjust", json={"delta": 2, "direction": "remove", "note": "Venta"})
    assert r.status_code == 200
    r = admin.post(f"/production/orders/{oid}/reopen")
    assert r.status_code == 409
    assert D(product(admin, forro["id"])["quantity"]) == D(2)


def test_cancelar_guarda_fecha(admin):
    oid, *_ = setup_order(admin)
    assert admin.post(f"/production/orders/{oid}/cancel").status_code == 200
    detail = admin.get(f"/production/orders/{oid}").json()
    assert detail["status"] == "cancelled" and detail["cancelled_at"]
    assert admin.post(f"/production/orders/{oid}/complete").status_code == 409


def test_catalogo_publico_no_expone_costes(admin):
    make_product(admin, "finished_product", quantity=2, unit_cost=50000, sale_price=80000)
    items = admin.get("/public/catalog").json()["items"]
    assert items and all(set(i) == {"id", "ref", "name", "unit", "sale_price", "available", "size", "styles"} for i in items)


def test_valor_de_productos_terminados(admin):
    from decimal import Decimal
    from conftest import unique
    before = admin.get("/inventory").json()["summary"]["finished"]
    admin.post("/inventory", json={"sku": unique("T"), "name": "Funda", "item_type": "finished_product", "unit": "unidad",
                                   "quantity": 3, "unit_cost": 30000, "sale_price": 80000})
    admin.post("/inventory", json={"sku": unique("T"), "name": "Funda sin precio", "item_type": "finished_product", "unit": "unidad",
                                   "quantity": 2, "unit_cost": 25000})
    admin.post("/inventory", json={"sku": unique("M"), "name": "Tela", "item_type": "raw_material", "unit": "metro",
                                   "quantity": 5, "unit_cost": 10000})
    f = admin.get("/inventory").json()["summary"]["finished"]
    D = lambda v: Decimal(str(v))
    assert D(f["units"]) - D(before["units"]) == 5
    assert D(f["cost_value"]) - D(before["cost_value"]) == 140000
    assert D(f["sale_value"]) - D(before["sale_value"]) == 240000
    assert D(f["units_without_price"]) - D(before["units_without_price"]) == 2

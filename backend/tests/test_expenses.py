"""Gastos del negocio. Usa meses de 2021 para no cruzarse con otras pruebas."""
import uuid
from decimal import Decimal


def D(v):
    return Decimal(str(v))


def gasto(client, **kw):
    body = {"expense_date": "2021-05-10", "category": "packaging", "description": "Bolsas de empaque",
            "amount": 25000, "payment_method": "cash", **kw}
    return client.post("/expenses", json=body)


def test_cualquier_cuenta_registra_y_solo_admin_anula(user, admin):
    r = gasto(user)
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["category_label"] == "Empaque" and e["status"] == "active" and e["created_by"]
    assert user.post(f"/expenses/{e['id']}/void", json={"reason": "Registrado dos veces"}).status_code == 403
    r = admin.post(f"/expenses/{e['id']}/void", json={"reason": "Registrado dos veces"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "voided" and r.json()["void_reason"] == "Registrado dos veces"
    assert admin.post(f"/expenses/{e['id']}/void", json={"reason": "Otra vez"}).status_code == 409
    assert admin.post("/expenses/999999999/void", json={"reason": "No existe"}).status_code == 404


def test_validaciones(user):
    assert gasto(user, amount=0).status_code == 422
    assert gasto(user, amount=-5).status_code == 422
    assert gasto(user, category="viajes").status_code == 422
    assert gasto(user, payment_method="tarjeta").status_code == 422
    assert gasto(user, description="x").status_code == 422
    assert gasto(user, expense_date="2999-01-01").status_code == 422


def test_envio_repetido_no_duplica(user):
    ref = uuid.uuid4().hex
    a = gasto(user, client_ref=ref, expense_date="2021-06-02")
    b = gasto(user, client_ref=ref, expense_date="2021-06-02")
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] == b.json()["id"] and b.json().get("duplicate") is True
    items = user.get("/expenses?month=2021-06").json()["items"]
    assert sum(1 for i in items if i["id"] == a.json()["id"]) == 1


def test_resumen_del_mes_y_costes(admin, db):
    db.execute("DELETE FROM operating_expenses WHERE expense_date>='2021-07-01' AND expense_date<'2021-09-01'")
    db.commit()
    gasto(admin, expense_date="2021-07-03", category="marketing", description="Pauta Instagram", amount=60000)
    gasto(admin, expense_date="2021-07-20", category="shipping", description="Domicilio entrega", amount=12000)
    gasto(admin, expense_date="2021-07-31", category="marketing", description="Pauta Instagram 2", amount=40000)
    anulado = gasto(admin, expense_date="2021-07-15", category="fees", description="Comisión", amount=5000).json()
    admin.post(f"/expenses/{anulado['id']}/void", json={"reason": "Era de otro mes"})
    gasto(admin, expense_date="2021-08-01", category="other", description="Mes siguiente", amount=9999)

    d = admin.get("/expenses?month=2021-07").json()
    s = d["summary"]
    assert D(s["total"]) == D(112000) and s["count"] == 3
    assert [(c["category"], D(c["amount"])) for c in s["by_category"]] == [("marketing", D(100000)), ("shipping", D(12000))]
    assert len(d["items"]) == 4  # el anulado se lista, pero no suma
    assert D(admin.get("/expenses?month=2021-08").json()["summary"]["previous_month_total"]) == D(112000)

    rep = admin.get("/costs/report?month=2021-07").json()
    assert D(rep["expenses"]["total"]) == D(112000)
    margin = D(rep["sales"]["margin"])
    assert D(rep["profit"]["amount"]) == margin - D(112000)
    july = next(t for t in rep["trend"] if t["month"] == "2021-07")
    assert D(july["expenses"]) == D(112000) and D(july["profit"]) == D(july["margin"]) - D(112000)


def test_requiere_sesion(app_ready):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app, base_url="https://testserver")
    assert anon.get("/expenses?month=2021-07").status_code == 401
    assert anon.post("/expenses", json={}).status_code in (401, 422)

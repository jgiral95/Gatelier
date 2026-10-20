import os
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone,date
from decimal import Decimal
from typing import Optional, Literal, List
import jwt, psycopg
from fastapi import FastAPI,Form,HTTPException,Query,Request,Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel,Field
from pwdlib import PasswordHash
from psycopg.errors import UniqueViolation

DATABASE_URL = os.environ["DATABASE_URL"]
DB = DATABASE_URL
SECRET = os.environ["SESSION_SECRET"]
COOKIE="gatelier_session"; HOURS=8; hasher=PasswordHash.recommended()

@asynccontextmanager
async def lifespan(app):
    with psycopg.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS app_users(
        id BIGSERIAL PRIMARY KEY, username VARCHAR(80) UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        # Migraciones aditivas: roles y revocación inmediata de sesiones tras resetear contraseña.
        c.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'user'")
        c.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0")
        c.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='app_users_role_valid') THEN
                ALTER TABLE app_users ADD CONSTRAINT app_users_role_valid CHECK (role IN ('admin','user'));
            END IF;
        END $$""")
        c.execute("""CREATE TABLE IF NOT EXISTS inventory_products(
        id BIGSERIAL PRIMARY KEY,sku VARCHAR(80) UNIQUE NOT NULL,name VARCHAR(180) NOT NULL,
        category VARCHAR(100) NOT NULL DEFAULT 'Sin categoría',unit VARCHAR(30) NOT NULL DEFAULT 'unidad',
        quantity NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK(quantity>=0),
        min_stock NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK(min_stock>=0),
        unit_cost NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(unit_cost>=0),notes TEXT NOT NULL DEFAULT '',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        # Migración no destructiva: los productos existentes quedan como "Por clasificar".
        c.execute("ALTER TABLE inventory_products ADD COLUMN IF NOT EXISTS item_type VARCHAR(30) NOT NULL DEFAULT 'to_classify'")
        c.execute("ALTER TABLE inventory_products ADD COLUMN IF NOT EXISTS sale_price NUMERIC(14,2)")
        c.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'inventory_products_sale_price_nonnegative') THEN
                ALTER TABLE inventory_products ADD CONSTRAINT inventory_products_sale_price_nonnegative CHECK (sale_price IS NULL OR sale_price >= 0);
            END IF;
        END $$""")
        c.execute("""CREATE TABLE IF NOT EXISTS inventory_movements(
        id BIGSERIAL PRIMARY KEY,product_id BIGINT NOT NULL REFERENCES inventory_products(id),
        user_id BIGINT REFERENCES app_users(id),movement_type VARCHAR(30) NOT NULL,
        quantity_delta NUMERIC(14,3) NOT NULL,quantity_after NUMERIC(14,3) NOT NULL,
        note TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("ALTER TABLE inventory_movements ADD COLUMN IF NOT EXISTS unit_cost_before NUMERIC(14,2)")
        c.execute("ALTER TABLE inventory_movements ADD COLUMN IF NOT EXISTS unit_cost_after NUMERIC(14,2)")
        c.execute("""CREATE TABLE IF NOT EXISTS inventory_categories(
        id BIGSERIAL PRIMARY KEY, name VARCHAR(100) UNIQUE NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        # Importa las categorías que ya existen sin alterar los productos actuales.
        c.execute("""INSERT INTO inventory_categories(name)
        SELECT DISTINCT COALESCE(NULLIF(BTRIM(category), ''), 'Sin categoría')
        FROM inventory_products ON CONFLICT (name) DO NOTHING""")
        # Producción: fichas, lista de materiales, órdenes y distribución de gastos mensuales.
        c.execute("""CREATE TABLE IF NOT EXISTS production_recipes(
            id BIGSERIAL PRIMARY KEY, finished_product_id BIGINT NOT NULL REFERENCES inventory_products(id),
            name VARCHAR(180) NOT NULL, description TEXT NOT NULL DEFAULT '',
            labor_hours_per_unit NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK(labor_hours_per_unit>=0),
            hourly_rate NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(hourly_rate>=0),
            is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by BIGINT REFERENCES app_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS production_recipe_materials(
            id BIGSERIAL PRIMARY KEY, recipe_id BIGINT NOT NULL REFERENCES production_recipes(id) ON DELETE CASCADE,
            product_id BIGINT NOT NULL REFERENCES inventory_products(id),
            quantity_per_unit NUMERIC(14,3) NOT NULL CHECK(quantity_per_unit>0),
            waste_percent NUMERIC(6,3) NOT NULL DEFAULT 0 CHECK(waste_percent>=0 AND waste_percent<=100),
            UNIQUE(recipe_id,product_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS production_monthly_overheads(
            period DATE PRIMARY KEY, amount NUMERIC(14,2) NOT NULL CHECK(amount>=0),
            planned_units NUMERIC(14,3) NOT NULL CHECK(planned_units>0),
            note TEXT NOT NULL DEFAULT '', updated_by BIGINT REFERENCES app_users(id), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS production_orders(
            id BIGSERIAL PRIMARY KEY, recipe_id BIGINT NOT NULL REFERENCES production_recipes(id),
            finished_product_id BIGINT NOT NULL REFERENCES inventory_products(id),
            planned_quantity NUMERIC(14,3) NOT NULL CHECK(planned_quantity>0),
            destination_mode VARCHAR(30) NOT NULL CHECK(destination_mode IN ('stock','customer_order')),
            status VARCHAR(30) NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_progress','completed','cancelled')),
            monthly_period DATE NOT NULL, labor_hours NUMERIC(14,3) NOT NULL DEFAULT 0,
            hourly_rate NUMERIC(14,2) NOT NULL DEFAULT 0, labor_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
            material_cost NUMERIC(14,2) NOT NULL DEFAULT 0, batch_overhead NUMERIC(14,2) NOT NULL DEFAULT 0,
            unit_overhead_total NUMERIC(14,2) NOT NULL DEFAULT 0, monthly_overhead_total NUMERIC(14,2) NOT NULL DEFAULT 0,
            total_cost NUMERIC(14,2) NOT NULL DEFAULT 0, unit_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '', created_by BIGINT REFERENCES app_users(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ)""")
        c.execute("""CREATE TABLE IF NOT EXISTS production_order_materials(
            id BIGSERIAL PRIMARY KEY, order_id BIGINT NOT NULL REFERENCES production_orders(id) ON DELETE CASCADE,
            product_id BIGINT NOT NULL REFERENCES inventory_products(id),
            required_quantity NUMERIC(14,3) NOT NULL CHECK(required_quantity>0),
            unit_cost_snapshot NUMERIC(14,2) NOT NULL DEFAULT 0,
            line_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
            consumed_quantity NUMERIC(14,3) NOT NULL DEFAULT 0)""")
        # Cierre con datos reales: unidades buenas/defectuosas, horas y costes reales separados de los previstos.
        for col in ("good_quantity NUMERIC(14,3)", "defective_quantity NUMERIC(14,3) NOT NULL DEFAULT 0",
                    "actual_labor_hours NUMERIC(14,3)", "actual_material_cost NUMERIC(14,2)",
                    "actual_labor_cost NUMERIC(14,2)", "actual_overhead_cost NUMERIC(14,2)",
                    "actual_total_cost NUMERIC(14,2)", "actual_unit_cost NUMERIC(14,2)",
                    "completion_note TEXT NOT NULL DEFAULT ''", "completed_by BIGINT REFERENCES app_users(id)",
                    "cancelled_at TIMESTAMPTZ", "reopened_at TIMESTAMPTZ", "reopen_count INTEGER NOT NULL DEFAULT 0"):
            c.execute(f"ALTER TABLE production_orders ADD COLUMN IF NOT EXISTS {col}")
        c.execute("ALTER TABLE production_order_materials ADD COLUMN IF NOT EXISTS actual_unit_cost NUMERIC(14,2)")
        # Ventas: cabecera, líneas con coste histórico y vínculo de cada movimiento con su venta.
        c.execute("""CREATE TABLE IF NOT EXISTS sales(
            id BIGSERIAL PRIMARY KEY, sale_date DATE NOT NULL DEFAULT CURRENT_DATE,
            channel VARCHAR(20) NOT NULL CHECK(channel IN ('whatsapp','instagram','in_person')),
            payment_method VARCHAR(20) NOT NULL CHECK(payment_method IN ('cash','transfer')),
            customer_name VARCHAR(160) NOT NULL DEFAULT '', customer_contact VARCHAR(160) NOT NULL DEFAULT '',
            subtotal NUMERIC(14,2) NOT NULL CHECK(subtotal>=0), discount NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(discount>=0),
            shipping NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(shipping>=0), total NUMERIC(14,2) NOT NULL CHECK(total>=0),
            cost_total NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(cost_total>=0),
            status VARCHAR(20) NOT NULL DEFAULT 'completed' CHECK(status IN ('completed','voided')),
            note TEXT NOT NULL DEFAULT '', client_ref VARCHAR(64) UNIQUE,
            created_by BIGINT REFERENCES app_users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            voided_by BIGINT REFERENCES app_users(id), voided_at TIMESTAMPTZ, void_reason TEXT NOT NULL DEFAULT '',
            CHECK(discount<=subtotal))""")
        c.execute("""CREATE TABLE IF NOT EXISTS sale_items(
            id BIGSERIAL PRIMARY KEY, sale_id BIGINT NOT NULL REFERENCES sales(id),
            product_id BIGINT NOT NULL REFERENCES inventory_products(id),
            quantity NUMERIC(14,3) NOT NULL CHECK(quantity>0), unit_price NUMERIC(14,2) NOT NULL CHECK(unit_price>=0),
            line_total NUMERIC(14,2) NOT NULL CHECK(line_total>=0),
            unit_cost NUMERIC(14,2) NOT NULL DEFAULT 0, line_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
            UNIQUE(sale_id,product_id))""")
        c.execute("CREATE INDEX IF NOT EXISTS sales_date_idx ON sales(sale_date)")
        c.execute("ALTER TABLE inventory_movements ADD COLUMN IF NOT EXISTS sale_id BIGINT REFERENCES sales(id)")
        # Órdenes completadas antes de esta versión consumieron exactamente lo previsto: se registran así una sola vez.
        c.execute("""UPDATE production_orders SET good_quantity=planned_quantity, actual_labor_hours=labor_hours,
            actual_material_cost=material_cost, actual_labor_cost=labor_cost,
            actual_overhead_cost=batch_overhead+unit_overhead_total+monthly_overhead_total,
            actual_total_cost=total_cost, actual_unit_cost=unit_cost
            WHERE status='completed' AND actual_total_cost IS NULL""")
        c.execute("""UPDATE production_order_materials om SET actual_unit_cost=om.unit_cost_snapshot
            FROM production_orders o WHERE o.id=om.order_id AND o.status='completed' AND om.actual_unit_cost IS NULL""")
    yield

app=FastAPI(title="Gatelier API",version="0.4.0",lifespan=lifespan)
def user_from(request):
    token=request.cookies.get(COOKIE)
    if not token: raise HTTPException(401,"Debes iniciar sesión")
    try:
        payload=jwt.decode(token,SECRET,algorithms=["HS256"])
        uid=int(payload["sub"])
        token_version=int(payload.get("sv",0))
    except (jwt.InvalidTokenError,KeyError,ValueError,TypeError): raise HTTPException(401,"Sesión inválida o vencida")
    with psycopg.connect(DB) as c:
        u=c.execute("SELECT id,username,is_active,role,session_version FROM app_users WHERE id=%s",(uid,)).fetchone()
    if not u or not u[2]: raise HTTPException(401,"Usuario inactivo")
    if token_version != u[4]: raise HTTPException(401,"La sesión fue revocada. Inicia sesión de nuevo.")
    return {"id":u[0],"username":u[1],"role":u[3]}

def require_admin(request:Request):
    u=user_from(request)
    if u["role"] != "admin": raise HTTPException(403,"Esta acción requiere permisos de administrador")
    return u

ITEM_TYPES = {"raw_material", "recovered_material", "work_in_progress", "finished_product", "to_classify"}
ITEM_TYPE_LABELS = {
    "raw_material": "Materia prima",
    "recovered_material": "Material recuperado",
    "work_in_progress": "Producto en proceso",
    "finished_product": "Producto terminado",
    "to_classify": "Por clasificar",
}
def pack(r):
    return {"id":r[0],"sku":r[1],"name":r[2],"item_type":r[3],"category":r[4],"unit":r[5],"quantity":r[6],"min_stock":r[7],"unit_cost":r[8],"sale_price":r[9],"notes":r[10],"updated_at":r[11]}

DISCRETE_UNITS={"unidad","rollo","paquete"}
def is_discrete_unit(unit:str)->bool:
    return (unit or "").strip().lower() in DISCRETE_UNITS
def require_integer_quantity(value:Decimal,unit:str,field_name:str="La cantidad"):
    if is_discrete_unit(unit) and value != value.to_integral_value():
        raise HTTPException(422,f"{field_name} debe ser un número entero cuando la unidad es {unit}.")
class Product(BaseModel):
    sku:str=Field(min_length=1,max_length=80); name:str=Field(min_length=1,max_length=180)
    item_type:str=Field(default="raw_material",max_length=30)
    category:str=Field(default="Sin categoría",max_length=100); unit:str=Field(default="unidad",max_length=30)
    quantity:Decimal=Field(default=Decimal(0),ge=0); min_stock:Decimal=Field(default=Decimal(0),ge=0)
    unit_cost:Decimal=Field(default=Decimal(0),ge=0); sale_price:Optional[Decimal]=Field(default=None,ge=0)
    notes:str=Field(default="",max_length=4000)
class ProductUpdate(BaseModel):
    sku:str=Field(min_length=1,max_length=80); name:str=Field(min_length=1,max_length=180)
    item_type:str=Field(default="raw_material",max_length=30)
    category:str=Field(default="Sin categoría",max_length=100); unit:str=Field(default="unidad",max_length=30)
    min_stock:Decimal=Field(ge=0); unit_cost:Decimal=Field(ge=0); sale_price:Optional[Decimal]=Field(default=None,ge=0)
    notes:str=Field(default="",max_length=4000)
class Receipt(BaseModel):
    quantity:Decimal=Field(gt=0,le=999999999)
    unit_cost:Decimal=Field(ge=0,le=999999999)
    note:str=Field(default='',max_length=500)
class CategoryCreate(BaseModel):
    name:str=Field(min_length=1,max_length=100)
class CategoryUpdate(BaseModel):
    name:str=Field(min_length=1,max_length=100)
class Adjustment(BaseModel):
    delta:Decimal=Field(gt=0,le=999999999); direction:str; note:str=Field(default="",max_length=500)

class AdminUserCreate(BaseModel):
    username: str = Field(min_length=3,max_length=80,pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10,max_length=256)
class AdminPasswordReset(BaseModel):
    password: str = Field(min_length=10,max_length=256)
class AdminUserStatus(BaseModel):
    is_active: bool
class AdminUserRole(BaseModel):
    role: Literal["admin","user"]

@app.get("/health")
def health():
    try:
        with psycopg.connect(DB,connect_timeout=3) as c:c.execute("SELECT 1")
        return {"status":"ok","database":"connected"}
    except Exception:return JSONResponse(status_code=503,content={"status":"error","database":"unavailable"})

@app.get("/public/catalog")
def public_catalog():
    """Public storefront catalog; never returns internal notes, costs, or exact stock."""
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT name,unit,sale_price,quantity>0
            FROM inventory_products
            WHERE is_active=TRUE AND item_type='finished_product' AND sale_price IS NOT NULL
            ORDER BY name,id""").fetchall()
    return {"items":[{"name":r[0],"unit":r[1],"sale_price":r[2],"available":r[3]} for r in rows]}

@app.post("/auth/login")
def login(response:Response,username:str=Form(...),password:str=Form(...)):
    with psycopg.connect(DB) as c:u=c.execute("SELECT id,username,password_hash,is_active,role,session_version FROM app_users WHERE username=%s",(username.strip(),)).fetchone()
    if not u or not u[3] or not hasher.verify(password,u[2]):raise HTTPException(401,"Usuario o contraseña incorrectos")
    now=datetime.now(timezone.utc); token=jwt.encode({"sub":str(u[0]),"sv":u[5],"iat":now,"exp":now+timedelta(hours=HOURS)},SECRET,algorithm="HS256")
    response.set_cookie(COOKIE,token,max_age=HOURS*3600,httponly=True,secure=True,samesite="lax",path="/")
    return {"message":"Sesión iniciada","username":u[1],"role":u[4]}
@app.get("/auth/me")
def me(request:Request):return user_from(request)
@app.post("/auth/logout")
def logout(response:Response):
    response.delete_cookie(COOKIE,path="/",secure=True,httponly=True,samesite="lax");return {"message":"Sesión cerrada"}
@app.get("/admin/users")
def admin_list_users(request:Request):
    require_admin(request)
    with psycopg.connect(DB) as c:
        rows=c.execute("SELECT id,username,role,is_active,created_at FROM app_users ORDER BY created_at DESC,id DESC").fetchall()
    return {"items":[{"id":r[0],"username":r[1],"role":r[2],"is_active":r[3],"created_at":r[4].isoformat() if r[4] else None} for r in rows]}

@app.post("/admin/users",status_code=201)
def admin_create_user(d:AdminUserCreate,request:Request):
    require_admin(request)
    username=d.username.strip()
    try:
        with psycopg.connect(DB) as c:
            row=c.execute("INSERT INTO app_users(username,password_hash,role,is_active) VALUES(%s,%s,'user',TRUE) RETURNING id,username,role,is_active,created_at",(username,hasher.hash(d.password))).fetchone()
    except UniqueViolation: raise HTTPException(409,"Ese nombre de usuario ya existe")
    return {"id":row[0],"username":row[1],"role":row[2],"is_active":row[3],"created_at":row[4].isoformat() if row[4] else None}

@app.patch("/admin/users/{user_id}/password")
def admin_reset_password(user_id:int,d:AdminPasswordReset,request:Request):
    require_admin(request)
    with psycopg.connect(DB) as c:
        row=c.execute("UPDATE app_users SET password_hash=%s,session_version=session_version+1 WHERE id=%s RETURNING id,username",(hasher.hash(d.password),user_id)).fetchone()
    if not row: raise HTTPException(404,"Usuario no encontrado")
    return {"message":f"Contraseña restablecida para {row[1]}. Sus sesiones anteriores han sido revocadas."}

@app.patch("/admin/users/{user_id}/status")
def admin_set_user_status(user_id:int,d:AdminUserStatus,request:Request):
    actor=require_admin(request)
    if user_id==actor["id"] and not d.is_active:
        raise HTTPException(400,"No puedes desactivar tu propia cuenta")
    with psycopg.connect(DB) as c:
        with c.transaction():
            c.execute("LOCK TABLE app_users IN EXCLUSIVE MODE")
            target=c.execute("SELECT id,username,role,is_active FROM app_users WHERE id=%s FOR UPDATE",(user_id,)).fetchone()
            if not target: raise HTTPException(404,"Usuario no encontrado")
            if target[2]=="admin" and target[3] and not d.is_active:
                n=c.execute("SELECT COUNT(*) FROM app_users WHERE role='admin' AND is_active=TRUE").fetchone()[0]
                if n<=1: raise HTTPException(400,"No se puede desactivar el último administrador activo")
            row=c.execute("UPDATE app_users SET is_active=%s,session_version=session_version+1 WHERE id=%s RETURNING username,is_active",(d.is_active,user_id)).fetchone()
    return {"message":("Usuario activado" if d.is_active else "Usuario desactivado")+f": {row[0]}","username":row[0],"is_active":row[1]}

@app.patch("/admin/users/{user_id}/role")
def admin_set_user_role(user_id:int,d:AdminUserRole,request:Request):
    actor=require_admin(request)
    if user_id==actor["id"] and d.role!="admin":
        raise HTTPException(400,"No puedes quitarte tus propios permisos de administrador")
    with psycopg.connect(DB) as c:
        with c.transaction():
            c.execute("LOCK TABLE app_users IN EXCLUSIVE MODE")
            target=c.execute("SELECT id,username,role,is_active FROM app_users WHERE id=%s FOR UPDATE",(user_id,)).fetchone()
            if not target: raise HTTPException(404,"Usuario no encontrado")
            if target[2]==d.role:
                return {"message":f"{target[1]} ya tiene ese permiso","username":target[1],"role":target[2]}
            if target[2]=="admin" and target[3]:
                n=c.execute("SELECT COUNT(*) FROM app_users WHERE role='admin' AND is_active=TRUE").fetchone()[0]
                if n<=1: raise HTTPException(400,"Debe quedar al menos un administrador activo")
            row=c.execute("UPDATE app_users SET role=%s WHERE id=%s RETURNING username,role",(d.role,user_id)).fetchone()
    label="Administrador" if row[1]=="admin" else "Usuario"
    return {"message":f"{row[0]} ahora es {label}","username":row[0],"role":row[1]}

@app.get("/dashboard")
def dashboard(request:Request):
    u=user_from(request);return {"message":"Bienvenido a Gatelier","username":u["username"],"modules":["Inventario","Ventas","Producción","Costes"]}
@app.get("/categories")
def list_categories(request:Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        rows=c.execute("SELECT id,name FROM inventory_categories WHERE is_active ORDER BY name").fetchall()
    return {"items":[{"id":r[0],"name":r[1]} for r in rows]}

@app.post("/categories",status_code=201)
def create_category(d:CategoryCreate,request:Request):
    require_admin(request); name=d.name.strip()
    if not name: raise HTTPException(422,"El nombre de la categoría es obligatorio")
    try:
        with psycopg.connect(DB) as c:
            r=c.execute("INSERT INTO inventory_categories(name) VALUES(%s) RETURNING id,name",(name,)).fetchone()
        return {"id":r[0],"name":r[1]}
    except UniqueViolation: raise HTTPException(409,"Ya existe una categoría con ese nombre")

@app.patch("/categories/{category_id}")
def rename_category(category_id:int,d:CategoryUpdate,request:Request):
    require_admin(request); name=d.name.strip()
    if not name: raise HTTPException(422,"El nombre de la categoría es obligatorio")
    try:
        with psycopg.connect(DB) as c:
            old=c.execute("SELECT name FROM inventory_categories WHERE id=%s AND is_active FOR UPDATE",(category_id,)).fetchone()
            if not old: raise HTTPException(404,"Categoría no encontrada")
            c.execute("UPDATE inventory_categories SET name=%s,updated_at=NOW() WHERE id=%s",(name,category_id))
            c.execute("UPDATE inventory_products SET category=%s,updated_at=NOW() WHERE category=%s",(name,old[0]))
            r=c.execute("SELECT id,name FROM inventory_categories WHERE id=%s",(category_id,)).fetchone()
        return {"id":r[0],"name":r[1]}
    except UniqueViolation: raise HTTPException(409,"Ya existe una categoría con ese nombre")

@app.delete("/categories/{category_id}")
def delete_category(category_id:int,request:Request):
    require_admin(request)
    with psycopg.connect(DB) as c:
        cat=c.execute("SELECT name FROM inventory_categories WHERE id=%s AND is_active FOR UPDATE",(category_id,)).fetchone()
        if not cat: raise HTTPException(404,"Categoría no encontrada")
        used=c.execute("SELECT COUNT(*) FROM inventory_products WHERE is_active AND category=%s",(cat[0],)).fetchone()[0]
        if used: raise HTTPException(409,f"No se puede eliminar: {used} producto(s) usan esta categoría. Reasígnalos primero.")
        c.execute("UPDATE inventory_categories SET is_active=FALSE,updated_at=NOW() WHERE id=%s",(category_id,))
    return {"message":"Categoría eliminada"}

@app.get("/inventory")
def inventory(request:Request,search:str=Query("",max_length=120),status:str=Query("all",pattern="^(all|low|ok)$"),item_type:str=Query("all",pattern="^(all|raw_material|recovered_material|work_in_progress|finished_product|to_classify)$")):
    user_from(request);term=f"%{search.strip()}%"
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT id,sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes,updated_at FROM inventory_products
        WHERE is_active AND (%s='' OR sku ILIKE %s OR name ILIKE %s OR category ILIKE %s)
        AND (%s='all' OR (%s='low' AND quantity<=min_stock) OR (%s='ok' AND quantity>min_stock))
        AND (%s='all' OR item_type=%s) ORDER BY name""",
        (search.strip(),term,term,term,status,status,status,item_type,item_type)).fetchall()
        totals=c.execute("""SELECT COUNT(*),COUNT(*) FILTER(WHERE quantity<=min_stock),COALESCE(SUM(quantity*unit_cost),0)
        FROM inventory_products WHERE is_active""").fetchone()
    return {"items":[pack(r) for r in rows],"summary":{"products":totals[0],"low_stock":totals[1],"inventory_value":totals[2]}}
@app.post("/inventory",status_code=201)
def create_product(d:Product,request:Request):
    u=user_from(request);sku=d.sku.strip();name=d.name.strip()
    if not sku or not name:raise HTTPException(422,"Referencia y nombre son obligatorios")
    if d.item_type not in ITEM_TYPES: raise HTTPException(422,"Tipo de artículo no válido")
    require_integer_quantity(d.quantity,d.unit,"La existencia inicial")
    require_integer_quantity(d.min_stock,d.unit,"El stock mínimo")
    sale_price = d.sale_price if d.item_type == "finished_product" else None
    try:
        with psycopg.connect(DB) as c:
            r=c.execute("""INSERT INTO inventory_products(sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes,updated_at""",
            (sku,name,d.item_type,d.category.strip() or "Sin categoría",d.unit.strip() or "unidad",d.quantity,d.min_stock,d.unit_cost,sale_price,d.notes.strip())).fetchone()
            if d.quantity>0:c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note)
            VALUES(%s,%s,'initial',%s,%s,'Existencia inicial')""",(r[0],u["id"],d.quantity,d.quantity))
        return pack(r)
    except UniqueViolation:raise HTTPException(409,"Ya existe un producto con esa referencia (SKU)")
@app.patch("/inventory/{pid}")
def update_product(pid:int,d:ProductUpdate,request:Request):
    user_from(request); sku=d.sku.strip(); name=d.name.strip(); category=d.category.strip() or "Sin categoría"
    if not sku or not name: raise HTTPException(422,"Referencia y nombre son obligatorios")
    if d.item_type not in ITEM_TYPES: raise HTTPException(422,"Tipo de artículo no válido")
    require_integer_quantity(d.min_stock,d.unit,"El stock mínimo")
    sale_price = d.sale_price if d.item_type == "finished_product" else None
    try:
        with psycopg.connect(DB) as c:
            row=c.execute("SELECT id,quantity,item_type FROM inventory_products WHERE id=%s AND is_active FOR UPDATE",(pid,)).fetchone()
            if not row: raise HTTPException(404,"Producto no encontrado")
            if row[2] == 'finished_product' and d.item_type != 'finished_product':
                linked=c.execute("SELECT 1 FROM production_recipes WHERE finished_product_id=%s AND is_active LIMIT 1",(pid,)).fetchone()
                if linked:
                    raise HTTPException(409,"Este producto está asociado a una o más fichas de producción. Cambia o desactiva primero esas fichas antes de quitarle el tipo Producto terminado.")
            require_integer_quantity(row[1],d.unit,"La existencia actual")
            out=c.execute("""UPDATE inventory_products SET sku=%s,name=%s,item_type=%s,category=%s,unit=%s,
            min_stock=%s,unit_cost=%s,sale_price=%s,notes=%s,updated_at=NOW() WHERE id=%s
            RETURNING id,sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes,updated_at""",
            (sku,name,d.item_type,category,d.unit.strip() or "unidad",d.min_stock,d.unit_cost,sale_price,d.notes.strip(),pid)).fetchone()
        return pack(out)
    except UniqueViolation: raise HTTPException(409,"Ya existe un producto con esa referencia (SKU)")

@app.post("/inventory/{pid}/receive")
def receive_stock(pid:int,d:Receipt,request:Request):
    """Registra una entrada valorizada y recalcula el coste promedio ponderado."""
    u=user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT id,quantity,unit_cost,unit FROM inventory_products WHERE id=%s AND is_active FOR UPDATE",(pid,)).fetchone()
        if not r: raise HTTPException(404,"Producto no encontrado")
        require_integer_quantity(d.quantity,r[3],"La cantidad recibida")
        old_qty,old_cost=r[1],r[2]
        new_qty=old_qty+d.quantity
        if new_qty <= 0: raise HTTPException(422,"La cantidad resultante debe ser positiva")
        weighted=((old_qty*old_cost)+(d.quantity*d.unit_cost))/new_qty
        # Mantiene precisión monetaria de dos decimales.
        weighted=weighted.quantize(Decimal('0.01'))
        c.execute("UPDATE inventory_products SET quantity=%s,unit_cost=%s,updated_at=NOW() WHERE id=%s",(new_qty,weighted,pid))
        note=d.note.strip() or "Entrada valorizada"
        c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
        VALUES(%s,%s,'receipt',%s,%s,%s,%s,%s)""",(pid,u['id'],d.quantity,new_qty,note,old_cost,weighted))
        out=c.execute("SELECT id,sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes,updated_at FROM inventory_products WHERE id=%s",(pid,)).fetchone()
    return {**pack(out),"receipt":{"quantity":d.quantity,"received_unit_cost":d.unit_cost,"previous_unit_cost":old_cost,"weighted_average_unit_cost":weighted}}

@app.post("/inventory/{pid}/adjust")
def adjust(pid:int,d:Adjustment,request:Request):
    u=user_from(request)
    if d.direction not in ("add","remove"):raise HTTPException(422,"Dirección inválida")
    delta=d.delta if d.direction=="add" else -d.delta
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT id,quantity,unit FROM inventory_products WHERE id=%s AND is_active FOR UPDATE",(pid,)).fetchone()
        if not r:raise HTTPException(404,"Producto no encontrado")
        require_integer_quantity(d.delta,r[2],"La cantidad del movimiento")
        new=r[1]+delta
        if new<0:raise HTTPException(409,"El ajuste dejaría el inventario en negativo")
        c.execute("UPDATE inventory_products SET quantity=%s,updated_at=NOW() WHERE id=%s",(new,pid))
        c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note)
        VALUES(%s,%s,%s,%s,%s,%s)""",(pid,u["id"],d.direction,delta,new,d.note.strip() or ("Entrada manual" if delta>0 else "Salida manual")))
        out=c.execute("SELECT id,sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes,updated_at FROM inventory_products WHERE id=%s",(pid,)).fetchone()
    return pack(out)
@app.get("/movements")
def all_movements(request:Request,search:str=Query("",max_length=120),movement_type:str=Query("all",max_length=30),product_id:Optional[int]=None):
    """Historial global de movimientos, opcionalmente filtrado por producto."""
    user_from(request)
    term=f"%{search.strip()}%"
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT m.id,m.movement_type,m.quantity_delta,m.quantity_after,m.note,m.created_at,
            u.username,m.unit_cost_before,m.unit_cost_after,p.id,p.sku,p.name,p.unit
            FROM inventory_movements m
            JOIN inventory_products p ON p.id=m.product_id
            LEFT JOIN app_users u ON u.id=m.user_id
            WHERE (%s IS NULL OR p.id=%s)
              AND (%s='' OR p.name ILIKE %s OR p.sku ILIKE %s OR m.note ILIKE %s OR COALESCE(u.username,'') ILIKE %s)
              AND (%s='all' OR m.movement_type=%s)
            ORDER BY m.created_at DESC,m.id DESC LIMIT 1000""",
            (product_id,product_id,search.strip(),term,term,term,term,movement_type,movement_type)).fetchall()
    labels={"initial":"Existencia inicial","receipt":"Entrada valorizada","add":"Entrada manual","remove":"Salida manual","production_consumption":"Consumo de producción","production_output":"Producto terminado de producción","production_reversal":"Reversión de producción","sale":"Venta","sale_void":"Venta anulada"}
    return {"items":[{"id":r[0],"type":r[1],"type_label":labels.get(r[1],r[1].replace('_',' ').capitalize()),
        "delta":r[2],"quantity_after":r[3],"note":r[4],"created_at":r[5],"username":r[6],
        "unit_cost_before":r[7],"unit_cost_after":r[8],"product_id":r[9],"sku":r[10],"product_name":r[11],"unit":r[12]} for r in rows]}

@app.get("/inventory/{pid}/movements")
def movements(pid:int,request:Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        if not c.execute("SELECT 1 FROM inventory_products WHERE id=%s AND is_active",(pid,)).fetchone():raise HTTPException(404,"Producto no encontrado")
        rows=c.execute("""SELECT m.id,m.movement_type,m.quantity_delta,m.quantity_after,m.note,m.created_at,u.username,m.unit_cost_before,m.unit_cost_after
        FROM inventory_movements m LEFT JOIN app_users u ON u.id=m.user_id WHERE m.product_id=%s ORDER BY m.created_at DESC LIMIT 100""",(pid,)).fetchall()
    return {"items":[{"id":r[0],"type":r[1],"delta":r[2],"quantity_after":r[3],"note":r[4],"created_at":r[5],"username":r[6],"unit_cost_before":r[7],"unit_cost_after":r[8]} for r in rows]}


# ------------------------- PRODUCCIÓN Y COSTES -------------------------
class RecipeMaterialInput(BaseModel):
    product_id: int
    quantity_per_unit: Decimal = Field(gt=0, le=999999)
    waste_percent: Decimal = Field(default=Decimal(0), ge=0, le=100)
class RecipeCreate(BaseModel):
    finished_product_id: int
    name: str = Field(min_length=1, max_length=180)
    description: str = Field(default="", max_length=3000)
    labor_hours_per_unit: Decimal = Field(default=Decimal(0), ge=0, le=999999)
    hourly_rate: Decimal = Field(default=Decimal(0), ge=0, le=999999999)
    materials: List[RecipeMaterialInput] = Field(min_length=1, max_length=100)
class MonthlyOverheadInput(BaseModel):
    amount: Decimal = Field(ge=0, le=999999999)
    planned_units: Decimal = Field(gt=0, le=999999999)
    note: str = Field(default="", max_length=1000)
class ProductionOrderCreate(BaseModel):
    recipe_id: int
    quantity: Decimal = Field(gt=0, le=999999)
    destination_mode: Literal["stock", "customer_order"] = "stock"
    monthly_period: str = Field(pattern=r"^\d{4}-\d{2}$")
    hourly_rate: Optional[Decimal] = Field(default=None, ge=0, le=999999999)
    batch_overhead: Decimal = Field(default=Decimal(0), ge=0, le=999999999)
    unit_overhead: Decimal = Field(default=Decimal(0), ge=0, le=999999999)
    note: str = Field(default="", max_length=1000)

class OrderMaterialActual(BaseModel):
    product_id: int
    quantity: Decimal = Field(ge=0, le=999999999)
class ProductionOrderComplete(BaseModel):
    good_quantity: Optional[Decimal] = Field(default=None, ge=0, le=999999)
    defective_quantity: Decimal = Field(default=Decimal(0), ge=0, le=999999)
    labor_hours: Optional[Decimal] = Field(default=None, ge=0, le=999999)
    materials: Optional[List[OrderMaterialActual]] = Field(default=None, max_length=100)
    note: str = Field(default="", max_length=1000)

def _month_date(value: str) -> date:
    try:
        return date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(422, "El periodo debe tener formato AAAA-MM")

def _recipe_dict(c, rid):
    row = c.execute("""SELECT r.id,r.finished_product_id,p.sku,p.name,p.unit,r.name,r.description,
        r.labor_hours_per_unit,r.hourly_rate,r.created_at
        FROM production_recipes r JOIN inventory_products p ON p.id=r.finished_product_id
        WHERE r.id=%s AND r.is_active""", (rid,)).fetchone()
    if not row: raise HTTPException(404, "Ficha de producción no encontrada")
    mats = c.execute("""SELECT rm.product_id,p.sku,p.name,p.unit,rm.quantity_per_unit,rm.waste_percent,p.unit_cost
        FROM production_recipe_materials rm JOIN inventory_products p ON p.id=rm.product_id
        WHERE rm.recipe_id=%s ORDER BY p.name""", (rid,)).fetchall()
    material_cost_per_unit = sum((m[4] * (Decimal(1)+m[5]/Decimal(100)) * m[6] for m in mats), Decimal(0))
    labor_per_unit = row[7] * row[8]
    return {"id":row[0],"finished_product_id":row[1],"sku":row[2],"product_name":row[3],"unit":row[4],
        "name":row[5],"description":row[6],"labor_hours_per_unit":row[7],"hourly_rate":row[8],
        "materials":[{"product_id":m[0],"sku":m[1],"name":m[2],"unit":m[3],"quantity_per_unit":m[4],"waste_percent":m[5],"current_unit_cost":m[6]} for m in mats],
        "material_cost_per_unit":material_cost_per_unit.quantize(Decimal('0.01')),
        "labor_cost_per_unit":labor_per_unit.quantize(Decimal('0.01')),
        "estimated_unit_cost":(material_cost_per_unit+labor_per_unit).quantize(Decimal('0.01'))}

@app.get("/production/material-options")
def production_material_options(request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT id,sku,name,item_type,unit,quantity,unit_cost FROM inventory_products
            WHERE is_active AND item_type <> 'finished_product' ORDER BY name""").fetchall()
        products=c.execute("""SELECT id,sku,name,unit,quantity,unit_cost FROM inventory_products
            WHERE is_active AND item_type='finished_product' ORDER BY name""").fetchall()
    return {"materials":[{"id":r[0],"sku":r[1],"name":r[2],"item_type":r[3],"unit":r[4],"quantity":r[5],"unit_cost":r[6]} for r in rows],
        "finished_products":[{"id":r[0],"sku":r[1],"name":r[2],"unit":r[3],"quantity":r[4],"unit_cost":r[5]} for r in products]}

@app.get("/production/recipes")
def production_recipes(request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        ids=c.execute("SELECT id FROM production_recipes WHERE is_active ORDER BY updated_at DESC,id DESC").fetchall()
        return {"items":[_recipe_dict(c,r[0]) for r in ids]}

@app.post("/production/recipes", status_code=201)
def create_production_recipe(d: RecipeCreate, request: Request):
    u=user_from(request); name=d.name.strip()
    if not name: raise HTTPException(422,"El nombre de la ficha es obligatorio")
    ids=[m.product_id for m in d.materials]
    if len(set(ids)) != len(ids): raise HTTPException(422,"No repitas el mismo material en una ficha")
    with psycopg.connect(DB) as c:
        finished=c.execute("SELECT id,item_type FROM inventory_products WHERE id=%s AND is_active FOR SHARE",(d.finished_product_id,)).fetchone()
        if not finished: raise HTTPException(404,"El producto terminado no existe o está inactivo")
        if finished[1] != 'finished_product': raise HTTPException(422,"La ficha debe asociarse a un artículo clasificado como producto terminado")
        for m in d.materials:
            material=c.execute("SELECT id,item_type FROM inventory_products WHERE id=%s AND is_active FOR SHARE",(m.product_id,)).fetchone()
            if not material: raise HTTPException(404,f"No se encuentra el material {m.product_id}")
            if m.product_id == d.finished_product_id: raise HTTPException(422,"El producto terminado no puede consumirse a sí mismo")
            if material[1] == 'finished_product': raise HTTPException(422,"Selecciona materias primas, materiales recuperados o productos en proceso, no otro producto terminado")
        rid=c.execute("""INSERT INTO production_recipes(finished_product_id,name,description,labor_hours_per_unit,hourly_rate,created_by)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING id""",(d.finished_product_id,name,d.description.strip(),d.labor_hours_per_unit,d.hourly_rate,u['id'])).fetchone()[0]
        for m in d.materials:
            c.execute("INSERT INTO production_recipe_materials(recipe_id,product_id,quantity_per_unit,waste_percent) VALUES(%s,%s,%s,%s)",(rid,m.product_id,m.quantity_per_unit,m.waste_percent))
        return _recipe_dict(c,rid)

@app.get("/production/overheads/{period}")
def get_monthly_overhead(period: str, request: Request):
    user_from(request); p=_month_date(period)
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT period,amount,planned_units,note FROM production_monthly_overheads WHERE period=%s",(p,)).fetchone()
    if not r: return {"period":period,"amount":Decimal(0),"planned_units":Decimal(1),"note":"","configured":False,"cost_per_unit":Decimal(0)}
    return {"period":r[0].strftime('%Y-%m'),"amount":r[1],"planned_units":r[2],"note":r[3],"configured":True,"cost_per_unit":(r[1]/r[2]).quantize(Decimal('0.01'))}

@app.put("/production/overheads/{period}")
def save_monthly_overhead(period: str, d: MonthlyOverheadInput, request: Request):
    u=require_admin(request); p=_month_date(period)
    with psycopg.connect(DB) as c:
        r=c.execute("""INSERT INTO production_monthly_overheads(period,amount,planned_units,note,updated_by,updated_at)
            VALUES(%s,%s,%s,%s,%s,NOW()) ON CONFLICT(period) DO UPDATE SET amount=EXCLUDED.amount,
            planned_units=EXCLUDED.planned_units,note=EXCLUDED.note,updated_by=EXCLUDED.updated_by,updated_at=NOW()
            RETURNING period,amount,planned_units,note""",(p,d.amount,d.planned_units,d.note.strip(),u['id'])).fetchone()
    return {"period":r[0].strftime('%Y-%m'),"amount":r[1],"planned_units":r[2],"note":r[3],"configured":True,"cost_per_unit":(r[1]/r[2]).quantize(Decimal('0.01'))}

ORDER_COLUMNS = """o.id,o.recipe_id,r.name,o.finished_product_id,p.sku,p.name,o.planned_quantity,p.unit,
    o.destination_mode,o.status,o.monthly_period,o.material_cost,o.labor_hours,o.hourly_rate,o.labor_cost,
    o.batch_overhead,o.unit_overhead_total,o.monthly_overhead_total,o.total_cost,o.unit_cost,o.note,o.created_at,o.started_at,o.completed_at,
    o.good_quantity,o.defective_quantity,o.actual_labor_hours,o.actual_material_cost,o.actual_labor_cost,o.actual_overhead_cost,
    o.actual_total_cost,o.actual_unit_cost,o.completion_note,cu.username,o.cancelled_at,o.reopened_at,o.reopen_count"""
ORDER_FROM = """FROM production_orders o JOIN production_recipes r ON r.id=o.recipe_id
    JOIN inventory_products p ON p.id=o.finished_product_id
    LEFT JOIN app_users cu ON cu.id=o.completed_by"""

def _order_dict(r):
    return {"id":r[0],"recipe_id":r[1],"recipe_name":r[2],"product_id":r[3],"sku":r[4],"product_name":r[5],
        "quantity":r[6],"unit":r[7],"destination_mode":r[8],"status":r[9],"monthly_period":r[10].strftime('%Y-%m'),
        "material_cost":r[11],"labor_hours":r[12],"hourly_rate":r[13],"labor_cost":r[14],"batch_overhead":r[15],
        "unit_overhead_total":r[16],"monthly_overhead_total":r[17],"total_cost":r[18],"unit_cost":r[19],"note":r[20],
        "created_at":r[21],"started_at":r[22],"completed_at":r[23],
        "good_quantity":r[24],"defective_quantity":r[25],"actual_labor_hours":r[26],"actual_material_cost":r[27],
        "actual_labor_cost":r[28],"actual_overhead_cost":r[29],"actual_total_cost":r[30],"actual_unit_cost":r[31],
        "completion_note":r[32],"completed_by":r[33],"cancelled_at":r[34],"reopened_at":r[35],"reopen_count":r[36]}

@app.get("/production/orders")
def production_orders(request: Request, status: str = Query("all",pattern="^(all|planned|in_progress|completed|cancelled)$")):
    user_from(request)
    with psycopg.connect(DB) as c:
        rows=c.execute(f"""SELECT {ORDER_COLUMNS} {ORDER_FROM}
            WHERE (%s='all' OR o.status=%s) ORDER BY o.created_at DESC,o.id DESC LIMIT 500""",(status,status)).fetchall()
    return {"items":[_order_dict(r) for r in rows]}

@app.get("/production/orders/{order_id}")
def production_order_detail(order_id: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute(f"SELECT {ORDER_COLUMNS} {ORDER_FROM} WHERE o.id=%s",(order_id,)).fetchone()
        if not r: raise HTTPException(404,"Orden de producción no encontrada")
        mats=c.execute("""SELECT om.product_id,p.sku,p.name,p.unit,om.required_quantity,om.consumed_quantity,
            om.unit_cost_snapshot,om.actual_unit_cost,p.quantity,p.unit_cost
            FROM production_order_materials om JOIN inventory_products p ON p.id=om.product_id
            WHERE om.order_id=%s ORDER BY p.name""",(order_id,)).fetchall()
    return {**_order_dict(r),"materials":[{"product_id":m[0],"sku":m[1],"name":m[2],"unit":m[3],
        "required_quantity":m[4],"consumed_quantity":m[5],"planned_unit_cost":m[6],"actual_unit_cost":m[7],
        "available_quantity":m[8],"current_unit_cost":m[9]} for m in mats]}

@app.post("/production/orders", status_code=201)
def create_production_order(d: ProductionOrderCreate, request: Request):
    u=user_from(request); period=_month_date(d.monthly_period)
    with psycopg.connect(DB) as c:
        recipe_row=c.execute("SELECT finished_product_id,labor_hours_per_unit,hourly_rate FROM production_recipes WHERE id=%s AND is_active FOR SHARE",(d.recipe_id,)).fetchone()
        if not recipe_row: raise HTTPException(404,"Ficha de producción no encontrada")
        product=c.execute("SELECT id,unit FROM inventory_products WHERE id=%s AND is_active FOR SHARE",(recipe_row[0],)).fetchone()
        require_integer_quantity(d.quantity,product[1],"La cantidad a fabricar")
        mats=c.execute("""SELECT rm.product_id,p.sku,p.name,p.unit,p.unit_cost,rm.quantity_per_unit,rm.waste_percent
            FROM production_recipe_materials rm JOIN inventory_products p ON p.id=rm.product_id
            WHERE rm.recipe_id=%s ORDER BY p.name""",(d.recipe_id,)).fetchall()
        if not mats: raise HTTPException(422,"La ficha no tiene materiales")
        qty=d.quantity; material_lines=[]; material_total=Decimal(0)
        for m in mats:
            req=(m[5]*(Decimal(1)+m[6]/Decimal(100))*qty).quantize(Decimal('0.001'))
            require_integer_quantity(req,m[3],f"El consumo de {m[2]}")
            line=(req*m[4]).quantize(Decimal('0.01'))
            material_total += line
            material_lines.append((m[0],req,m[4],line))
        rate=d.hourly_rate if d.hourly_rate is not None else recipe_row[2]
        labor_hours=(recipe_row[1]*qty).quantize(Decimal('0.001'))
        labor_cost=(labor_hours*rate).quantize(Decimal('0.01'))
        unit_overhead_total=(d.unit_overhead*qty).quantize(Decimal('0.01'))
        overhead=c.execute("SELECT amount,planned_units FROM production_monthly_overheads WHERE period=%s",(period,)).fetchone()
        monthly_total=Decimal(0)
        if overhead:
            monthly_total=(overhead[0]/overhead[1]*qty).quantize(Decimal('0.01'))
        total=(material_total+labor_cost+d.batch_overhead+unit_overhead_total+monthly_total).quantize(Decimal('0.01'))
        unit_cost=(total/qty).quantize(Decimal('0.01'))
        oid=c.execute("""INSERT INTO production_orders(recipe_id,finished_product_id,planned_quantity,destination_mode,monthly_period,
            labor_hours,hourly_rate,labor_cost,material_cost,batch_overhead,unit_overhead_total,monthly_overhead_total,total_cost,unit_cost,note,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (d.recipe_id,recipe_row[0],qty,d.destination_mode,period,labor_hours,rate,labor_cost,material_total,d.batch_overhead,
             unit_overhead_total,monthly_total,total,unit_cost,d.note.strip(),u['id'])).fetchone()[0]
        for line in material_lines:
            c.execute("INSERT INTO production_order_materials(order_id,product_id,required_quantity,unit_cost_snapshot,line_cost) VALUES(%s,%s,%s,%s,%s)",(oid,*line))
        return {"id":oid,"status":"planned","quantity":qty,"destination_mode":d.destination_mode,"material_cost":material_total,
            "labor_hours":labor_hours,"hourly_rate":rate,"labor_cost":labor_cost,"batch_overhead":d.batch_overhead,
            "unit_overhead_total":unit_overhead_total,"monthly_overhead_total":monthly_total,"total_cost":total,"unit_cost":unit_cost}

@app.post("/production/orders/{order_id}/start")
def start_production_order(order_id: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT status FROM production_orders WHERE id=%s FOR UPDATE",(order_id,)).fetchone()
        if not r: raise HTTPException(404,"Orden de producción no encontrada")
        if r[0] != 'planned': raise HTTPException(409,"Solo se pueden iniciar órdenes planificadas")
        c.execute("UPDATE production_orders SET status='in_progress',started_at=NOW() WHERE id=%s",(order_id,))
    return {"id":order_id,"status":"in_progress"}

@app.post("/production/orders/{order_id}/complete")
def complete_production_order(order_id: int, request: Request, d: Optional[ProductionOrderComplete] = None):
    """Cierra la orden con lo que realmente pasó. Sin datos, asume que se cumplió exactamente lo previsto."""
    u=user_from(request); d=d or ProductionOrderComplete()
    with psycopg.connect(DB) as c:
        o=c.execute("""SELECT id,finished_product_id,planned_quantity,destination_mode,status,hourly_rate,labor_hours,
            batch_overhead,unit_overhead_total,monthly_overhead_total
            FROM production_orders WHERE id=%s FOR UPDATE""",(order_id,)).fetchone()
        if not o: raise HTTPException(404,"Orden de producción no encontrada")
        if o[4] not in ('planned','in_progress'): raise HTTPException(409,"La orden ya fue cerrada o cancelada")
        planned=o[2]
        good=planned if d.good_quantity is None else d.good_quantity
        defective=d.defective_quantity
        processed=good+defective
        if processed<=0: raise HTTPException(422,"Registra al menos una unidad buena o defectuosa")
        out=c.execute("SELECT quantity,unit_cost,unit FROM inventory_products WHERE id=%s AND is_active FOR UPDATE",(o[1],)).fetchone()
        if not out: raise HTTPException(404,"El producto terminado ya no está activo")
        require_integer_quantity(good,out[2],"Las unidades buenas")
        require_integer_quantity(defective,out[2],"Las unidades defectuosas")
        mats=c.execute("""SELECT om.product_id,om.required_quantity,p.quantity,p.unit,p.name,p.unit_cost
            FROM production_order_materials om JOIN inventory_products p ON p.id=om.product_id
            WHERE om.order_id=%s ORDER BY om.product_id FOR UPDATE OF p""",(order_id,)).fetchall()
        actual={m.product_id:m.quantity for m in (d.materials or [])}
        known={m[0] for m in mats}
        extra=set(actual)-known
        if extra: raise HTTPException(422,"Solo puedes registrar consumos de los materiales de la orden")
        lines=[]; material_cost=Decimal(0)
        for m in mats:
            used=actual.get(m[0],m[1])
            require_integer_quantity(used,m[3],f"El consumo de {m[4]}")
            if m[2] < used: raise HTTPException(409,f"Stock insuficiente de {m[4]}: disponible {m[2]} {m[3]}, consumido {used} {m[3]}")
            line=(used*m[5]).quantize(Decimal('0.01'))
            material_cost+=line
            lines.append((m[0],used,m[2],m[5],m[4]))
        hours=o[6] if d.labor_hours is None else d.labor_hours
        labor_cost=(hours*o[5]).quantize(Decimal('0.01'))
        # Gastos por unidad y mensuales se asignan por unidad procesada (buenas + defectuosas); el de lote completo.
        ratio=processed/planned
        overhead=(o[7]+(o[8]+o[9])*ratio).quantize(Decimal('0.01'))
        total=(material_cost+labor_cost+overhead).quantize(Decimal('0.01'))
        unit_cost=(total/good).quantize(Decimal('0.01')) if good>0 else None
        for pid,used,stock,cost,name in lines:
            if used>0:
                newq=stock-used
                c.execute("UPDATE inventory_products SET quantity=%s,updated_at=NOW() WHERE id=%s",(newq,pid))
                c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
                    VALUES(%s,%s,'production_consumption',%s,%s,%s,%s,%s)""",(pid,u['id'],-used,newq,f"Consumo de producción · Orden #{order_id}",cost,cost))
            c.execute("UPDATE production_order_materials SET consumed_quantity=%s,actual_unit_cost=%s WHERE order_id=%s AND product_id=%s",(used,cost,order_id,pid))
        added=False
        if o[3]=='stock' and good>0:
            oldq,oldcost=out[0],out[1]; newq=oldq+good
            newcost=(((oldq*oldcost)+(good*unit_cost))/newq).quantize(Decimal('0.01'))
            c.execute("UPDATE inventory_products SET quantity=%s,unit_cost=%s,updated_at=NOW() WHERE id=%s",(newq,newcost,o[1]))
            c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
                VALUES(%s,%s,'production_output',%s,%s,%s,%s,%s)""",(o[1],u['id'],good,newq,f"Producción terminada · Orden #{order_id}",oldcost,newcost))
            added=True
        c.execute("""UPDATE production_orders SET status='completed',completed_at=NOW(),completed_by=%s,
            good_quantity=%s,defective_quantity=%s,actual_labor_hours=%s,actual_material_cost=%s,actual_labor_cost=%s,
            actual_overhead_cost=%s,actual_total_cost=%s,actual_unit_cost=%s,completion_note=%s WHERE id=%s""",
            (u['id'],good,defective,hours,material_cost,labor_cost,overhead,total,unit_cost,d.note.strip(),order_id))
    msg="Producción completada. Materiales descontados"
    msg+=f" y {good} unidad(es) buena(s) añadida(s) al inventario." if added else ("; producto por encargo, no se añade al stock disponible." if o[3]!='stock' else "; no hubo unidades buenas para añadir al inventario.")
    return {"id":order_id,"status":"completed","destination_mode":o[3],"good_quantity":good,"defective_quantity":defective,
        "actual_material_cost":material_cost,"actual_labor_cost":labor_cost,"actual_overhead_cost":overhead,
        "actual_total_cost":total,"actual_unit_cost":unit_cost,"message":msg}

@app.post("/production/orders/{order_id}/reopen")
def reopen_production_order(order_id: int, request: Request):
    """Revierte una orden completada con movimientos explícitos y la deja en proceso."""
    u=require_admin(request)
    with psycopg.connect(DB) as c:
        o=c.execute("""SELECT id,finished_product_id,destination_mode,status,good_quantity,actual_unit_cost
            FROM production_orders WHERE id=%s FOR UPDATE""",(order_id,)).fetchone()
        if not o: raise HTTPException(404,"Orden de producción no encontrada")
        if o[3]!='completed': raise HTTPException(409,"Solo se pueden reabrir órdenes completadas")
        if o[2]=='stock' and o[4] and o[4]>0:
            out=c.execute("SELECT quantity,unit_cost FROM inventory_products WHERE id=%s FOR UPDATE",(o[1],)).fetchone()
            if out[0] < o[4]:
                raise HTTPException(409,f"No se puede reabrir: el inventario del producto terminado ({out[0]}) es menor que las unidades que produjo esta orden ({o[4]}). Puede que ya se hayan vendido o usado.")
            newq=out[0]-o[4]
            newcost=out[1]
            if newq>0 and o[5] is not None:
                calc=((out[0]*out[1])-(o[4]*o[5]))/newq
                if calc>=0: newcost=calc.quantize(Decimal('0.01'))
            c.execute("UPDATE inventory_products SET quantity=%s,unit_cost=%s,updated_at=NOW() WHERE id=%s",(newq,newcost,o[1]))
            c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
                VALUES(%s,%s,'production_reversal',%s,%s,%s,%s,%s)""",(o[1],u['id'],-o[4],newq,f"Reapertura · Orden #{order_id}: se retiran unidades producidas",out[1],newcost))
        mats=c.execute("""SELECT om.product_id,om.consumed_quantity,p.quantity,p.unit_cost
            FROM production_order_materials om JOIN inventory_products p ON p.id=om.product_id
            WHERE om.order_id=%s AND om.consumed_quantity>0 ORDER BY om.product_id FOR UPDATE OF p""",(order_id,)).fetchall()
        for pid,used,stock,cost in mats:
            newq=stock+used
            c.execute("UPDATE inventory_products SET quantity=%s,updated_at=NOW() WHERE id=%s",(newq,pid))
            c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
                VALUES(%s,%s,'production_reversal',%s,%s,%s,%s,%s)""",(pid,u['id'],used,newq,f"Reapertura · Orden #{order_id}: devolución de material",cost,cost))
        c.execute("UPDATE production_order_materials SET consumed_quantity=0,actual_unit_cost=NULL WHERE order_id=%s",(order_id,))
        c.execute("""UPDATE production_orders SET status='in_progress',started_at=COALESCE(started_at,NOW()),completed_at=NULL,completed_by=NULL,
            good_quantity=NULL,defective_quantity=0,actual_labor_hours=NULL,actual_material_cost=NULL,actual_labor_cost=NULL,
            actual_overhead_cost=NULL,actual_total_cost=NULL,actual_unit_cost=NULL,completion_note='',
            reopened_at=NOW(),reopen_count=reopen_count+1 WHERE id=%s""",(order_id,))
    return {"id":order_id,"status":"in_progress","message":f"Orden #{order_id} reabierta. Materiales devueltos al inventario y producción revertida."}

@app.post("/production/orders/{order_id}/cancel")
def cancel_production_order(order_id: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT status FROM production_orders WHERE id=%s FOR UPDATE",(order_id,)).fetchone()
        if not r: raise HTTPException(404,"Orden de producción no encontrada")
        if r[0] not in ('planned','in_progress'): raise HTTPException(409,"Solo se pueden cancelar órdenes abiertas")
        c.execute("UPDATE production_orders SET status='cancelled',cancelled_at=NOW() WHERE id=%s",(order_id,))
    return {"id":order_id,"status":"cancelled"}


# ------------------------------- VENTAS -------------------------------
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ=ZoneInfo(os.environ.get("APP_TIMEZONE","America/Bogota"))
except Exception:
    LOCAL_TZ=timezone(timedelta(hours=-5))  # Colombia no usa horario de verano
def local_today()->date:
    return datetime.now(LOCAL_TZ).date()

SALE_CHANNELS={"whatsapp":"WhatsApp","instagram":"Instagram","in_person":"Presencial"}
PAYMENT_METHODS={"cash":"Efectivo","transfer":"Transferencia"}

class SaleItemInput(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0, le=999999)
    unit_price: Decimal = Field(ge=0, le=999999999)
class SaleCreate(BaseModel):
    sale_date: Optional[date] = None
    channel: Literal["whatsapp","instagram","in_person"]
    payment_method: Literal["cash","transfer"]
    customer_name: str = Field(default="", max_length=160)
    customer_contact: str = Field(default="", max_length=160)
    items: List[SaleItemInput] = Field(min_length=1, max_length=50)
    discount: Decimal = Field(default=Decimal(0), ge=0, le=999999999)
    shipping: Decimal = Field(default=Decimal(0), ge=0, le=999999999)
    note: str = Field(default="", max_length=1000)
    client_ref: Optional[str] = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
class SaleVoid(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

SALE_COLUMNS="""s.id,s.sale_date,s.channel,s.payment_method,s.customer_name,s.customer_contact,s.subtotal,s.discount,
    s.shipping,s.total,s.cost_total,s.status,s.note,cu.username,s.created_at,vu.username,s.voided_at,s.void_reason"""
SALE_FROM="""FROM sales s LEFT JOIN app_users cu ON cu.id=s.created_by LEFT JOIN app_users vu ON vu.id=s.voided_by"""

def _sale_dict(r, items=None):
    net=r[6]-r[7]
    d={"id":r[0],"sale_date":r[1].isoformat(),"channel":r[2],"channel_label":SALE_CHANNELS.get(r[2],r[2]),
       "payment_method":r[3],"payment_label":PAYMENT_METHODS.get(r[3],r[3]),"customer_name":r[4],"customer_contact":r[5],
       "subtotal":r[6],"discount":r[7],"shipping":r[8],"total":r[9],"net_revenue":net,"cost_total":r[10],
       "margin":net-r[10],"status":r[11],"note":r[12],"created_by":r[13],"created_at":r[14],
       "voided_by":r[15],"voided_at":r[16],"void_reason":r[17]}
    if items is not None: d["items"]=items
    return d

def _sale_items(c, ids):
    if not ids: return {}
    rows=c.execute("""SELECT si.sale_id,si.product_id,p.sku,p.name,p.unit,si.quantity,si.unit_price,si.line_total,si.unit_cost,si.line_cost
        FROM sale_items si JOIN inventory_products p ON p.id=si.product_id WHERE si.sale_id=ANY(%s) ORDER BY si.id""",(list(ids),)).fetchall()
    out={}
    for r in rows:
        out.setdefault(r[0],[]).append({"product_id":r[1],"sku":r[2],"name":r[3],"unit":r[4],"quantity":r[5],
            "unit_price":r[6],"line_total":r[7],"unit_cost":r[8],"line_cost":r[9]})
    return out

@app.get("/sales/options")
def sales_options(request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT id,sku,name,unit,quantity,sale_price FROM inventory_products
            WHERE is_active AND item_type='finished_product' ORDER BY name""").fetchall()
    return {"products":[{"id":r[0],"sku":r[1],"name":r[2],"unit":r[3],"quantity":r[4],"sale_price":r[5]} for r in rows],
            "channels":SALE_CHANNELS,"payment_methods":PAYMENT_METHODS}

@app.get("/sales")
def list_sales(request: Request, month: str = Query("", pattern=r"^(\d{4}-\d{2})?$"),
               channel: str = Query("all", pattern="^(all|whatsapp|instagram|in_person)$"),
               status: str = Query("all", pattern="^(all|completed|voided)$"),
               search: str = Query("", max_length=120)):
    user_from(request)
    start=_month_date(month) if month else None
    end=(date(start.year+(start.month==12), start.month%12+1, 1)) if start else None
    term=f"%{search.strip()}%"
    with psycopg.connect(DB) as c:
        where="""WHERE (%(start)s::date IS NULL OR (s.sale_date>=%(start)s AND s.sale_date<%(end)s))
            AND (%(channel)s='all' OR s.channel=%(channel)s) AND (%(status)s='all' OR s.status=%(status)s)
            AND (%(search)s='' OR s.customer_name ILIKE %(term)s OR s.customer_contact ILIKE %(term)s OR s.note ILIKE %(term)s
                 OR EXISTS(SELECT 1 FROM sale_items si JOIN inventory_products p ON p.id=si.product_id
                           WHERE si.sale_id=s.id AND (p.name ILIKE %(term)s OR p.sku ILIKE %(term)s)))"""
        params={"start":start,"end":end,"channel":channel,"status":status,"search":search.strip(),"term":term}
        rows=c.execute(f"SELECT {SALE_COLUMNS} {SALE_FROM} {where} ORDER BY s.sale_date DESC,s.id DESC LIMIT 500",params).fetchall()
        items=_sale_items(c,[r[0] for r in rows])
        agg=c.execute(f"""SELECT COUNT(*),COALESCE(SUM(s.subtotal-s.discount),0),COALESCE(SUM(s.shipping),0),
            COALESCE(SUM(s.cost_total),0),COALESCE(SUM(s.discount),0)
            {SALE_FROM} {where} AND s.status='completed'""",params).fetchone()
        units=c.execute(f"""SELECT COALESCE(SUM(si.quantity),0) FROM sale_items si JOIN sales s ON s.id=si.sale_id
            LEFT JOIN app_users cu ON cu.id=s.created_by LEFT JOIN app_users vu ON vu.id=s.voided_by
            {where} AND s.status='completed'""",params).fetchone()[0]
        by_channel=c.execute(f"""SELECT s.channel,COUNT(*),COALESCE(SUM(s.subtotal-s.discount),0) {SALE_FROM} {where}
            AND s.status='completed' GROUP BY s.channel ORDER BY 3 DESC""",params).fetchall()
    return {"items":[_sale_dict(r,items.get(r[0],[])) for r in rows],
            "summary":{"sales":agg[0],"net_revenue":agg[1],"shipping":agg[2],"cost":agg[3],"discounts":agg[4],
                       "margin":agg[1]-agg[3],"units":units,
                       "by_channel":[{"channel":r[0],"label":SALE_CHANNELS.get(r[0],r[0]),"sales":r[1],"net_revenue":r[2]} for r in by_channel]}}

@app.get("/sales/{sale_id}")
def sale_detail(sale_id: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute(f"SELECT {SALE_COLUMNS} {SALE_FROM} WHERE s.id=%s",(sale_id,)).fetchone()
        if not r: raise HTTPException(404,"Venta no encontrada")
        return _sale_dict(r,_sale_items(c,[sale_id]).get(sale_id,[]))

@app.post("/sales", status_code=201)
def create_sale(d: SaleCreate, request: Request, response: Response):
    """Registra una venta y descuenta el stock en una sola transacción. Guarda el coste del día de la venta."""
    u=user_from(request)
    ids=[i.product_id for i in d.items]
    if len(set(ids))!=len(ids): raise HTTPException(422,"No repitas el mismo producto; ajusta la cantidad en una sola línea")
    sale_date=d.sale_date or local_today()
    if sale_date>local_today(): raise HTTPException(422,"La fecha de la venta no puede estar en el futuro")
    with psycopg.connect(DB) as c:
        if d.client_ref:
            prev=c.execute("SELECT id FROM sales WHERE client_ref=%s",(d.client_ref,)).fetchone()
            if prev:
                response.status_code=200
                r=c.execute(f"SELECT {SALE_COLUMNS} {SALE_FROM} WHERE s.id=%s",(prev[0],)).fetchone()
                return {**_sale_dict(r,_sale_items(c,[prev[0]]).get(prev[0],[])),"duplicate":True}
        # Bloquea los productos siempre en el mismo orden para evitar bloqueos cruzados entre ventas simultáneas.
        prods={}
        for pid in sorted(ids):
            p=c.execute("SELECT id,name,unit,quantity,unit_cost,item_type,is_active FROM inventory_products WHERE id=%s FOR UPDATE",(pid,)).fetchone()
            if not p or not p[6]: raise HTTPException(404,f"El producto {pid} no existe o está inactivo")
            if p[5]!='finished_product': raise HTTPException(422,f"{p[1]} no es un producto terminado; solo se venden productos terminados")
            prods[pid]=p
        subtotal=Decimal(0); cost=Decimal(0); lines=[]
        for it in d.items:
            p=prods[it.product_id]
            require_integer_quantity(it.quantity,p[2],f"La cantidad de {p[1]}")
            if p[3]<it.quantity: raise HTTPException(409,f"Stock insuficiente de {p[1]}: disponible {p[3]} {p[2]}, pedido {it.quantity} {p[2]}")
            line=(it.quantity*it.unit_price).quantize(Decimal('0.01'))
            lcost=(it.quantity*p[4]).quantize(Decimal('0.01'))
            subtotal+=line; cost+=lcost
            lines.append((it.product_id,it.quantity,it.unit_price,line,p[4],lcost))
        if d.discount>subtotal: raise HTTPException(422,"El descuento no puede ser mayor que el valor de los productos")
        total=(subtotal-d.discount+d.shipping).quantize(Decimal('0.01'))
        try:
            sid=c.execute("""INSERT INTO sales(sale_date,channel,payment_method,customer_name,customer_contact,subtotal,discount,shipping,total,cost_total,note,client_ref,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (sale_date,d.channel,d.payment_method,d.customer_name.strip(),d.customer_contact.strip(),subtotal,d.discount,d.shipping,total,cost,d.note.strip(),d.client_ref,u['id'])).fetchone()[0]
        except UniqueViolation:
            raise HTTPException(409,"Esta venta ya se estaba registrando. Actualiza la lista para verla.")
        for pid,q,price,line,ucost,lcost in lines:
            c.execute("INSERT INTO sale_items(sale_id,product_id,quantity,unit_price,line_total,unit_cost,line_cost) VALUES(%s,%s,%s,%s,%s,%s,%s)",(sid,pid,q,price,line,ucost,lcost))
            newq=prods[pid][3]-q
            c.execute("UPDATE inventory_products SET quantity=%s,updated_at=NOW() WHERE id=%s",(newq,pid))
            c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after,sale_id)
                VALUES(%s,%s,'sale',%s,%s,%s,%s,%s,%s)""",(pid,u['id'],-q,newq,f"Venta #{sid} · {SALE_CHANNELS[d.channel]}",ucost,ucost,sid))
        r=c.execute(f"SELECT {SALE_COLUMNS} {SALE_FROM} WHERE s.id=%s",(sid,)).fetchone()
        return _sale_dict(r,_sale_items(c,[sid]).get(sid,[]))

@app.post("/sales/{sale_id}/void")
def void_sale(sale_id: int, d: SaleVoid, request: Request):
    """Anula una venta: devuelve las piezas al inventario y conserva el registro."""
    u=require_admin(request)
    with psycopg.connect(DB) as c:
        s=c.execute("SELECT id,status FROM sales WHERE id=%s FOR UPDATE",(sale_id,)).fetchone()
        if not s: raise HTTPException(404,"Venta no encontrada")
        if s[1]!='completed': raise HTTPException(409,"Esta venta ya fue anulada")
        items=c.execute("SELECT product_id,quantity,unit_cost FROM sale_items WHERE sale_id=%s ORDER BY product_id",(sale_id,)).fetchall()
        for pid,q,ucost in items:
            p=c.execute("SELECT quantity,unit_cost FROM inventory_products WHERE id=%s FOR UPDATE",(pid,)).fetchone()
            newq=p[0]+q
            # La pieza vuelve con su coste histórico; el promedio del producto se recalcula de forma ponderada.
            newcost=(((p[0]*p[1])+(q*ucost))/newq).quantize(Decimal('0.01'))
            c.execute("UPDATE inventory_products SET quantity=%s,unit_cost=%s,updated_at=NOW() WHERE id=%s",(newq,newcost,pid))
            c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after,sale_id)
                VALUES(%s,%s,'sale_void',%s,%s,%s,%s,%s,%s)""",(pid,u['id'],q,newq,f"Anulación de venta #{sale_id}: {d.reason.strip()}",p[1],newcost,sale_id))
        c.execute("UPDATE sales SET status='voided',voided_by=%s,voided_at=NOW(),void_reason=%s WHERE id=%s",(u['id'],d.reason.strip(),sale_id))
    return {"id":sale_id,"status":"voided","message":f"Venta #{sale_id} anulada. Las piezas volvieron al inventario."}

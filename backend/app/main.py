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
    user_from(request); name=d.name.strip()
    if not name: raise HTTPException(422,"El nombre de la categoría es obligatorio")
    try:
        with psycopg.connect(DB) as c:
            r=c.execute("INSERT INTO inventory_categories(name) VALUES(%s) RETURNING id,name",(name,)).fetchone()
        return {"id":r[0],"name":r[1]}
    except UniqueViolation: raise HTTPException(409,"Ya existe una categoría con ese nombre")

@app.patch("/categories/{category_id}")
def rename_category(category_id:int,d:CategoryUpdate,request:Request):
    user_from(request); name=d.name.strip()
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
    user_from(request)
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
    labels={"initial":"Existencia inicial","receipt":"Entrada valorizada","add":"Entrada manual","remove":"Salida manual","production_consumption":"Consumo de producción","production_output":"Producto terminado de producción"}
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
    u=user_from(request); p=_month_date(period)
    with psycopg.connect(DB) as c:
        r=c.execute("""INSERT INTO production_monthly_overheads(period,amount,planned_units,note,updated_by,updated_at)
            VALUES(%s,%s,%s,%s,%s,NOW()) ON CONFLICT(period) DO UPDATE SET amount=EXCLUDED.amount,
            planned_units=EXCLUDED.planned_units,note=EXCLUDED.note,updated_by=EXCLUDED.updated_by,updated_at=NOW()
            RETURNING period,amount,planned_units,note""",(p,d.amount,d.planned_units,d.note.strip(),u['id'])).fetchone()
    return {"period":r[0].strftime('%Y-%m'),"amount":r[1],"planned_units":r[2],"note":r[3],"configured":True,"cost_per_unit":(r[1]/r[2]).quantize(Decimal('0.01'))}

@app.get("/production/orders")
def production_orders(request: Request, status: str = Query("all",pattern="^(all|planned|in_progress|completed|cancelled)$")):
    user_from(request)
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT o.id,o.recipe_id,r.name,o.finished_product_id,p.sku,p.name,o.planned_quantity,p.unit,
            o.destination_mode,o.status,o.monthly_period,o.material_cost,o.labor_hours,o.hourly_rate,o.labor_cost,
            o.batch_overhead,o.unit_overhead_total,o.monthly_overhead_total,o.total_cost,o.unit_cost,o.note,o.created_at,o.started_at,o.completed_at
            FROM production_orders o JOIN production_recipes r ON r.id=o.recipe_id
            JOIN inventory_products p ON p.id=o.finished_product_id
            WHERE (%s='all' OR o.status=%s) ORDER BY o.created_at DESC,o.id DESC LIMIT 500""",(status,status)).fetchall()
    return {"items":[{"id":r[0],"recipe_id":r[1],"recipe_name":r[2],"product_id":r[3],"sku":r[4],"product_name":r[5],
        "quantity":r[6],"unit":r[7],"destination_mode":r[8],"status":r[9],"monthly_period":r[10].strftime('%Y-%m'),
        "material_cost":r[11],"labor_hours":r[12],"hourly_rate":r[13],"labor_cost":r[14],"batch_overhead":r[15],
        "unit_overhead_total":r[16],"monthly_overhead_total":r[17],"total_cost":r[18],"unit_cost":r[19],"note":r[20],
        "created_at":r[21],"started_at":r[22],"completed_at":r[23]} for r in rows]}

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
def complete_production_order(order_id: int, request: Request):
    u=user_from(request)
    with psycopg.connect(DB) as c:
        o=c.execute("""SELECT id,finished_product_id,planned_quantity,destination_mode,status,unit_cost,total_cost
            FROM production_orders WHERE id=%s FOR UPDATE""",(order_id,)).fetchone()
        if not o: raise HTTPException(404,"Orden de producción no encontrada")
        if o[4] not in ('planned','in_progress'): raise HTTPException(409,"La orden ya fue cerrada o cancelada")
        mats=c.execute("""SELECT om.product_id,om.required_quantity,om.unit_cost_snapshot,p.quantity,p.unit,p.name
            FROM production_order_materials om JOIN inventory_products p ON p.id=om.product_id
            WHERE om.order_id=%s ORDER BY om.product_id FOR UPDATE OF p""",(order_id,)).fetchall()
        for m in mats:
            if m[3] < m[1]: raise HTTPException(409,f"Stock insuficiente de {m[5]}: disponible {m[3]} {m[4]}, necesario {m[1]} {m[4]}")
            require_integer_quantity(m[1],m[4],f"El consumo de {m[5]}")
        out=c.execute("SELECT quantity,unit_cost,unit FROM inventory_products WHERE id=%s AND is_active FOR UPDATE",(o[1],)).fetchone()
        if not out: raise HTTPException(404,"El producto terminado ya no está activo")
        require_integer_quantity(o[2],out[2],"La cantidad producida")
        for m in mats:
            newq=m[3]-m[1]
            c.execute("UPDATE inventory_products SET quantity=%s,updated_at=NOW() WHERE id=%s",(newq,m[0]))
            c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
                VALUES(%s,%s,'production_consumption',%s,%s,%s,%s,%s)""",(m[0],u['id'],-m[1],newq,f"Consumo de producción · Orden #{order_id}",m[2],m[2]))
            c.execute("UPDATE production_order_materials SET consumed_quantity=%s WHERE order_id=%s AND product_id=%s",(m[1],order_id,m[0]))
        if o[3] == 'stock':
            oldq,oldcost=out[0],out[1]; newq=oldq+o[2]
            newcost=o[6] if newq==0 else ((oldq*oldcost)+(o[2]*o[5]))/newq
            newcost=newcost.quantize(Decimal('0.01'))
            c.execute("UPDATE inventory_products SET quantity=%s,unit_cost=%s,updated_at=NOW() WHERE id=%s",(newq,newcost,o[1]))
            c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
                VALUES(%s,%s,'production_output',%s,%s,%s,%s,%s)""",(o[1],u['id'],o[2],newq,f"Producción terminada · Orden #{order_id}",oldcost,newcost))
        c.execute("UPDATE production_orders SET status='completed',completed_at=NOW() WHERE id=%s",(order_id,))
    return {"id":order_id,"status":"completed","destination_mode":o[3],"quantity":o[2],"unit_cost":o[5],"total_cost":o[6],
        "message":"Producción completada. Materiales descontados y producto terminado añadido al inventario." if o[3]=='stock' else "Producción completada para pedido. Materiales descontados; producto terminado no añadido al stock disponible."}

@app.post("/production/orders/{order_id}/cancel")
def cancel_production_order(order_id: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT status FROM production_orders WHERE id=%s FOR UPDATE",(order_id,)).fetchone()
        if not r: raise HTTPException(404,"Orden de producción no encontrada")
        if r[0] not in ('planned','in_progress'): raise HTTPException(409,"Solo se pueden cancelar órdenes abiertas")
        c.execute("UPDATE production_orders SET status='cancelled' WHERE id=%s",(order_id,))
    return {"id":order_id,"status":"cancelled"}

import os
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone,date
from decimal import Decimal
from typing import Optional, Literal, List
import jwt, psycopg
from fastapi import FastAPI,File,Form,HTTPException,Query,Request,Response,UploadFile
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
        # Talla pública de cada forro y fotos (cerrado / abierto) guardadas en la base, que ya tiene volumen y respaldo.
        c.execute("ALTER TABLE inventory_products ADD COLUMN IF NOT EXISTS size_code VARCHAR(4)")
        c.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='inventory_products_size_valid') THEN
                ALTER TABLE inventory_products ADD CONSTRAINT inventory_products_size_valid CHECK (size_code IS NULL OR size_code IN ('XS','S','M','L','XL'));
            END IF;
        END $$""")
        c.execute("""CREATE TABLE IF NOT EXISTS product_photos(
            product_id BIGINT NOT NULL REFERENCES inventory_products(id),
            kind VARCHAR(10) NOT NULL CHECK(kind IN ('closed','open')),
            content_type VARCHAR(20) NOT NULL, data BYTEA NOT NULL,
            updated_by BIGINT REFERENCES app_users(id), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(product_id,kind))""")
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
        # Inversiones: equipos y mejoras que se deprecian en línea recta desde el mes de compra.
        c.execute("""CREATE TABLE IF NOT EXISTS investments(
            id BIGSERIAL PRIMARY KEY, name VARCHAR(160) NOT NULL,
            category VARCHAR(20) NOT NULL DEFAULT 'equipment' CHECK(category IN ('equipment','tools','workshop','other')),
            purchase_date DATE NOT NULL, amount NUMERIC(14,2) NOT NULL CHECK(amount>0),
            salvage_value NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK(salvage_value>=0),
            useful_life_months INTEGER NOT NULL CHECK(useful_life_months BETWEEN 1 AND 600),
            note TEXT NOT NULL DEFAULT '', retired_date DATE, retire_reason TEXT NOT NULL DEFAULT '',
            created_by BIGINT REFERENCES app_users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by BIGINT REFERENCES app_users(id), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK(salvage_value<amount), CHECK(retired_date IS NULL OR retired_date>=purchase_date))""")
        # Gastos del negocio que no son de fabricar (publicidad, transporte, comisiones…). Se anulan, nunca se borran.
        c.execute("""CREATE TABLE IF NOT EXISTS operating_expenses(
            id BIGSERIAL PRIMARY KEY, expense_date DATE NOT NULL,
            category VARCHAR(20) NOT NULL CHECK(category IN ('packaging','shipping','marketing','fees','supplies','services','other')),
            description VARCHAR(160) NOT NULL, amount NUMERIC(14,2) NOT NULL CHECK(amount>0),
            payment_method VARCHAR(20) NOT NULL DEFAULT 'cash' CHECK(payment_method IN ('cash','transfer')),
            note TEXT NOT NULL DEFAULT '', status VARCHAR(10) NOT NULL DEFAULT 'active' CHECK(status IN ('active','voided')),
            void_reason TEXT NOT NULL DEFAULT '', voided_by BIGINT REFERENCES app_users(id), voided_at TIMESTAMPTZ,
            client_ref VARCHAR(64) UNIQUE,
            created_by BIGINT REFERENCES app_users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("CREATE INDEX IF NOT EXISTS operating_expenses_date_idx ON operating_expenses(expense_date)")
        # Compras para el inventario registradas en Gastos: crean la entrada valorizada y quedan enlazadas a ella.
        for col in ("inventory_product_id BIGINT REFERENCES inventory_products(id)","inventory_quantity NUMERIC(14,3)",
                    "inventory_movement_id BIGINT REFERENCES inventory_movements(id)","inventory_void_movement_id BIGINT REFERENCES inventory_movements(id)"):
            c.execute(f"ALTER TABLE operating_expenses ADD COLUMN IF NOT EXISTS {col}")
        c.execute("ALTER TABLE operating_expenses DROP CONSTRAINT IF EXISTS operating_expenses_category_check")
        c.execute("""ALTER TABLE operating_expenses ADD CONSTRAINT operating_expenses_category_check
            CHECK(category IN ('inventory','packaging','shipping','marketing','fees','supplies','services','other'))""")
        # Comprobantes: una imagen (o PDF) por venta o gasto, guardada en la base como las fotos de las unidades.
        c.execute("""CREATE TABLE IF NOT EXISTS receipts(
            id BIGSERIAL PRIMARY KEY, owner_type VARCHAR(10) NOT NULL,
            owner_id BIGINT NOT NULL, content_type VARCHAR(40) NOT NULL, data BYTEA NOT NULL,
            uploaded_by BIGINT REFERENCES app_users(id), uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(owner_type,owner_id))""")
        # También las compras que entran al inventario (entradas). Se redefine la regla sin tocar las filas.
        c.execute("ALTER TABLE receipts DROP CONSTRAINT IF EXISTS receipts_owner_type_check")
        c.execute("ALTER TABLE receipts ADD CONSTRAINT receipts_owner_type_check CHECK(owner_type IN ('sale','expense','movement'))")
        # Unidades: cada pieza única de una referencia terminada, con sus propias fotos.
        c.execute("""CREATE TABLE IF NOT EXISTS product_units(
            id BIGSERIAL PRIMARY KEY, product_id BIGINT NOT NULL REFERENCES inventory_products(id),
            code VARCHAR(100) UNIQUE NOT NULL, name VARCHAR(120) NOT NULL DEFAULT '',
            status VARCHAR(12) NOT NULL DEFAULT 'available' CHECK(status IN ('available','sold','removed')),
            sale_id BIGINT REFERENCES sales(id), removed_reason TEXT NOT NULL DEFAULT '',
            created_by BIGINT REFERENCES app_users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("CREATE INDEX IF NOT EXISTS product_units_product_idx ON product_units(product_id,status)")
        # Reservas: una pieza disponible puede apartarse para un cliente hasta una fecha; al vencer vuelve sola a la tienda.
        for col in ("reserved_until TIMESTAMPTZ","reserved_for VARCHAR(160) NOT NULL DEFAULT ''","reserved_contact VARCHAR(160) NOT NULL DEFAULT ''",
                    "reserved_note TEXT NOT NULL DEFAULT ''","reserved_by BIGINT REFERENCES app_users(id)","reserved_at TIMESTAMPTZ"):
            c.execute(f"ALTER TABLE product_units ADD COLUMN IF NOT EXISTS {col}")
        c.execute("""CREATE TABLE IF NOT EXISTS unit_photos(
            unit_id BIGINT NOT NULL REFERENCES product_units(id) ON DELETE CASCADE,
            kind VARCHAR(10) NOT NULL CHECK(kind IN ('closed','open')),
            content_type VARCHAR(20) NOT NULL, data BYTEA NOT NULL,
            updated_by BIGINT REFERENCES app_users(id), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(unit_id,kind))""")
        # Las fotos que se subieron a una referencia pasan a una primera unidad de esa referencia (una sola vez).
        for pid,sku,qty in c.execute("""SELECT p.id,p.sku,p.quantity FROM inventory_products p
                WHERE EXISTS(SELECT 1 FROM product_photos ph WHERE ph.product_id=p.id)""").fetchall():
            if qty>=1 and not c.execute("SELECT 1 FROM product_units WHERE product_id=%s AND status='available'",(pid,)).fetchone():
                uid=c.execute("INSERT INTO product_units(product_id,code) VALUES(%s,%s) ON CONFLICT(code) DO NOTHING RETURNING id",(pid,f"{sku}-001")).fetchone()
                if uid:
                    c.execute("""INSERT INTO unit_photos(unit_id,kind,content_type,data,updated_by,updated_at)
                        SELECT %s,kind,content_type,data,updated_by,updated_at FROM product_photos WHERE product_id=%s""",(uid[0],pid))
                    c.execute("DELETE FROM product_photos WHERE product_id=%s",(pid,))
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
SIZE_CODES=("XS","S","M","L","XL")
PHOTO_KINDS={"closed":"Cerrado","open":"Abierto"}
PCOLS="""id,sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes,updated_at,size_code,
    (SELECT COUNT(*) FROM product_units pu WHERE pu.product_id=inventory_products.id AND pu.status='available'),
    (SELECT COUNT(*) FROM product_units pu WHERE pu.product_id=inventory_products.id AND pu.status='available'
        AND (pu.reserved_until IS NULL OR pu.reserved_until<=NOW())
        AND EXISTS(SELECT 1 FROM unit_photos up WHERE up.unit_id=pu.id AND up.kind='closed')),
    (SELECT pu.id::text||':'||floor(extract(epoch FROM up.updated_at))::bigint FROM product_units pu JOIN unit_photos up ON up.unit_id=pu.id
        WHERE pu.product_id=inventory_products.id AND pu.status='available' AND up.kind='closed' ORDER BY pu.id LIMIT 1)"""
def unit_photo_urls(uid, versions):
    return {k:f"/api/public/unit-photos/{uid}/{k}?v={v}" for k,v in (versions or {}).items()}
def pack(r):
    thumb=None
    if r[15]:
        uid,ver=r[15].split(':'); thumb=f"/api/public/unit-photos/{uid}/closed?v={ver}"
    return {"id":r[0],"sku":r[1],"name":r[2],"item_type":r[3],"category":r[4],"unit":r[5],"quantity":r[6],"min_stock":r[7],"unit_cost":r[8],"sale_price":r[9],"notes":r[10],"updated_at":r[11],
            "size_code":r[12],"units_available":r[13],"units_with_photo":r[14],"thumb":thumb}

DISCRETE_UNITS={"unidad","rollo","paquete"}
def is_discrete_unit(unit:str)->bool:
    return (unit or "").strip().lower() in DISCRETE_UNITS
def fmt_qty(v: Decimal) -> str:
    """Cantidad legible en mensajes: sin ceros de más y con coma decimal."""
    t=format(Decimal(v).normalize(),'f')
    return t.replace('.',',')

def require_integer_quantity(value:Decimal,unit:str,field_name:str="La cantidad"):
    if is_discrete_unit(unit) and value != value.to_integral_value():
        raise HTTPException(422,f"{field_name} debe ser un número entero cuando la unidad es {unit}.")
class Product(BaseModel):
    sku:str=Field(min_length=1,max_length=80); name:str=Field(min_length=1,max_length=180)
    item_type:str=Field(default="raw_material",max_length=30)
    category:str=Field(default="Sin categoría",max_length=100); unit:str=Field(default="unidad",max_length=30)
    quantity:Decimal=Field(default=Decimal(0),ge=0); min_stock:Decimal=Field(default=Decimal(0),ge=0)
    unit_cost:Decimal=Field(default=Decimal(0),ge=0); sale_price:Optional[Decimal]=Field(default=None,ge=0)
    notes:str=Field(default="",max_length=4000); size_code:Optional[Literal["XS","S","M","L","XL"]]=None
class ProductUpdate(BaseModel):
    sku:str=Field(min_length=1,max_length=80); name:str=Field(min_length=1,max_length=180)
    item_type:str=Field(default="raw_material",max_length=30)
    category:str=Field(default="Sin categoría",max_length=100); unit:str=Field(default="unidad",max_length=30)
    min_stock:Decimal=Field(ge=0); unit_cost:Decimal=Field(ge=0); sale_price:Optional[Decimal]=Field(default=None,ge=0)
    notes:str=Field(default="",max_length=4000); size_code:Optional[Literal["XS","S","M","L","XL"]]=None
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

UNIT_PHOTOS_JSON="""(SELECT COALESCE(json_object_agg(up.kind,floor(extract(epoch FROM up.updated_at))::bigint),'{}'::json)
    FROM unit_photos up WHERE up.unit_id=pu.id)"""

@app.get("/public/catalog")
def public_catalog():
    """Catálogo de la tienda: referencias terminadas y sus piezas únicas disponibles con foto.
    Nunca devuelve costes, notas internas ni cantidades exactas."""
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT id,sku,name,unit,sale_price,quantity>0,size_code FROM inventory_products
            WHERE is_active=TRUE AND item_type='finished_product' AND sale_price IS NOT NULL ORDER BY name,id""").fetchall()
        units=c.execute(f"""SELECT pu.product_id,pu.id,pu.code,pu.name,{UNIT_PHOTOS_JSON} FROM product_units pu
            JOIN inventory_products p ON p.id=pu.product_id
            WHERE pu.status='available' AND p.is_active AND p.item_type='finished_product' AND p.quantity>0
              AND (pu.reserved_until IS NULL OR pu.reserved_until<=NOW())
              AND EXISTS(SELECT 1 FROM unit_photos up WHERE up.unit_id=pu.id AND up.kind='closed')
            ORDER BY pu.id""").fetchall()
    styles={}
    for pid,uid,code,name,ph in units:
        styles.setdefault(pid,[]).append({"id":uid,"ref":code,"name":name,"photos":unit_photo_urls(uid,ph)})
    return {"items":[{"id":r[0],"ref":r[1],"name":r[2],"unit":r[3],"sale_price":r[4],"available":r[5],"size":r[6],
                      "styles":styles.get(r[0],[])} for r in rows]}

MAX_PHOTO_BYTES=1_500_000
def _image_type(data: bytes) -> Optional[str]:
    if data[:3]==b"\xff\xd8\xff": return "image/jpeg"
    if data[:8]==b"\x89PNG\r\n\x1a\n": return "image/png"
    if data[:4]==b"RIFF" and data[8:12]==b"WEBP": return "image/webp"
    return None

# Comprobantes de ventas y gastos: privados, solo para cuentas con sesión.
MAX_RECEIPT_BYTES=1_500_000
def _receipt_type(data: bytes) -> Optional[str]:
    return _image_type(data) or ("application/pdf" if data[:5]==b"%PDF-" else None)

def _receipt_info(prefix: str, oid: int, packed: Optional[str]):
    if not packed: return None
    ver,ctype,who=packed.split('|',2)
    return {"url":f"/api/{prefix}/{oid}/receipt?v={ver}","content_type":ctype,"is_pdf":ctype=="application/pdf",
            "uploaded_by":who or None,"uploaded_at":datetime.fromtimestamp(int(ver)/1000,timezone.utc).isoformat()}

RECEIPT_OWNERS={"sale":("sales","Venta no encontrada"),"expense":("operating_expenses","Gasto no encontrado")}
# Movimientos de inventario que son compras o entradas: solo ellos llevan comprobante.
RECEIPT_MOVEMENT_TYPES=("initial","receipt","add")

def _receipt_owner(c, kind: str, oid: int, lock: bool=False):
    if kind=="movement":
        r=c.execute("SELECT movement_type FROM inventory_movements WHERE id=%s"+(" FOR UPDATE" if lock else ""),(oid,)).fetchone()
        if not r: raise HTTPException(404,"Movimiento no encontrado")
        if r[0] not in RECEIPT_MOVEMENT_TYPES: raise HTTPException(422,"Solo las entradas de inventario llevan comprobante")
        if c.execute("SELECT 1 FROM operating_expenses WHERE inventory_movement_id=%s",(oid,)).fetchone():
            raise HTTPException(409,"Esta entrada viene de una compra registrada en Gastos: su comprobante se gestiona allí")
        return "active"
    table,missing=RECEIPT_OWNERS[kind]
    r=c.execute(f"SELECT status FROM {table} WHERE id=%s"+(" FOR UPDATE" if lock else ""),(oid,)).fetchone()
    if not r: raise HTTPException(404,missing)
    return r[0]

async def _save_receipt(kind: str, oid: int, request: Request, file: UploadFile):
    u=user_from(request)
    data=await file.read(MAX_RECEIPT_BYTES+1)
    if len(data)>MAX_RECEIPT_BYTES: raise HTTPException(413,"El comprobante pesa demasiado. Usa uno de menos de 1,5 MB.")
    ctype=_receipt_type(data)
    if not ctype: raise HTTPException(422,"El comprobante debe ser una imagen JPG, PNG o WebP, o un PDF")
    with psycopg.connect(DB) as c:
        if _receipt_owner(c,kind,oid,lock=True) in ("voided",): raise HTTPException(409,"No se puede cambiar el comprobante de un registro anulado")
        c.execute("""INSERT INTO receipts(owner_type,owner_id,content_type,data,uploaded_by,uploaded_at) VALUES(%s,%s,%s,%s,%s,NOW())
            ON CONFLICT(owner_type,owner_id) DO UPDATE SET content_type=EXCLUDED.content_type,data=EXCLUDED.data,
            uploaded_by=EXCLUDED.uploaded_by,uploaded_at=NOW()""",(kind,oid,ctype,data,u['id']))
    return {"message":"Comprobante guardado"}

def _get_receipt(kind: str, oid: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT content_type,data FROM receipts WHERE owner_type=%s AND owner_id=%s",(kind,oid)).fetchone()
    if not r: raise HTTPException(404,"Este registro no tiene comprobante")
    ext="pdf" if r[0]=="application/pdf" else r[0].split('/')[1].replace('jpeg','jpg')
    return Response(content=bytes(r[1]),media_type=r[0],headers={"Cache-Control":"private, max-age=86400",
        "Content-Disposition":f'inline; filename="comprobante-{kind}-{oid}.{ext}"',"X-Content-Type-Options":"nosniff"})

def _delete_receipt(kind: str, oid: int, request: Request):
    require_admin(request)
    with psycopg.connect(DB) as c:
        _receipt_owner(c,kind,oid)
        if not c.execute("DELETE FROM receipts WHERE owner_type=%s AND owner_id=%s",(kind,oid)).rowcount:
            raise HTTPException(404,"Este registro no tiene comprobante")
    return {"message":"Comprobante eliminado"}

@app.post("/sales/{sale_id}/receipt")
async def upload_sale_receipt(sale_id: int, request: Request, file: UploadFile = File(...)):
    return await _save_receipt("sale",sale_id,request,file)
@app.get("/sales/{sale_id}/receipt")
def sale_receipt(sale_id: int, request: Request):
    return _get_receipt("sale",sale_id,request)
@app.delete("/sales/{sale_id}/receipt")
def delete_sale_receipt(sale_id: int, request: Request):
    return _delete_receipt("sale",sale_id,request)
@app.post("/expenses/{exp_id}/receipt")
async def upload_expense_receipt(exp_id: int, request: Request, file: UploadFile = File(...)):
    return await _save_receipt("expense",exp_id,request,file)
@app.get("/expenses/{exp_id}/receipt")
def expense_receipt(exp_id: int, request: Request):
    return _get_receipt("expense",exp_id,request)
@app.delete("/expenses/{exp_id}/receipt")
def delete_expense_receipt(exp_id: int, request: Request):
    return _delete_receipt("expense",exp_id,request)

@app.post("/movements/{mid}/receipt")
async def upload_movement_receipt(mid: int, request: Request, file: UploadFile = File(...)):
    return await _save_receipt("movement",mid,request,file)
@app.get("/movements/{mid}/receipt")
def movement_receipt(mid: int, request: Request):
    return _get_receipt("movement",mid,request)
@app.delete("/movements/{mid}/receipt")
def delete_movement_receipt(mid: int, request: Request):
    return _delete_receipt("movement",mid,request)

MOV_RECEIPT_SQL="""COALESCE((SELECT 'movements|'||m.id||'|'||floor(extract(epoch FROM rc.uploaded_at)*1000)::bigint||'|'||rc.content_type||'|'||COALESCE(ru.username,'')
     FROM receipts rc LEFT JOIN app_users ru ON ru.id=rc.uploaded_by WHERE rc.owner_type='movement' AND rc.owner_id=m.id),
    (SELECT 'expenses|'||ex.id||'|'||floor(extract(epoch FROM rc.uploaded_at)*1000)::bigint||'|'||rc.content_type||'|'||COALESCE(ru.username,'')
     FROM operating_expenses ex JOIN receipts rc ON rc.owner_type='expense' AND rc.owner_id=ex.id LEFT JOIN app_users ru ON ru.id=rc.uploaded_by
     WHERE ex.inventory_movement_id=m.id),
    (SELECT 'expenses|'||ex.id||'|' FROM operating_expenses ex WHERE ex.inventory_movement_id=m.id))"""

def _movement_receipt(mid: int, mtype: str, packed: Optional[str]):
    """Comprobante de un movimiento. Una compra registrada en Gastos muestra el comprobante del gasto."""
    out={"receipt_allowed":mtype in RECEIPT_MOVEMENT_TYPES,"receipt":None,"expense_id":None}
    if not packed: return out
    prefix,oid,rest=packed.split('|',2)
    if prefix=="expenses":
        out["expense_id"]=int(oid); out["receipt_allowed"]=False  # se gestiona desde Gastos
    out["receipt"]=_receipt_info(prefix,int(oid),rest) if rest else None
    return out

@app.get("/public/unit-photos/{uid}/{kind}")
def public_unit_photo(uid: int, kind: Literal["closed","open"]):
    with psycopg.connect(DB) as c:
        r=c.execute("""SELECT up.content_type,up.data FROM unit_photos up JOIN product_units pu ON pu.id=up.unit_id
            JOIN inventory_products p ON p.id=pu.product_id
            WHERE up.unit_id=%s AND up.kind=%s AND p.is_active AND p.item_type='finished_product'""",(uid,kind)).fetchone()
    if not r: raise HTTPException(404,"Foto no encontrada")
    return Response(content=bytes(r[1]),media_type=r[0],headers={"Cache-Control":"public, max-age=86400"})

# ------------------------------- UNIDADES -------------------------------
UNIT_STATUS={"available":"Disponible","sold":"Vendida","removed":"Dada de baja","reserved":"Reservada"}
class UnitCreate(BaseModel):
    name: str = Field(default="", max_length=120)
class UnitUpdate(BaseModel):
    name: str = Field(default="", max_length=120)
class UnitRemove(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
class UnitReserve(BaseModel):
    customer: str = Field(min_length=2, max_length=160)
    contact: str = Field(default="", max_length=160)
    hours: int = Field(default=48, ge=1, le=336)
    note: str = Field(default="", max_length=500)

def _units_fit(c, pid, new_quantity, available_after=None):
    """Nunca puede haber más unidades disponibles identificadas que existencias."""
    n=available_after if available_after is not None else c.execute(
        "SELECT COUNT(*) FROM product_units WHERE product_id=%s AND status='available'",(pid,)).fetchone()[0]
    if n>new_quantity:
        raise HTTPException(409,f"Quedarían {n} unidad(es) con ficha disponible pero solo {new_quantity} en existencias. Indica qué unidades salen (véndelas, dalas de baja o elimina sus fichas) antes de bajar el inventario.")

def _unit_rows(c, pid=None, uid=None):
    where="pu.product_id=%s" if pid is not None else "pu.id=%s"
    return c.execute(f"""SELECT pu.id,pu.product_id,pu.code,pu.name,pu.status,pu.sale_id,pu.removed_reason,pu.created_at,cu.username,{UNIT_PHOTOS_JSON},
        pu.reserved_until,pu.reserved_until>NOW(),pu.reserved_for,pu.reserved_contact,pu.reserved_note,ru.username,pu.reserved_at
        FROM product_units pu LEFT JOIN app_users cu ON cu.id=pu.created_by LEFT JOIN app_users ru ON ru.id=pu.reserved_by WHERE {where}
        ORDER BY (pu.status='available') DESC, pu.id DESC""",(pid if pid is not None else uid,)).fetchall()

def _unit_dict(r):
    reserved=r[4]=='available' and bool(r[11])
    shown='reserved' if reserved else r[4]
    return {"id":r[0],"product_id":r[1],"code":r[2],"name":r[3],"status":r[4],"shown_status":shown,"status_label":UNIT_STATUS.get(shown,shown),
            "sale_id":r[5],"removed_reason":r[6],"created_at":r[7],"created_by":r[8],"photos":unit_photo_urls(r[0],r[9]),
            "reservation":({"until":r[10],"customer":r[12],"contact":r[13],"note":r[14],"by":r[15],"at":r[16]} if reserved else None)}

def _units_payload(c, pid):
    p=c.execute(f"SELECT {PCOLS} FROM inventory_products WHERE id=%s",(pid,)).fetchone()
    return {"product":pack(p),"units":[_unit_dict(r) for r in _unit_rows(c,pid=pid)]}

def _lock_finished(c, pid):
    p=c.execute("SELECT id,sku,quantity,unit,item_type,is_active FROM inventory_products WHERE id=%s FOR UPDATE",(pid,)).fetchone()
    if not p or not p[5]: raise HTTPException(404,"Producto no encontrado")
    if p[4]!='finished_product': raise HTTPException(422,"Solo los productos terminados tienen unidades con fotos")
    return p

@app.get("/inventory/{pid}/units")
def list_units(pid: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        if not c.execute("SELECT 1 FROM inventory_products WHERE id=%s AND is_active",(pid,)).fetchone(): raise HTTPException(404,"Producto no encontrado")
        return _units_payload(c,pid)

@app.post("/inventory/{pid}/units", status_code=201)
def create_unit(pid: int, d: UnitCreate, request: Request):
    """Crea la ficha de una pieza que ya está en existencias (por producción o entrada)."""
    u=user_from(request)
    with psycopg.connect(DB) as c:
        p=_lock_finished(c,pid)
        if not is_discrete_unit(p[3]): raise HTTPException(422,"Las unidades individuales solo aplican a productos que se cuentan por unidad")
        avail=c.execute("SELECT COUNT(*) FROM product_units WHERE product_id=%s AND status='available'",(pid,)).fetchone()[0]
        if avail+1>p[2]:
            raise HTTPException(409,f"Todas las existencias ({p[2]}) ya tienen ficha. Registra primero la producción o la entrada de la nueva pieza.")
        n=c.execute("SELECT COUNT(*) FROM product_units WHERE product_id=%s",(pid,)).fetchone()[0]+1
        while c.execute("SELECT 1 FROM product_units WHERE code=%s",(f"{p[1]}-{n:03d}",)).fetchone(): n+=1
        c.execute("INSERT INTO product_units(product_id,code,name,created_by) VALUES(%s,%s,%s,%s)",(pid,f"{p[1]}-{n:03d}",d.name.strip(),u['id']))
        return {**_units_payload(c,pid),"message":f"Unidad {p[1]}-{n:03d} creada. Súbele sus dos fotos."}

def _lock_unit(c, uid):
    r=c.execute("SELECT id,product_id,code,status FROM product_units WHERE id=%s FOR UPDATE",(uid,)).fetchone()
    if not r: raise HTTPException(404,"Unidad no encontrada")
    return r

@app.patch("/units/{uid}")
def update_unit(uid: int, d: UnitUpdate, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=_lock_unit(c,uid)
        c.execute("UPDATE product_units SET name=%s,updated_at=NOW() WHERE id=%s",(d.name.strip(),uid))
        return {**_units_payload(c,r[1]),"message":"Unidad actualizada"}

@app.delete("/units/{uid}")
def delete_unit(uid: int, request: Request):
    """Elimina una ficha creada por error. La pieza sigue en existencias, solo sin identificar."""
    user_from(request)
    with psycopg.connect(DB) as c:
        r=_lock_unit(c,uid)
        if r[3]!='available': raise HTTPException(409,"Solo se pueden eliminar fichas de unidades disponibles; las vendidas o dadas de baja se conservan")
        c.execute("DELETE FROM product_units WHERE id=%s",(uid,))
        return {**_units_payload(c,r[1]),"message":f"Ficha {r[2]} eliminada. Las existencias no cambian."}

@app.post("/units/{uid}/remove")
def remove_unit(uid: int, d: UnitRemove, request: Request):
    """Saca la pieza del inventario (dañada, regalada, perdida) con un movimiento trazable."""
    u=user_from(request)
    with psycopg.connect(DB) as c:
        r=_lock_unit(c,uid)
        if r[3]!='available': raise HTTPException(409,"Esta unidad ya no está disponible")
        p=c.execute("SELECT quantity,unit_cost FROM inventory_products WHERE id=%s FOR UPDATE",(r[1],)).fetchone()
        if p[0]<1: raise HTTPException(409,"No hay existencias para descontar")
        newq=p[0]-1
        c.execute("UPDATE inventory_products SET quantity=%s,updated_at=NOW() WHERE id=%s",(newq,r[1]))
        c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
            VALUES(%s,%s,'remove',-1,%s,%s,%s,%s)""",(r[1],u['id'],newq,f"Unidad {r[2]} dada de baja: {d.reason.strip()}",p[1],p[1]))
        c.execute("UPDATE product_units SET status='removed',removed_reason=%s,updated_at=NOW() WHERE id=%s",(d.reason.strip(),uid))
        return {**_units_payload(c,r[1]),"message":f"Unidad {r[2]} dada de baja y descontada del inventario"}

@app.post("/units/{uid}/reserve")
def reserve_unit(uid: int, d: UnitReserve, request: Request):
    """Aparta una pieza para un cliente. Mientras la reserva esté vigente no se muestra en la tienda.
    Volver a reservarla actualiza el cliente o el plazo."""
    u=user_from(request)
    with psycopg.connect(DB) as c:
        r=_lock_unit(c,uid)
        if r[3]!='available': raise HTTPException(409,"Solo se pueden reservar unidades disponibles")
        c.execute("""UPDATE product_units SET reserved_until=NOW()+make_interval(hours=>%s),reserved_for=%s,reserved_contact=%s,
            reserved_note=%s,reserved_by=%s,reserved_at=NOW(),updated_at=NOW() WHERE id=%s""",
            (d.hours,d.customer.strip(),d.contact.strip(),d.note.strip(),u['id'],uid))
        return {**_units_payload(c,r[1]),"message":f"{r[2]} reservada para {d.customer.strip()} por {d.hours} h. No se muestra en la tienda mientras tanto."}

@app.delete("/units/{uid}/reserve")
def release_unit(uid: int, request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=_lock_unit(c,uid)
        if not c.execute("SELECT 1 FROM product_units WHERE id=%s AND reserved_until>NOW()",(uid,)).fetchone():
            raise HTTPException(409,"Esta unidad no tiene una reserva vigente")
        c.execute("""UPDATE product_units SET reserved_until=NULL,reserved_for='',reserved_contact='',reserved_note='',
            reserved_by=NULL,reserved_at=NULL,updated_at=NOW() WHERE id=%s""",(uid,))
        return {**_units_payload(c,r[1]),"message":f"Reserva de {r[2]} liberada. Vuelve a verse en la tienda."}

@app.post("/units/{uid}/photos/{kind}")
async def upload_unit_photo(uid: int, kind: Literal["closed","open"], request: Request, file: UploadFile = File(...)):
    u=user_from(request)
    data=await file.read(MAX_PHOTO_BYTES+1)
    if len(data)>MAX_PHOTO_BYTES: raise HTTPException(413,"La foto pesa demasiado. Usa una de menos de 1,5 MB.")
    ctype=_image_type(data)
    if not ctype: raise HTTPException(422,"El archivo no es una imagen JPG, PNG o WebP")
    with psycopg.connect(DB) as c:
        r=_lock_unit(c,uid)
        c.execute("""INSERT INTO unit_photos(unit_id,kind,content_type,data,updated_by,updated_at) VALUES(%s,%s,%s,%s,%s,NOW())
            ON CONFLICT(unit_id,kind) DO UPDATE SET content_type=EXCLUDED.content_type,data=EXCLUDED.data,updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
            (uid,kind,ctype,data,u['id']))
        return {**_units_payload(c,r[1]),"message":f"Foto «{PHOTO_KINDS[kind]}» de {r[2]} guardada"}

@app.get("/admin/unit-photos")
def unit_photo_sizes(request: Request):
    """Tamaño de cada foto de unidad, para optimizarlas desde el panel."""
    require_admin(request)
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT up.unit_id,up.kind,octet_length(up.data),pu.code,floor(extract(epoch FROM up.updated_at))::bigint
            FROM unit_photos up JOIN product_units pu ON pu.id=up.unit_id ORDER BY octet_length(up.data) DESC""").fetchall()
    return {"items":[{"unit_id":r[0],"kind":r[1],"bytes":r[2],"code":r[3],"url":f"/api/units/{r[0]}/photos/{r[1]}?v={r[4]}"} for r in rows],
            "total_bytes":sum(r[2] for r in rows)}

@app.get("/units/{uid}/photos/{kind}")
def private_unit_photo(uid: int, kind: Literal["closed","open"], request: Request):
    """Foto de cualquier unidad (también vendidas o de referencias inactivas), solo con sesión."""
    user_from(request)
    with psycopg.connect(DB) as c:
        r=c.execute("SELECT content_type,data FROM unit_photos WHERE unit_id=%s AND kind=%s",(uid,kind)).fetchone()
    if not r: raise HTTPException(404,"Foto no encontrada")
    return Response(content=bytes(r[1]),media_type=r[0],headers={"Cache-Control":"private, no-store"})

@app.delete("/units/{uid}/photos/{kind}")
def delete_unit_photo(uid: int, kind: Literal["closed","open"], request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        r=_lock_unit(c,uid)
        if not c.execute("DELETE FROM unit_photos WHERE unit_id=%s AND kind=%s",(uid,kind)).rowcount: raise HTTPException(404,"Esa foto no existe")
        return {**_units_payload(c,r[1]),"message":f"Foto «{PHOTO_KINDS[kind]}» de {r[2]} eliminada"}

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
        rows=c.execute(f"""SELECT {PCOLS} FROM inventory_products
        WHERE is_active AND (%s='' OR sku ILIKE %s OR name ILIKE %s OR category ILIKE %s)
        AND (%s='all' OR (%s='low' AND quantity<=min_stock) OR (%s='ok' AND quantity>min_stock))
        AND (%s='all' OR item_type=%s) ORDER BY name""",
        (search.strip(),term,term,term,status,status,status,item_type,item_type)).fetchall()
        totals=c.execute("""SELECT COUNT(*),COUNT(*) FILTER(WHERE quantity<=min_stock),COALESCE(SUM(quantity*unit_cost),0)
        FROM inventory_products WHERE is_active""").fetchone()
        # Artículos con existencias pero sin coste: no suman al valor del inventario.
        uncosted=c.execute("""SELECT name FROM inventory_products WHERE is_active AND quantity>0 AND unit_cost=0
            ORDER BY item_type='finished_product' DESC, name""").fetchall()
    return {"items":[pack(r) for r in rows],"summary":{"products":totals[0],"low_stock":totals[1],"inventory_value":totals[2],
            "uncosted":{"count":len(uncosted),"names":[r[0] for r in uncosted[:4]]}}}
@app.post("/inventory",status_code=201)
def create_product(d:Product,request:Request):
    u=user_from(request);sku=d.sku.strip();name=d.name.strip()
    if not sku or not name:raise HTTPException(422,"Referencia y nombre son obligatorios")
    if d.item_type not in ITEM_TYPES: raise HTTPException(422,"Tipo de artículo no válido")
    require_integer_quantity(d.quantity,d.unit,"La existencia inicial")
    require_integer_quantity(d.min_stock,d.unit,"El stock mínimo")
    sale_price = d.sale_price if d.item_type == "finished_product" else None
    size_code = d.size_code if d.item_type == "finished_product" else None
    try:
        with psycopg.connect(DB) as c:
            r=c.execute(f"""INSERT INTO inventory_products(sku,name,item_type,category,unit,quantity,min_stock,unit_cost,sale_price,notes,size_code)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {PCOLS}""",
            (sku,name,d.item_type,d.category.strip() or "Sin categoría",d.unit.strip() or "unidad",d.quantity,d.min_stock,d.unit_cost,sale_price,d.notes.strip(),size_code)).fetchone()
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
    size_code = d.size_code if d.item_type == "finished_product" else None
    try:
        with psycopg.connect(DB) as c:
            row=c.execute("SELECT id,quantity,item_type FROM inventory_products WHERE id=%s AND is_active FOR UPDATE",(pid,)).fetchone()
            if not row: raise HTTPException(404,"Producto no encontrado")
            if row[2] == 'finished_product' and d.item_type != 'finished_product':
                linked=c.execute("SELECT 1 FROM production_recipes WHERE finished_product_id=%s AND is_active LIMIT 1",(pid,)).fetchone()
                if linked:
                    raise HTTPException(409,"Este producto está asociado a una o más fichas de producción. Cambia o desactiva primero esas fichas antes de quitarle el tipo Producto terminado.")
                if c.execute("SELECT 1 FROM product_units WHERE product_id=%s AND status='available' LIMIT 1",(pid,)).fetchone():
                    raise HTTPException(409,"Este producto tiene unidades con ficha. Elimina o da de baja esas fichas antes de quitarle el tipo Producto terminado.")
            require_integer_quantity(row[1],d.unit,"La existencia actual")
            out=c.execute(f"""UPDATE inventory_products SET sku=%s,name=%s,item_type=%s,category=%s,unit=%s,
            min_stock=%s,unit_cost=%s,sale_price=%s,notes=%s,size_code=%s,updated_at=NOW() WHERE id=%s
            RETURNING {PCOLS}""",
            (sku,name,d.item_type,category,d.unit.strip() or "unidad",d.min_stock,d.unit_cost,sale_price,d.notes.strip(),size_code,pid)).fetchone()
        return pack(out)
    except UniqueViolation: raise HTTPException(409,"Ya existe un producto con esa referencia (SKU)")

def _receive_into_stock(c, pid: int, quantity: Decimal, unit_cost: Decimal, note: str, user_id: int, allow_finished: bool=True):
    """Entrada valorizada dentro de una transacción abierta: suma existencias y recalcula el coste promedio ponderado."""
    r=c.execute("SELECT id,quantity,unit_cost,unit,item_type,name FROM inventory_products WHERE id=%s AND is_active FOR UPDATE",(pid,)).fetchone()
    if not r: raise HTTPException(404,"Producto no encontrado")
    if not allow_finished and r[4]=="finished_product":
        raise HTTPException(422,"Los productos terminados entran por Producción, no como compra")
    require_integer_quantity(quantity,r[3],"La cantidad recibida")
    old_qty,old_cost=r[1],r[2]
    new_qty=old_qty+quantity
    if new_qty <= 0: raise HTTPException(422,"La cantidad resultante debe ser positiva")
    # Mantiene precisión monetaria de dos decimales.
    weighted=(((old_qty*old_cost)+(quantity*unit_cost))/new_qty).quantize(Decimal('0.01'))
    c.execute("UPDATE inventory_products SET quantity=%s,unit_cost=%s,updated_at=NOW() WHERE id=%s",(new_qty,weighted,pid))
    mid=c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
    VALUES(%s,%s,'receipt',%s,%s,%s,%s,%s) RETURNING id""",(pid,user_id,quantity,new_qty,note,old_cost,weighted)).fetchone()[0]
    return mid,old_cost,weighted,r

@app.post("/inventory/{pid}/receive")
def receive_stock(pid:int,d:Receipt,request:Request):
    """Registra una entrada valorizada y recalcula el coste promedio ponderado."""
    u=user_from(request)
    with psycopg.connect(DB) as c:
        mid,old_cost,weighted,_=_receive_into_stock(c,pid,d.quantity,d.unit_cost,d.note.strip() or "Entrada valorizada",u['id'])
        out=c.execute(f"SELECT {PCOLS} FROM inventory_products WHERE id=%s",(pid,)).fetchone()
    return {**pack(out),"movement_id":mid,"receipt":{"quantity":d.quantity,"received_unit_cost":d.unit_cost,"previous_unit_cost":old_cost,"weighted_average_unit_cost":weighted}}

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
        if delta<0: _units_fit(c,pid,new)
        c.execute("UPDATE inventory_products SET quantity=%s,updated_at=NOW() WHERE id=%s",(new,pid))
        c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note)
        VALUES(%s,%s,%s,%s,%s,%s)""",(pid,u["id"],d.direction,delta,new,d.note.strip() or ("Entrada manual" if delta>0 else "Salida manual")))
        out=c.execute(f"SELECT {PCOLS} FROM inventory_products WHERE id=%s",(pid,)).fetchone()
    return pack(out)
@app.get("/movements")
def all_movements(request:Request,search:str=Query("",max_length=120),movement_type:str=Query("all",max_length=30),product_id:Optional[int]=None):
    """Historial global de movimientos, opcionalmente filtrado por producto."""
    user_from(request)
    term=f"%{search.strip()}%"
    with psycopg.connect(DB) as c:
        rows=c.execute(f"""SELECT m.id,m.movement_type,m.quantity_delta,m.quantity_after,m.note,m.created_at,
            u.username,m.unit_cost_before,m.unit_cost_after,p.id,p.sku,p.name,p.unit,{MOV_RECEIPT_SQL}
            FROM inventory_movements m
            JOIN inventory_products p ON p.id=m.product_id
            LEFT JOIN app_users u ON u.id=m.user_id
            WHERE (%s IS NULL OR p.id=%s)
              AND (%s='' OR p.name ILIKE %s OR p.sku ILIKE %s OR m.note ILIKE %s OR COALESCE(u.username,'') ILIKE %s)
              AND (%s='all' OR m.movement_type=%s)
            ORDER BY m.created_at DESC,m.id DESC LIMIT 1000""",
            (product_id,product_id,search.strip(),term,term,term,term,movement_type,movement_type)).fetchall()
    labels={"initial":"Existencia inicial","receipt":"Entrada valorizada","add":"Entrada manual","remove":"Salida manual","production_consumption":"Consumo de producción","production_output":"Producto terminado de producción","production_reversal":"Reversión de producción","sale":"Venta","sale_void":"Venta anulada","purchase_void":"Compra anulada"}
    return {"items":[{"id":r[0],"type":r[1],"type_label":labels.get(r[1],r[1].replace('_',' ').capitalize()),
        "delta":r[2],"quantity_after":r[3],"note":r[4],"created_at":r[5],"username":r[6],
        "unit_cost_before":r[7],"unit_cost_after":r[8],"product_id":r[9],"sku":r[10],"product_name":r[11],"unit":r[12],
        **_movement_receipt(r[0],r[1],r[13])} for r in rows]}

@app.get("/inventory/{pid}/movements")
def movements(pid:int,request:Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        if not c.execute("SELECT 1 FROM inventory_products WHERE id=%s AND is_active",(pid,)).fetchone():raise HTTPException(404,"Producto no encontrado")
        rows=c.execute(f"""SELECT m.id,m.movement_type,m.quantity_delta,m.quantity_after,m.note,m.created_at,u.username,m.unit_cost_before,m.unit_cost_after,
        {MOV_RECEIPT_SQL} FROM inventory_movements m LEFT JOIN app_users u ON u.id=m.user_id WHERE m.product_id=%s ORDER BY m.created_at DESC LIMIT 100""",(pid,)).fetchall()
    return {"items":[{"id":r[0],"type":r[1],"delta":r[2],"quantity_after":r[3],"note":r[4],"created_at":r[5],"username":r[6],"unit_cost_before":r[7],"unit_cost_after":r[8],
        **_movement_receipt(r[0],r[1],r[9])} for r in rows]}


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
        dep=_depreciation_for_month(c,p)
    return _overhead_payload(period,r,dep)

@app.put("/production/overheads/{period}")
def save_monthly_overhead(period: str, d: MonthlyOverheadInput, request: Request):
    u=require_admin(request); p=_month_date(period)
    with psycopg.connect(DB) as c:
        r=c.execute("""INSERT INTO production_monthly_overheads(period,amount,planned_units,note,updated_by,updated_at)
            VALUES(%s,%s,%s,%s,%s,NOW()) ON CONFLICT(period) DO UPDATE SET amount=EXCLUDED.amount,
            planned_units=EXCLUDED.planned_units,note=EXCLUDED.note,updated_by=EXCLUDED.updated_by,updated_at=NOW()
            RETURNING period,amount,planned_units,note""",(p,d.amount,d.planned_units,d.note.strip(),u['id'])).fetchone()
        dep=_depreciation_for_month(c,p)
    return _overhead_payload(period,r,dep)

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
            # Gasto fijo del mes + depreciación de equipos, repartidos entre las unidades previstas.
            monthly_total=((overhead[0]+_depreciation_for_month(c,period))/overhead[1]*qty).quantize(Decimal('0.01'))
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
            _units_fit(c,o[1],newq)
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
    unit_ids: List[int] = Field(default_factory=list, max_length=200)
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
    s.shipping,s.total,s.cost_total,s.status,s.note,cu.username,s.created_at,vu.username,s.voided_at,s.void_reason,
    (SELECT floor(extract(epoch FROM rc.uploaded_at)*1000)::bigint||'|'||rc.content_type||'|'||COALESCE(ru.username,'')
     FROM receipts rc LEFT JOIN app_users ru ON ru.id=rc.uploaded_by WHERE rc.owner_type='sale' AND rc.owner_id=s.id)"""
SALE_FROM="""FROM sales s LEFT JOIN app_users cu ON cu.id=s.created_by LEFT JOIN app_users vu ON vu.id=s.voided_by"""

def _sale_dict(r, items=None):
    net=r[6]-r[7]
    d={"id":r[0],"sale_date":r[1].isoformat(),"channel":r[2],"channel_label":SALE_CHANNELS.get(r[2],r[2]),
       "payment_method":r[3],"payment_label":PAYMENT_METHODS.get(r[3],r[3]),"customer_name":r[4],"customer_contact":r[5],
       "subtotal":r[6],"discount":r[7],"shipping":r[8],"total":r[9],"net_revenue":net,"cost_total":r[10],
       "margin":net-r[10],"status":r[11],"note":r[12],"created_by":r[13],"created_at":r[14],
       "voided_by":r[15],"voided_at":r[16],"void_reason":r[17],"receipt":_receipt_info("sales",r[0],r[18])}
    if items is not None: d["items"]=items
    return d

def _sale_items(c, ids):
    if not ids: return {}
    rows=c.execute("""SELECT si.sale_id,si.product_id,p.sku,p.name,p.unit,si.quantity,si.unit_price,si.line_total,si.unit_cost,si.line_cost
        FROM sale_items si JOIN inventory_products p ON p.id=si.product_id WHERE si.sale_id=ANY(%s) ORDER BY si.id""",(list(ids),)).fetchall()
    out={}
    for r in rows:
        out.setdefault(r[0],[]).append({"product_id":r[1],"sku":r[2],"name":r[3],"unit":r[4],"quantity":r[5],
            "unit_price":r[6],"line_total":r[7],"unit_cost":r[8],"line_cost":r[9],"units":[]})
    for sid,pid,code,name in c.execute("""SELECT sale_id,product_id,code,name FROM product_units WHERE sale_id=ANY(%s) ORDER BY id""",(list(ids),)).fetchall():
        for it in out.get(sid,[]):
            if it["product_id"]==pid: it["units"].append({"code":code,"name":name})
    return out

@app.get("/sales/options")
def sales_options(request: Request):
    user_from(request)
    with psycopg.connect(DB) as c:
        rows=c.execute("""SELECT id,sku,name,unit,quantity,sale_price,size_code FROM inventory_products
            WHERE is_active AND item_type='finished_product' ORDER BY name""").fetchall()
        units={}
        for pid,uid,code,name,ph,rfor,runtil in c.execute(f"""SELECT pu.product_id,pu.id,pu.code,pu.name,{UNIT_PHOTOS_JSON},
                CASE WHEN pu.reserved_until>NOW() THEN pu.reserved_for END, CASE WHEN pu.reserved_until>NOW() THEN pu.reserved_until END
                FROM product_units pu WHERE pu.status='available' ORDER BY pu.id""").fetchall():
            units.setdefault(pid,[]).append({"id":uid,"code":code,"name":name,"photos":unit_photo_urls(uid,ph),
                                             "reserved_for":rfor,"reserved_until":runtil})
    return {"products":[{"id":r[0],"sku":r[1],"name":r[2],"unit":r[3],"quantity":r[4],"sale_price":r[5],"size_code":r[6],
                         "units":units.get(r[0],[])} for r in rows],
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
            if len(set(it.unit_ids))!=len(it.unit_ids): raise HTTPException(422,f"Hay una unidad repetida en {p[1]}")
            if len(it.unit_ids)>it.quantity: raise HTTPException(422,f"Elegiste más unidades de {p[1]} que la cantidad vendida")
            for uid in sorted(it.unit_ids):
                ur=c.execute("SELECT product_id,status,code FROM product_units WHERE id=%s FOR UPDATE",(uid,)).fetchone()
                if not ur or ur[0]!=it.product_id: raise HTTPException(422,f"La unidad {uid} no pertenece a {p[1]}")
                if ur[1]!='available': raise HTTPException(409,f"La unidad {ur[2]} ya no está disponible")
            avail=c.execute("SELECT COUNT(*) FROM product_units WHERE product_id=%s AND status='available'",(it.product_id,)).fetchone()[0]
            if avail-len(it.unit_ids)>p[3]-it.quantity:
                raise HTTPException(422,f"{p[1]} tiene piezas con ficha: elige qué unidad(es) se venden")
            line=(it.quantity*it.unit_price).quantize(Decimal('0.01'))
            lcost=(it.quantity*p[4]).quantize(Decimal('0.01'))
            subtotal+=line; cost+=lcost
            lines.append((it.product_id,it.quantity,it.unit_price,line,p[4],lcost,it.unit_ids))
        if d.discount>subtotal: raise HTTPException(422,"El descuento no puede ser mayor que el valor de los productos")
        total=(subtotal-d.discount+d.shipping).quantize(Decimal('0.01'))
        try:
            sid=c.execute("""INSERT INTO sales(sale_date,channel,payment_method,customer_name,customer_contact,subtotal,discount,shipping,total,cost_total,note,client_ref,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (sale_date,d.channel,d.payment_method,d.customer_name.strip(),d.customer_contact.strip(),subtotal,d.discount,d.shipping,total,cost,d.note.strip(),d.client_ref,u['id'])).fetchone()[0]
        except UniqueViolation:
            raise HTTPException(409,"Esta venta ya se estaba registrando. Actualiza la lista para verla.")
        for pid,q,price,line,ucost,lcost,unit_ids in lines:
            if unit_ids: c.execute("""UPDATE product_units SET status='sold',sale_id=%s,reserved_until=NULL,reserved_for='',reserved_contact='',
                reserved_note='',reserved_by=NULL,reserved_at=NULL,updated_at=NOW() WHERE id=ANY(%s)""",(sid,unit_ids))
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
        c.execute("UPDATE product_units SET status='available',sale_id=NULL,updated_at=NOW() WHERE sale_id=%s AND status='sold'",(sale_id,))
        c.execute("UPDATE sales SET status='voided',voided_by=%s,voided_at=NOW(),void_reason=%s WHERE id=%s",(u['id'],d.reason.strip(),sale_id))
    return {"id":sale_id,"status":"voided","message":f"Venta #{sale_id} anulada. Las piezas volvieron al inventario."}


# ------------------------------- COSTES -------------------------------
def _month_range(month: str):
    start=_month_date(month)
    end=date(start.year+(start.month==12), start.month%12+1, 1)
    return start,end

def _shift_month(d: date, n: int) -> date:
    y,m=divmod(d.year*12+d.month-1+n,12)
    return date(y,m+1,1)

LOCAL_TZ_NAME=os.environ.get("APP_TIMEZONE","America/Bogota")
Q2=Decimal('0.01')

@app.get("/costs/report")
def costs_report(request: Request, month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    """Rentabilidad del mes con datos registrados: ventas vigentes, órdenes completadas y gasto mensual."""
    user_from(request)
    start,end=_month_range(month)
    with psycopg.connect(DB) as c:
        # Ventas por producto. El descuento de cada venta se reparte entre sus líneas según su valor.
        rows=c.execute("""SELECT p.id,p.sku,p.name,p.unit,p.sale_price,
                SUM(si.quantity),SUM(si.line_total),
                SUM(CASE WHEN s.subtotal>0 THEN s.discount*si.line_total/s.subtotal ELSE 0 END),
                SUM(si.line_cost)
            FROM sale_items si JOIN sales s ON s.id=si.sale_id JOIN inventory_products p ON p.id=si.product_id
            WHERE s.status='completed' AND s.sale_date>=%s AND s.sale_date<%s
            GROUP BY p.id,p.sku,p.name,p.unit,p.sale_price ORDER BY SUM(si.line_total) DESC""",(start,end)).fetchall()
        products=[]
        for r in rows:
            gross=r[6]; disc=(r[7] or Decimal(0)).quantize(Q2); net=gross-disc; cost=r[8]
            products.append({"product_id":r[0],"sku":r[1],"name":r[2],"unit":r[3],"list_price":r[4],"units":r[5],
                "gross":gross,"discount":disc,"net_revenue":net,"cost":cost,"margin":net-cost,
                "avg_price":(net/r[5]).quantize(Q2) if r[5] else None,
                "unit_cost":(cost/r[5]).quantize(Q2) if r[5] else None})
        sales_agg=c.execute("""SELECT COUNT(*),COALESCE(SUM(subtotal-discount),0),COALESCE(SUM(discount),0),
                COALESCE(SUM(shipping),0),COALESCE(SUM(cost_total),0)
            FROM sales WHERE status='completed' AND sale_date>=%s AND sale_date<%s""",(start,end)).fetchone()

        # Producción completada en el mes (fecha local de cierre).
        orders=c.execute("""SELECT o.id,p.id,p.sku,p.name,p.unit,o.destination_mode,o.planned_quantity,o.good_quantity,o.defective_quantity,
                o.actual_labor_hours,o.actual_material_cost,o.actual_labor_cost,o.actual_overhead_cost,o.actual_total_cost,o.actual_unit_cost,
                o.total_cost,o.unit_cost,o.batch_overhead,o.unit_overhead_total,o.monthly_overhead_total,o.labor_hours,o.labor_cost,o.material_cost,p.sale_price
            FROM production_orders o JOIN inventory_products p ON p.id=o.finished_product_id
            WHERE o.status='completed' AND (o.completed_at AT TIME ZONE %s)::date>=%s AND (o.completed_at AT TIME ZONE %s)::date<%s
            ORDER BY o.completed_at""",(LOCAL_TZ_NAME,start,LOCAL_TZ_NAME,end)).fetchall()
        split={}
        if orders:
            for oid,itype,amount in c.execute("""SELECT om.order_id,p.item_type,SUM(om.consumed_quantity*COALESCE(om.actual_unit_cost,om.unit_cost_snapshot))
                    FROM production_order_materials om JOIN inventory_products p ON p.id=om.product_id
                    WHERE om.order_id=ANY(%s) GROUP BY om.order_id,p.item_type""",([o[0] for o in orders],)).fetchall():
                split.setdefault(oid,{})[itype]=amount
        prod={"orders":len(orders),"good_units":Decimal(0),"defective_units":Decimal(0),"labor_hours":Decimal(0),
              "raw_material":Decimal(0),"recovered_material":Decimal(0),"other_material":Decimal(0),"labor":Decimal(0),
              "batch_overhead":Decimal(0),"unit_overhead":Decimal(0),"monthly_overhead":Decimal(0),"total":Decimal(0),"planned_total":Decimal(0)}
        by_prod={}
        for o in orders:
            processed=(o[7] or 0)+(o[8] or 0); ratio=processed/o[6] if o[6] else Decimal(0)
            s=split.get(o[0],{})
            raw=s.get("raw_material",Decimal(0)); rec=s.get("recovered_material",Decimal(0))
            other=(o[10] or Decimal(0))-raw-rec
            unit_oh=o[18]*ratio; month_oh=o[19]*ratio
            prod["good_units"]+=o[7] or 0; prod["defective_units"]+=o[8] or 0; prod["labor_hours"]+=o[9] or 0
            prod["raw_material"]+=raw; prod["recovered_material"]+=rec; prod["other_material"]+=other
            prod["labor"]+=o[11] or 0; prod["batch_overhead"]+=o[17]; prod["unit_overhead"]+=unit_oh; prod["monthly_overhead"]+=month_oh
            prod["total"]+=o[13] or 0; prod["planned_total"]+=o[15]
            b=by_prod.setdefault(o[1],{"product_id":o[1],"sku":o[2],"name":o[3],"unit":o[4],"sale_price":o[23],"orders":0,"good_units":Decimal(0),
                "defective_units":Decimal(0),"actual_total":Decimal(0),"planned_total":Decimal(0),"planned_units":Decimal(0)})
            b["orders"]+=1; b["good_units"]+=o[7] or 0; b["defective_units"]+=o[8] or 0
            b["actual_total"]+=o[13] or 0; b["planned_total"]+=o[15]; b["planned_units"]+=o[6]
        for k in list(prod):
            if isinstance(prod[k],Decimal) and k not in ("good_units","defective_units","labor_hours"): prod[k]=prod[k].quantize(Q2)
        processed_total=prod["good_units"]+prod["defective_units"]
        prod["defect_rate"]=(prod["defective_units"]/processed_total*100).quantize(Decimal('0.1')) if processed_total else None
        prod["unit_cost"]=(prod["total"]/prod["good_units"]).quantize(Q2) if prod["good_units"] else None
        produced=[]
        for b in by_prod.values():
            b["actual_unit_cost"]=(b["actual_total"]/b["good_units"]).quantize(Q2) if b["good_units"] else None
            b["planned_unit_cost"]=(b["planned_total"]/b["planned_units"]).quantize(Q2) if b["planned_units"] else None
            produced.append(b)
        produced.sort(key=lambda x:x["actual_total"],reverse=True)

        # Gasto mensual: configurado frente a lo repartido en órdenes completadas imputadas a este mes.
        oh=c.execute("SELECT amount,planned_units,note FROM production_monthly_overheads WHERE period=%s",(start,)).fetchone()
        absorbed=c.execute("""SELECT COALESCE(SUM(monthly_overhead_total*(COALESCE(good_quantity,0)+defective_quantity)/planned_quantity),0),
                COALESCE(SUM(COALESCE(good_quantity,0)+defective_quantity),0)
            FROM production_orders WHERE status='completed' AND monthly_period=%s""",(start,)).fetchone()
        dep=_depreciation_for_month(c,start)
        fixed=oh[0] if oh else Decimal(0)
        overhead={"configured":oh is not None,"fixed_amount":fixed,"depreciation":dep,"amount":(fixed+dep).quantize(Q2),
                  "planned_units":oh[1] if oh else None,"note":oh[2] if oh else "",
                  "absorbed":absorbed[0].quantize(Q2),"units_charged":absorbed[1]}
        overhead["unabsorbed"]=(overhead["amount"]-overhead["absorbed"]).quantize(Q2) if oh else Decimal(0)

        # Tendencia de los últimos seis meses (incluido el elegido).
        first=_shift_month(start,-5)
        trend_rows=c.execute("""SELECT date_trunc('month',sale_date)::date,COUNT(*),SUM(subtotal-discount),SUM(cost_total)
            FROM sales WHERE status='completed' AND sale_date>=%s AND sale_date<%s GROUP BY 1""",(first,end)).fetchall()
        tmap={r[0]:r for r in trend_rows}
        emap={r[0]:r[1] for r in c.execute("""SELECT date_trunc('month',expense_date)::date,SUM(amount) FROM operating_expenses
            WHERE status='active' AND category<>'inventory' AND expense_date>=%s AND expense_date<%s GROUP BY 1""",(first,end)).fetchall()}
        trend=[]
        for i in range(6):
            m=_shift_month(first,i); r=tmap.get(m)
            net=r[2] if r else Decimal(0); cost=r[3] if r else Decimal(0)
            ex=emap.get(m,Decimal(0))
            trend.append({"month":m.strftime('%Y-%m'),"sales":r[1] if r else 0,"net_revenue":net,"cost":cost,"margin":net-cost,
                          "expenses":ex,"profit":net-cost-ex})
        exp_cats,exp_total=_expenses_by_category(c,start,end)

    net=sales_agg[1]; cogs=sales_agg[4]
    return {"month":month,
        "sales":{"count":sales_agg[0],"net_revenue":net,"discounts":sales_agg[2],"shipping":sales_agg[3],"cost":cogs,
                 "margin":net-cogs,"margin_pct":(((net-cogs)/net)*100).quantize(Decimal('0.1')) if net else None,
                 "units":sum((p["units"] for p in products),Decimal(0))},
        "products":products,"production":prod,"produced":produced,"overhead":overhead,"trend":trend,
        "expenses":{"total":exp_total,"by_category":exp_cats},
        "profit":{"amount":net-cogs-exp_total,
                  "pct":(((net-cogs-exp_total)/net)*100).quantize(Decimal('0.1')) if net else None}}



# ----------------------------- INVERSIONES -----------------------------
INVESTMENT_CATEGORIES={"equipment":"Máquinas y equipos","tools":"Herramientas","workshop":"Adecuación del taller","other":"Otro"}

class InvestmentInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    category: Literal["equipment","tools","workshop","other"] = "equipment"
    purchase_date: date
    amount: Decimal = Field(gt=0, le=9999999999)
    salvage_value: Decimal = Field(default=Decimal(0), ge=0, le=9999999999)
    useful_life_months: int = Field(ge=1, le=600)
    note: str = Field(default="", max_length=1000)
class InvestmentRetire(BaseModel):
    retired_date: date
    reason: str = Field(min_length=3, max_length=500)

def _months_between(a: date, b: date) -> int:
    """Meses completos de calendario de a hasta b (ambos como primer día de mes)."""
    return (b.year-a.year)*12+(b.month-a.month)

def _inv_monthly(amount, salvage, life):
    return ((amount-salvage)/life)

def _inv_months_charged(purchase: date, life: int, retired: Optional[date], upto: date) -> int:
    """Meses depreciados desde el mes de compra hasta 'upto' (incluido). Se detiene al terminar la vida útil o desde el mes de baja."""
    start=purchase.replace(day=1); upto=upto.replace(day=1)
    n=_months_between(start,upto)+1
    if n<=0: return 0
    n=min(n,life)
    if retired:
        n=min(n,max(0,_months_between(start,retired.replace(day=1))))
    return n

def _depreciation_for_month(c, month: date) -> Decimal:
    month=month.replace(day=1)
    rows=c.execute("""SELECT amount,salvage_value,useful_life_months,purchase_date,retired_date FROM investments
        WHERE purchase_date<%s""",(_shift_month(month,1),)).fetchall()
    total=Decimal(0)
    for amount,salvage,life,purchase,retired in rows:
        here=_inv_months_charged(purchase,life,retired,month)
        before=_inv_months_charged(purchase,life,retired,_shift_month(month,-1))
        if here>before: total+=_inv_monthly(amount,salvage,life)
    return total.quantize(Q2)

def _overhead_payload(period, r, dep):
    if not r:
        return {"period":period,"amount":Decimal(0),"planned_units":Decimal(1),"note":"","configured":False,
                "depreciation":dep,"total_amount":dep,"cost_per_unit":Decimal(0)}
    total=(r[1]+dep).quantize(Q2)
    return {"period":r[0].strftime('%Y-%m'),"amount":r[1],"planned_units":r[2],"note":r[3],"configured":True,
            "depreciation":dep,"total_amount":total,"cost_per_unit":(total/r[2]).quantize(Q2)}

INV_COLUMNS="""i.id,i.name,i.category,i.purchase_date,i.amount,i.salvage_value,i.useful_life_months,i.note,i.retired_date,i.retire_reason,
    cu.username,i.created_at,uu.username,i.updated_at"""
INV_FROM="""FROM investments i LEFT JOIN app_users cu ON cu.id=i.created_by LEFT JOIN app_users uu ON uu.id=i.updated_by"""

def _investment_dict(r, today: date):
    monthly=_inv_monthly(r[4],r[5],r[6])
    charged=_inv_months_charged(r[3],r[6],r[8],today)
    accumulated=(monthly*charged).quantize(Q2) if charged<r[6] else (r[4]-r[5]).quantize(Q2)
    book=(r[4]-accumulated).quantize(Q2)
    end=_shift_month(r[3].replace(day=1),r[6]-1)
    if r[8]: status="retired"
    elif charged>=r[6]: status="fully_depreciated"
    else: status="active"
    return {"id":r[0],"name":r[1],"category":r[2],"category_label":INVESTMENT_CATEGORIES.get(r[2],r[2]),
        "purchase_date":r[3].isoformat(),"amount":r[4],"salvage_value":r[5],"useful_life_months":r[6],"note":r[7],
        "retired_date":r[8].isoformat() if r[8] else None,"retire_reason":r[9],
        "monthly_depreciation":monthly.quantize(Q2),"months_charged":charged,"accumulated_depreciation":accumulated,
        "book_value":book,"last_month":end.strftime('%Y-%m'),"status":status,
        "created_by":r[10],"created_at":r[11],"updated_by":r[12],"updated_at":r[13]}

def _validate_investment(d):
    if d.salvage_value>=d.amount: raise HTTPException(422,"El valor de reventa debe ser menor que el valor de compra")
    if d.purchase_date>local_today(): raise HTTPException(422,"La fecha de compra no puede estar en el futuro")

@app.get("/investments")
def list_investments(request: Request):
    user_from(request)
    today=local_today()
    with psycopg.connect(DB) as c:
        rows=c.execute(f"SELECT {INV_COLUMNS} {INV_FROM} ORDER BY i.retired_date IS NOT NULL, i.purchase_date DESC, i.id DESC").fetchall()
        items=[_investment_dict(r,today) for r in rows]
        this_month=_depreciation_for_month(c,today)
        first=min((date.fromisoformat(i["purchase_date"]) for i in items),default=None)
        recovery=None
        if first:
            # Recuperación: margen de ventas acumulado desde la primera inversión frente a lo invertido.
            m=c.execute("""SELECT COALESCE(SUM(subtotal-discount-cost_total),0) FROM sales
                WHERE status='completed' AND sale_date>=%s""",(first.replace(day=1),)).fetchone()[0]
            last3=c.execute("""SELECT COALESCE(SUM(subtotal-discount-cost_total),0) FROM sales
                WHERE status='completed' AND sale_date>=%s AND sale_date<%s""",(_shift_month(today.replace(day=1),-3),today.replace(day=1))).fetchone()[0]
            invested=sum((i["amount"] for i in items),Decimal(0))
            avg=(last3/3).quantize(Q2)
            pending=max(Decimal(0),invested-m)
            recovery={"since":first.replace(day=1).strftime('%Y-%m'),"invested":invested,"margin_since":m,
                      "recovered_pct":min(Decimal(100),(m/invested*100)).quantize(Decimal('0.1')) if invested and m>0 else Decimal(0),
                      "pending":pending,"avg_monthly_margin":avg,
                      "months_to_recover":(pending/avg).quantize(Decimal('0.1')) if pending>0 and avg>0 else (Decimal(0) if pending==0 else None)}
    active=[i for i in items if i["status"]=="active"]
    return {"items":items,"categories":INVESTMENT_CATEGORIES,
        "summary":{"count":len(items),"active":len(active),"invested":sum((i["amount"] for i in items),Decimal(0)),
                   "book_value":sum((i["book_value"] for i in items if i["status"]!="retired"),Decimal(0)),
                   "monthly_depreciation":this_month,"month":today.strftime('%Y-%m')},
        "recovery":recovery}

@app.post("/investments", status_code=201)
def create_investment(d: InvestmentInput, request: Request):
    u=require_admin(request); _validate_investment(d)
    with psycopg.connect(DB) as c:
        iid=c.execute("""INSERT INTO investments(name,category,purchase_date,amount,salvage_value,useful_life_months,note,created_by,updated_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",(d.name.strip(),d.category,d.purchase_date,d.amount,d.salvage_value,
            d.useful_life_months,d.note.strip(),u['id'],u['id'])).fetchone()[0]
        r=c.execute(f"SELECT {INV_COLUMNS} {INV_FROM} WHERE i.id=%s",(iid,)).fetchone()
    return _investment_dict(r,local_today())

@app.patch("/investments/{inv_id}")
def update_investment(inv_id: int, d: InvestmentInput, request: Request):
    """Corrige los datos. Las órdenes ya creadas conservan su coste; solo cambian las siguientes."""
    u=require_admin(request); _validate_investment(d)
    with psycopg.connect(DB) as c:
        cur=c.execute("SELECT retired_date FROM investments WHERE id=%s FOR UPDATE",(inv_id,)).fetchone()
        if not cur: raise HTTPException(404,"Inversión no encontrada")
        if cur[0] and cur[0]<d.purchase_date: raise HTTPException(422,"La fecha de compra no puede ser posterior a la fecha de baja")
        c.execute("""UPDATE investments SET name=%s,category=%s,purchase_date=%s,amount=%s,salvage_value=%s,useful_life_months=%s,
            note=%s,updated_by=%s,updated_at=NOW() WHERE id=%s""",(d.name.strip(),d.category,d.purchase_date,d.amount,d.salvage_value,
            d.useful_life_months,d.note.strip(),u['id'],inv_id))
        r=c.execute(f"SELECT {INV_COLUMNS} {INV_FROM} WHERE i.id=%s",(inv_id,)).fetchone()
    return _investment_dict(r,local_today())

@app.post("/investments/{inv_id}/retire")
def retire_investment(inv_id: int, d: InvestmentRetire, request: Request):
    """Da de baja un equipo (vendido, dañado, registrado por error). Deja de depreciarse desde el mes de baja."""
    u=require_admin(request)
    if d.retired_date>local_today(): raise HTTPException(422,"La fecha de baja no puede estar en el futuro")
    with psycopg.connect(DB) as c:
        cur=c.execute("SELECT purchase_date,retired_date FROM investments WHERE id=%s FOR UPDATE",(inv_id,)).fetchone()
        if not cur: raise HTTPException(404,"Inversión no encontrada")
        if cur[1]: raise HTTPException(409,"Esta inversión ya está dada de baja")
        if d.retired_date<cur[0]: raise HTTPException(422,"La fecha de baja no puede ser anterior a la compra")
        c.execute("UPDATE investments SET retired_date=%s,retire_reason=%s,updated_by=%s,updated_at=NOW() WHERE id=%s",
                  (d.retired_date,d.reason.strip(),u['id'],inv_id))
        r=c.execute(f"SELECT {INV_COLUMNS} {INV_FROM} WHERE i.id=%s",(inv_id,)).fetchone()
    return {**_investment_dict(r,local_today()),"message":"Inversión dada de baja. Deja de sumar al gasto mensual desde ese mes."}


# ----------------------------- GASTOS DEL NEGOCIO -----------------------------
# Lo que cuesta vender y operar sin ser parte de fabricar: no entra al coste de las piezas,
# se resta del margen del mes para ver la ganancia. Lo que se gasta fabricando va en Producción.
# Excepción: una «Compra para el inventario» se registra aquí con su comprobante, pero entra al
# inventario como entrada valorizada y NO cuenta como gasto del mes: su coste se reconoce cuando
# el material se usa en una pieza que se vende. Así no se cuenta dos veces.
EXPENSE_CATEGORIES={"inventory":"Compra para el inventario","packaging":"Empaque","shipping":"Envíos y transporte","marketing":"Publicidad",
    "fees":"Comisiones y bancos","supplies":"Papelería e insumos","services":"Servicios e internet","other":"Otro"}
ExpenseCategory=Literal["inventory","packaging","shipping","marketing","fees","supplies","services","other"]

class ExpenseInventory(BaseModel):
    product_id: int
    quantity: Decimal = Field(gt=0, le=999999999)
class ExpenseInput(BaseModel):
    expense_date: date
    category: ExpenseCategory
    description: str = Field(min_length=2, max_length=160)
    amount: Decimal = Field(gt=0, le=999999999)
    payment_method: Literal["cash","transfer"] = "cash"
    note: str = Field(default="", max_length=1000)
    inventory: Optional[ExpenseInventory] = None
    client_ref: Optional[str] = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
class ExpenseVoid(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

EXP_COLUMNS="""e.id,e.expense_date,e.category,e.description,e.amount,e.payment_method,e.note,e.status,e.void_reason,
    cu.username,e.created_at,vu.username,e.voided_at,
    (SELECT floor(extract(epoch FROM rc.uploaded_at)*1000)::bigint||'|'||rc.content_type||'|'||COALESCE(ru.username,'')
     FROM receipts rc LEFT JOIN app_users ru ON ru.id=rc.uploaded_by WHERE rc.owner_type='expense' AND rc.owner_id=e.id),
    e.inventory_product_id,e.inventory_quantity,e.inventory_movement_id,ip.sku,ip.name,ip.unit"""
EXP_FROM="""FROM operating_expenses e LEFT JOIN app_users cu ON cu.id=e.created_by LEFT JOIN app_users vu ON vu.id=e.voided_by
    LEFT JOIN inventory_products ip ON ip.id=e.inventory_product_id"""

def _expense_dict(r):
    inv=None
    if r[14]:
        inv={"product_id":r[14],"quantity":r[15],"movement_id":r[16],"sku":r[17],"name":r[18],"unit":r[19],
             "unit_cost":(r[4]/r[15]).quantize(Q2) if r[15] else None}
    return {"id":r[0],"expense_date":r[1].isoformat(),"category":r[2],"category_label":EXPENSE_CATEGORIES.get(r[2],r[2]),
        "description":r[3],"amount":r[4],"payment_method":r[5],"note":r[6],"status":r[7],"void_reason":r[8],
        "created_by":r[9],"created_at":r[10],"voided_by":r[11],"voided_at":r[12],"receipt":_receipt_info("expenses",r[0],r[13]),
        "inventory":inv,"counts_as_expense":r[2]!="inventory"}

def _expenses_by_category(c, start: date, end: date):
    """Gastos del negocio vigentes del periodo, sin las compras para el inventario."""
    rows=c.execute("""SELECT category,COUNT(*),SUM(amount) FROM operating_expenses
        WHERE status='active' AND category<>'inventory' AND expense_date>=%s AND expense_date<%s
        GROUP BY category ORDER BY SUM(amount) DESC""",(start,end)).fetchall()
    cats=[{"category":r[0],"label":EXPENSE_CATEGORIES.get(r[0],r[0]),"count":r[1],"amount":r[2]} for r in rows]
    return cats,sum((x["amount"] for x in cats),Decimal(0))

@app.get("/expenses")
def list_expenses(request: Request, month: str = Query(..., pattern=r"^\d{4}-\d{2}$")):
    user_from(request)
    start,end=_month_range(month)
    with psycopg.connect(DB) as c:
        rows=c.execute(f"""SELECT {EXP_COLUMNS} {EXP_FROM} WHERE e.expense_date>=%s AND e.expense_date<%s
            ORDER BY e.expense_date DESC, e.id DESC""",(start,end)).fetchall()
        cats,total=_expenses_by_category(c,start,end)
        _,prev_total=_expenses_by_category(c,_shift_month(start,-1),start)
        purchases=c.execute("""SELECT COUNT(*),COALESCE(SUM(amount),0) FROM operating_expenses
            WHERE status='active' AND category='inventory' AND expense_date>=%s AND expense_date<%s""",(start,end)).fetchone()
    return {"month":month,"items":[_expense_dict(r) for r in rows],"categories":EXPENSE_CATEGORIES,
            "summary":{"total":total,"count":sum(x["count"] for x in cats),"by_category":cats,"previous_month_total":prev_total,
                       "inventory_purchases":{"count":purchases[0],"total":purchases[1]}}}

@app.post("/expenses", status_code=201)
def create_expense(d: ExpenseInput, request: Request):
    """Cualquier cuenta del equipo registra gastos; solo un administrador los anula.
    Una compra para el inventario crea, en la misma transacción, la entrada valorizada del artículo."""
    u=user_from(request)
    if d.expense_date>local_today(): raise HTTPException(422,"La fecha del gasto no puede estar en el futuro")
    if d.category=="inventory" and not d.inventory: raise HTTPException(422,"Elige el artículo del inventario y la cantidad comprada")
    if d.category!="inventory" and d.inventory: raise HTTPException(422,"Solo una compra para el inventario lleva artículo y cantidad")
    with psycopg.connect(DB) as c:
        if d.client_ref:
            prev=c.execute("SELECT id FROM operating_expenses WHERE client_ref=%s",(d.client_ref,)).fetchone()
            if prev:
                r=c.execute(f"SELECT {EXP_COLUMNS} {EXP_FROM} WHERE e.id=%s",(prev[0],)).fetchone()
                return {**_expense_dict(r),"duplicate":True}
        eid=c.execute("""INSERT INTO operating_expenses(expense_date,category,description,amount,payment_method,note,client_ref,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (client_ref) DO NOTHING RETURNING id""",
            (d.expense_date,d.category,d.description.strip(),d.amount,d.payment_method,d.note.strip(),d.client_ref,u['id'])).fetchone()
        if not eid:
            eid=c.execute("SELECT id FROM operating_expenses WHERE client_ref=%s",(d.client_ref,)).fetchone()
            r=c.execute(f"SELECT {EXP_COLUMNS} {EXP_FROM} WHERE e.id=%s",(eid[0],)).fetchone()
            return {**_expense_dict(r),"duplicate":True}
        if d.inventory:
            unit_cost=d.amount/d.inventory.quantity
            mid,_,_,_=_receive_into_stock(c,d.inventory.product_id,d.inventory.quantity,unit_cost,
                f"Compra registrada en Gastos: {d.description.strip()}"[:500],u['id'],allow_finished=False)
            c.execute("""UPDATE operating_expenses SET inventory_product_id=%s,inventory_quantity=%s,inventory_movement_id=%s WHERE id=%s""",
                      (d.inventory.product_id,d.inventory.quantity,mid,eid[0]))
        r=c.execute(f"SELECT {EXP_COLUMNS} {EXP_FROM} WHERE e.id=%s",(eid[0],)).fetchone()
    return _expense_dict(r)

@app.post("/expenses/{exp_id}/void")
def void_expense(exp_id: int, d: ExpenseVoid, request: Request):
    """Anula un gasto registrado por error. Se conserva con el motivo y deja de contar.
    Si era una compra para el inventario, saca del inventario lo que había entrado y deshace su efecto en el coste promedio."""
    u=require_admin(request)
    with psycopg.connect(DB) as c:
        cur=c.execute("SELECT status,inventory_product_id,inventory_quantity,amount,description FROM operating_expenses WHERE id=%s FOR UPDATE",(exp_id,)).fetchone()
        if not cur: raise HTTPException(404,"Gasto no encontrado")
        if cur[0]=="voided": raise HTTPException(409,"Este gasto ya está anulado")
        void_mid=None; message="Gasto anulado. Ya no cuenta en Costes."
        if cur[1]:
            p=c.execute("SELECT quantity,unit_cost,unit,name,item_type FROM inventory_products WHERE id=%s FOR UPDATE",(cur[1],)).fetchone()
            qty=cur[2]
            if p[0]<qty:
                raise HTTPException(409,f"No se puede anular: de esta compra entraron {fmt_qty(qty)} {p[2]} de {p[3]} y en el inventario quedan {fmt_qty(p[0])}. "
                                        "Corrige primero las existencias con un ajuste trazable.")
            new_qty=p[0]-qty
            mv=c.execute("SELECT inventory_movement_id FROM operating_expenses WHERE id=%s",(exp_id,)).fetchone()[0]
            later=c.execute("SELECT 1 FROM inventory_movements WHERE product_id=%s AND id>%s LIMIT 1",(cur[1],mv)).fetchone()
            if not later:
                # Nada se movió después de la compra: se vuelve exactamente al coste que había antes.
                new_cost=c.execute("SELECT unit_cost_before FROM inventory_movements WHERE id=%s",(mv,)).fetchone()[0]
            elif new_qty>0:
                # Deshace la compra en el promedio ponderado: (valor actual − valor comprado) ÷ lo que queda.
                new_cost=max(Decimal(0),((p[0]*p[1])-cur[3])/new_qty).quantize(Q2)
            else:
                new_cost=p[1]
            c.execute("UPDATE inventory_products SET quantity=%s,unit_cost=%s,updated_at=NOW() WHERE id=%s",(new_qty,new_cost,cur[1]))
            void_mid=c.execute("""INSERT INTO inventory_movements(product_id,user_id,movement_type,quantity_delta,quantity_after,note,unit_cost_before,unit_cost_after)
                VALUES(%s,%s,'purchase_void',%s,%s,%s,%s,%s) RETURNING id""",(cur[1],u['id'],-qty,new_qty,
                f"Compra anulada en Gastos: {cur[4]} · {d.reason.strip()}"[:500],p[1],new_cost)).fetchone()[0]
            message=f"Compra anulada. Salieron {fmt_qty(qty)} {p[2]} de {p[3]} del inventario."
        c.execute("""UPDATE operating_expenses SET status='voided',void_reason=%s,voided_by=%s,voided_at=NOW(),inventory_void_movement_id=%s
            WHERE id=%s""",(d.reason.strip(),u['id'],void_mid,exp_id))
        r=c.execute(f"SELECT {EXP_COLUMNS} {EXP_FROM} WHERE e.id=%s",(exp_id,)).fetchone()
    return {**_expense_dict(r),"message":message}

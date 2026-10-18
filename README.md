# Gatelier

Aplicación de gestión para un atelier textil consciente. Este repositorio reúne el último código disponible en el kit compartido y conserva los paquetes históricos en [`versiones/`](versiones/README.md).

## Estado de este repositorio

Este código se preparó a partir del snapshot `latest_known_update` del kit, porque no se proporcionó el checkout real del servidor. No se ha verificado contra la aplicación desplegada ni contra una base de datos real. Debe tratarse como una base de trabajo reconstruida desde los artefactos disponibles, no como una copia certificada de producción.

El starter histórico contiene un esquema PostgreSQL distinto (UUID y tablas `products`/`users`) del que espera la versión actual del backend (`BIGSERIAL` y tablas `inventory_products`/`app_users`). Por ese motivo, su migración `001_initial_schema.sql` no se instala aquí. El backend actual contiene DDL aditivo en el inicio de la aplicación; antes de usarlo con datos existentes hay que compararlo con el esquema real y preparar migraciones compatibles.

## Estructura

- `backend/app/main.py`: API FastAPI, autenticación, inventario, movimientos y producción.
- `backend/app/promote_admin.py`: utilidad para promover una cuenta existente a administrador.
- `backend/requirements.txt` y `backend/Dockerfile`: dependencias y empaquetado del API.
- `frontend/`: páginas HTML, JavaScript y el logo original.
- `versiones/`: paquetes históricos, snapshot de origen y contexto del proyecto.
- `.env.example`: nombres de variables requeridas; no contiene secretos reales.

## Configuración local

1. Crea un entorno virtual dentro de `backend/` e instala `backend/requirements.txt`.
2. Prepara PostgreSQL y configura `DATABASE_URL` y `SESSION_SECRET` usando `.env.example` como referencia. No guardes secretos reales en Git.
3. Ejecuta el API desde `backend/` con `uvicorn app.main:app --reload`.

Las páginas frontend llaman al API mediante rutas `/api/...`; el kit no contiene la configuración Nginx ni el Compose real del servidor. Para probar las páginas localmente hace falta un servidor estático con proxy `/api/` hacia FastAPI. No se ha reconstruido ni validado la configuración de producción.

El backend crea o amplía tablas al iniciar. No lo conectes a una base con datos existentes hasta revisar el esquema y las migraciones. El script `promote_admin.py` presupone que `app_users` ya existe.

## Versiones

Añade los nuevos paquetes sin reemplazar los anteriores en `versiones/paquetes/` y registra su procedencia y estado de despliegue en [`versiones/README.md`](versiones/README.md). Un ZIP preparado no demuestra que se haya instalado en el servidor.

# Gatelier

Aplicación de gestión para un atelier textil consciente. Este repositorio reúne el código fuente y la configuración incluidos en `gatelier.zip`, junto con los paquetes históricos y el contexto conservados en [`versiones/`](versiones/README.md).

## Origen y alcance

El backend y las páginas del ZIP coinciden con el snapshot histórico de administración de usuarios. El ZIP añade `compose.yaml`, Nginx, dependencias fijadas y el script para crear usuarios. Esto forma una base de proyecto coherente con los artefactos recibidos, pero no confirma qué versión está desplegada en el servidor ni cuál es el esquema activo de su PostgreSQL.

El archivo `gatelier_db.sql` del ZIP contiene filas de tablas de usuarios, inventario y movimientos. Se excluyó del repositorio para no versionar datos ni hashes de contraseñas. No lo uses para inicializar o restaurar una base productiva. Los archivos `.bak` también se excluyeron.

## Estructura

- `backend/app/main.py`: API FastAPI, autenticación, inventario, movimientos y producción.
- `backend/app/create_user.py`: creación interactiva de una cuenta.
- `backend/app/promote_admin.py`: promoción de una cuenta existente a administrador.
- `backend/requirements.txt` y `backend/Dockerfile`: dependencias fijadas y empaquetado del API.
- `frontend/`: páginas HTML, JavaScript y logo original.
- `compose.yaml` y `nginx.conf`: servicios locales y proxy de `/api/` al backend.
- `versiones/`: paquetes históricos, snapshot de referencia y documentación de contexto.
- `.env.example`: plantilla sin credenciales reales.

## Espacios público y administrativo

- `/`: catálogo público de productos terminados activos con precio configurado.
- `/admin/`: panel administrativo actual, con inventario, producción, movimientos y usuarios.
- `/api/public/catalog`: lectura pública limitada a nombre, unidad, precio y disponibilidad. No expone costes, notas internas ni cantidades exactas de stock.

El botón del catálogo abre una conversación de WhatsApp con el nombre y el precio del producto. Configura el número comercial en `frontend/store-config.js` con el formato internacional solo en dígitos. Hasta configurarlo, el botón aparece desactivado. Esta modalidad no crea un pedido en Gatelier ni reserva existencias: disponibilidad y entrega se confirman por WhatsApp.

## Ejecución local

Para un entorno local separado:

1. Copia `.env.example` a `.env` y reemplaza los valores de ejemplo. Usa una contraseña URL-safe para PostgreSQL, porque Compose la inserta en `DATABASE_URL`.
2. Ejecuta `docker compose config -q` para revisar la configuración.
3. Ejecuta `docker compose up -d --build`.
4. Abre `http://localhost:8085` (o el puerto definido en `APP_PORT`).

El Compose no publica PostgreSQL ni la API en puertos del host; publica el frontend. El backend aplica DDL al arrancar. Antes de conectar esta configuración a una base existente, compara el esquema y prepara una migración compatible; no borres ni recrees el volumen para forzar la inicialización.

Para crear una cuenta dentro del contenedor API usa `docker compose exec api python -m app.create_user NOMBRE_USUARIO`. Para promover una cuenta existente usa `docker compose exec api python -m app.promote_admin NOMBRE_USUARIO`.

## Estado de producción

Este repositorio se armó con ZIPs porque el checkout del servidor no estaba disponible durante la preparación. Antes de desplegar, contrasta `compose.yaml`, `.env`, volúmenes, Nginx y esquema PostgreSQL con el servidor real; respalda los archivos afectados y los datos. No se modificó ni verificó ningún contenedor o servicio del servidor.

## Versiones

Añade nuevos paquetes sin reemplazar los anteriores en `versiones/paquetes/` y registra su procedencia y estado en [`versiones/README.md`](versiones/README.md). No archives dumps de datos ni secretos.

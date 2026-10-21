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
- `frontend/assets/products/`: fotografías de la galería pública, con recorte limpio y formato WebP optimizado.
- `frontend/assets/og-gatelier.jpg`: imagen de vista previa (1200 × 630) que muestran WhatsApp, Instagram y otras apps al compartir un enlace. Las etiquetas Open Graph usan URLs absolutas de `https://gatelier.online`.
- `frontend/assets/cats/`: fotos de los siete gatos del atelier para la sección «Los supervisores de cada lote».

La portada incluye una guía de tallas a escala con las medidas del forro (ancho × alto): XS 16 × 23, S 19 × 26, M 22 × 30, L 24 × 32 y XL 29 × 36 cm, y un medidor que recomienda la talla con 1,5 cm de margen. Las medidas, precios y fotos de referencia viven en el arreglo `SIZES` de `frontend/index.html`. La lista de disponibilidad del inventario (`/api/public/catalog`) aparece debajo de la colección solo cuando hay referencias públicas.

Los botones de las piezas abren WhatsApp para consultar tamaño, precio y disponibilidad; las referencias con precio del inventario también muestran su precio registrado. Configura el número comercial en `frontend/store-config.js` con el formato internacional solo en dígitos. Hasta configurarlo, el botón aparece desactivado. Esta modalidad no crea un pedido en Gatelier ni reserva existencias: disponibilidad y entrega se confirman por WhatsApp.

## Panel de administración

- **Permisos.** Solo los administradores gestionan usuarios y roles, crean, renombran o eliminan categorías, cambian el gasto mensual de producción y reabren órdenes completadas. Las demás cuentas trabajan en inventario y producción.
- **Roles.** En `/admin/usuarios.html` un administrador puede hacer administrador a otra cuenta o quitarle ese permiso. No puede quitarse su propio permiso y siempre debe quedar un administrador activo. El cambio aplica de inmediato.
- **Cierre de órdenes.** Al completar una orden se registran las unidades buenas y defectuosas, las horas reales y el material realmente consumido (por defecto, lo previsto). Solo las unidades buenas entran al inventario y absorben el coste real. El coste previsto se conserva por separado para comparar.
- **Reapertura.** Una orden completada puede reabrirse: los materiales vuelven al inventario y se retiran las unidades producidas, con movimientos «Reversión de producción» en el historial. Se bloquea si esas unidades ya no están en inventario.
- **Ventas.** En `/admin/ventas.html` se registran ventas por WhatsApp, Instagram o en persona, pagadas en efectivo o por transferencia, con cliente, contacto, descuento y envío cobrado. Solo se venden productos terminados. Al guardar se descuenta el stock en una sola transacción, sin permitir negativos, y cada línea guarda el coste unitario de ese día, así el margen histórico no cambia si después cambian los costes. El ingreso neto es productos menos descuento; el envío se cobra aparte y no cuenta como ingreso por productos. Un envío repetido del mismo formulario no duplica la venta. Solo un administrador puede anular una venta: las piezas vuelven al inventario con un movimiento «Venta anulada» y la venta se conserva con el motivo.
- **Costes.** `/admin/costes.html` muestra la rentabilidad de un mes solo con datos registrados:
  - ingreso neto, coste de lo vendido (el coste guardado en cada venta) y margen;
  - rentabilidad por producto, con el descuento de cada venta repartido entre sus productos según su valor;
  - producción completada en el mes (fecha de cierre en hora de Colombia) separada en materia prima, material recuperado, mano de obra y gastos por lote, por unidad y mensuales, frente a lo previsto;
  - gasto mensual configurado frente a lo que realmente se repartió en piezas producidas;
  - margen de los últimos seis meses.
  El envío cobrado no cuenta como ingreso porque no se registra su coste.
- **Gastos del negocio.** En `/admin/gastos.html` cualquier cuenta registra lo que cuesta vender y operar sin ser parte de fabricar: empaque que no se lleva en inventario, envíos y transporte, publicidad, comisiones y bancos, papelería, servicios e internet. Cada gasto guarda fecha, categoría, concepto, valor, forma de pago, nota y quién lo registró; un envío repetido del mismo formulario no lo duplica. No se borran: solo un administrador los anula con un motivo y dejan de contar. No cambian el coste de las piezas; en Costes se restan del margen de las ventas para mostrar la **ganancia del mes**, también en la gráfica de los últimos seis meses. Lo que va dentro de cada pieza (incluidas las bolsas, si se quiere llevar su existencia) va en Inventario y en la ficha de producción; arriendo y servicios del taller, en el gasto mensual de Producción; equipos, en Inversiones.
- **Inversiones.** En `/admin/inversiones.html` un administrador registra máquinas, herramientas y mejoras del taller con fecha de compra, valor, valor de reventa opcional y vida útil. Cada una se deprecia en línea recta, (valor − reventa) ÷ meses, desde el mes de compra hasta el final de su vida útil. Esa depreciación se suma sola al gasto mensual de producción y entra al coste de las órdenes nuevas; las órdenes ya creadas conservan su coste aunque se corrija una inversión. Un equipo vendido o dañado se da de baja con fecha y motivo, deja de depreciarse desde ese mes y no se borra. La página muestra el valor pendiente por repartir y cuánto del margen de las ventas desde la primera inversión ya la cubre. Es una herramienta de gestión, no contabilidad: las vidas útiles fiscales las define la norma colombiana.
- **Referencias y unidades.** Cada producto terminado es una referencia (por ejemplo «Forro talla S», con su talla, precio y existencias). Dentro de ella, cada pieza única es una **unidad** con código propio (`FOR-S-001`…), nombre opcional y dos fotos: **cerrada**, la tela exterior, y **abierta**, la interior. Se gestionan desde Inventario → «Unidades».
  - Solo se puede crear una ficha por cada pieza que ya está en existencias; nunca hay más fichas disponibles que existencias. Por eso un ajuste o una reapertura que dejaría fichas «de más» se bloquea hasta indicar qué unidad sale.
  - «Dar de baja» saca la pieza del inventario con un movimiento trazable. «Eliminar ficha» solo borra una ficha creada por error; la pieza sigue en existencias.
  - Al vender se marca qué unidad salió. Si todas las piezas tienen ficha, es obligatorio. Al anular la venta, la unidad vuelve a estar disponible.
  - En la portada, cada talla lista las unidades disponibles con foto cerrada, su vista abierta, su precio y un botón de WhatsApp con su código.
  - **Reservas.** Desde «Unidades», «Reservar…» aparta una pieza para un cliente (nombre, contacto, nota) por 24, 48, 72 horas o una semana. Mientras está reservada no aparece en la tienda; al vencer vuelve sola, sin tarea programada. «Liberar» la devuelve antes. En Ventas la pieza aparece marcada «Reservada · cliente» y al venderla se borra la reserva.
  - **Enlace de cada pieza.** `https://gatelier.online/#CODIGO` abre la talla y resalta esa pieza; si ya se vendió o está reservada, la página lo avisa. El panel tiene «Copiar enlace» y la tienda «Compartir esta pieza»; el mensaje de WhatsApp incluye el enlace.
  - Las fotos se reducen en el navegador a menos de 900 KB, porque Nginx acepta 1 MB por petición. Se validan como JPG, PNG o WebP reales y se guardan en PostgreSQL (`unit_photos`), dentro del mismo volumen y respaldo, sin cambios en `compose.yaml`. Las fotos subidas antes a nivel de referencia se trasladan solas a una primera unidad.
- **Zona horaria.** La fecha por defecto de una venta usa `APP_TIMEZONE` (por defecto `America/Bogota`); si la imagen no trae datos de zonas horarias, se usa UTC−5.
- **Migración.** Al arrancar, la API añade las columnas nuevas de producción sin borrar datos y registra las órdenes ya completadas como «real = previsto», que es lo que ocurrió con la versión anterior. Es idempotente.

## Pruebas

Las pruebas del backend usan un PostgreSQL desechable; nunca apuntes a la base de producción. El nombre de la base debe terminar en `_test`, porque se borra y se recrea en cada ejecución.

```
pip install -r backend/requirements.txt pytest httpx
TEST_DATABASE_URL=postgresql://USUARIO@localhost:5432/gatelier_test python -m pytest backend/tests
```

Cubren gastos del negocio (quién registra y quién anula, validaciones, envío repetido, resumen por categoría y ganancia en Costes), reservas de unidades, cubren unidades y fotos (fichas solo hasta las existencias, venta y anulación por unidad, bajas, ajustes bloqueados, migración de fotos, tipo y tamaño de imagen, qué ve el público), inversiones y depreciación (inicio, fin de vida útil, baja, valor de reventa, efecto en órdenes nuevas y no en las anteriores), el informe de costes con un escenario de cifras conocidas, ventas (stock, coste histórico, descuentos, doble envío, anulación y resumen del mes), permisos, roles, último administrador, cierre con consumos reales y defectuosas, órdenes por encargo, cierre doble, reapertura y su bloqueo, cancelación y que el catálogo público no exponga costes.

## Respaldo y restauración de la base

Copiar con `rsync` la carpeta del volumen de PostgreSQL mientras la base está en uso puede dejar una copia inconsistente. El respaldo diario del servidor debe hacer primero un volcado lógico, que incluye las fotos de las unidades:

```
docker compose --project-directory ~/gatelier exec -T db \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > gatelier-AAAA-MM-DD.dump
```

El script de respaldo del servidor lo guarda cada día en `/mnt/backup/gatelier-db/`, comprueba que se pueda leer con `pg_restore --list` y conserva 30 días.

**Restaurar** (borra los datos actuales de la base: haz antes un volcado del estado actual):

```
docker compose --project-directory ~/gatelier stop api
docker compose --project-directory ~/gatelier exec -T db \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < gatelier-AAAA-MM-DD.dump
docker compose --project-directory ~/gatelier start api
```

Para probar un respaldo sin tocar la base real, restáuralo en una base aparte (`createdb` dentro del contenedor) y revisa las tablas.

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

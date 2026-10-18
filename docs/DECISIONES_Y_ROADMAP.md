# Decisiones, roadmap y criterios de aceptación

## Prioridad 0 — Auditoría sin cambios
- Confirmar estado Git y estructura real.
- Confirmar los HTML y rutas actuales.
- Inspeccionar tablas/columnas PostgreSQL, endpoints y migraciones.
- Confirmar autenticación/roles y el estado de usuarios admin.
- Confirmar validación de cantidades y el flujo de movimientos.
- Confirmar fichas/órdenes y cómo se registra stock.
- Ejecutar tests existentes sin modificar datos.

## Prioridad 1 — Inventario fiable
- CRUD de referencias y tipos.
- Unidades enteras/decimales con validación backend y frontend.
- Entradas valorizadas y coste promedio ponderado.
- Ajustes con motivo, actor y movimiento.
- Stock nunca negativo.
- Movimientos por artículo en página separada, no en menú permanente.
- No modificar saldos históricos silenciosamente.

## Prioridad 2 — Producción de extremo a extremo
- Ficha de receta ligada a producto terminado.
- Materiales por unidad, merma, horas previstas y tarifa horaria.
- Costes adicionales por lote, por unidad y mensuales.
- Órdenes por encargo y para inventario.
- Consumos reales y horas reales.
- Finalización idempotente y transaccional.
- Entrada de unidades terminadas solo para producción destinada a inventario.
- Costes previstos vs reales; conservación del coste histórico por lote.
- Cancelación/reapertura con reversión explícita sin duplicar movimientos.

## Prioridad 3 — Ventas
- Ventas presenciales y de Instagram.
- Verificación de stock y transacción atómica.
- Registro de precio, descuento, ingreso neto y coste histórico.
- Reversión de venta documentada, sin borrar trazabilidad.

## Prioridad 4 — Costes y panel
- Mano de obra, materiales, recuperación e indirectos separados.
- Regla explícita para distribuir gastos mensuales.
- Rentabilidad por producto y periodo con fuente de datos auditable.

## Criterios de aceptación transversales
- Autorización backend para todo endpoint privado/administrativo.
- Pruebas de caso válido, inválido, concurrencia/idempotencia y datos históricos.
- Migraciones aditivas, repetibles y compatibles.
- No se duplican movimientos ni costes.
- No hay stock negativo.
- No se modifica `.env`, `compose.yaml`, logo ni servicios externos.
- README actualizado con backup, instalación y rollback.

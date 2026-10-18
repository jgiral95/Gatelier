# AGENTS.md — Gatelier

## Misión
Mantener y desarrollar Gatelier, una aplicación de gestión para un atelier textil consciente. Trabaja sobre el repositorio real, no reconstruyas el proyecto basándote en ZIP históricos ni asumas que una actualización preparada fue desplegada.

## Reglas no negociables
- Responder/documentar en español salvo que el usuario pida otro idioma.
- **Nunca cambiar la tipografía, geometría, composición ni proporciones del logo.** Reutilizar el archivo original `frontend/assets/gatelier-logo-white.png` cuando exista; no redibujarlo ni sustituirlo por texto estilizado.
- Color de marca: KOLOR BL-019 Cremona. Subtítulo: «Atelier textil consciente».
- No modificar `.env` ni `compose.yaml` sin autorización explícita.
- No publicar la API en un puerto nuevo del host. El puerto 8000 del host se usa por Portainer; la API de Gatelier debe permanecer interna a Docker, detrás de Nginx.
- No detener, eliminar, recrear ni reconfigurar otros servicios Docker del servidor.
- No borrar ni reinicializar volúmenes o tablas de PostgreSQL.
- Nunca incluir secretos, contraseñas, JWT/session secrets o contenido real de `.env` en archivos, logs, commits o ZIP.
- No sobrescribir todo el proyecto para implementar una mejora puntual.
- Antes de tocar código, inspeccionar el repositorio, el estado de Git, la estructura real, las rutas API y el esquema real de DB.
- Antes de desplegar, crear respaldo de los archivos afectados y diseñar migraciones aditivas, idempotentes y compatibles con datos existentes.
- No afirmar que pruebas, despliegues o migraciones funcionaron si no se ejecutaron y verificaron.

## Stack y despliegue conocido
- Backend: FastAPI/Python.
- DB: PostgreSQL 16 en Docker.
- Frontend: HTML/CSS/JavaScript estático.
- Nginx sirve el frontend y hace proxy de `/api/` al servicio `api:8000`.
- Docker Compose.
- Dominio conocido: `https://gatelier.jgiral95.casa` vía Cloudflare Tunnel.
- Puerto de frontend del host: normalmente 8085.
- API en puerto interno 8000; no mapear `8000:8000` en el host.
- El usuario ha descrito la carpeta del servidor como `~/gatelier`, pero confirmar en el entorno real.

## Identidad y navegación
- La portada pública de clientes se sirve en `/`; el panel interno existente se sirve bajo `/admin/`.
- No exponer costes, notas internas ni cantidades exactas de inventario en el catálogo público.
- Las consultas de compra se hacen por WhatsApp; no afirmar que se registra o reserva una venta si no hay una transacción de venta implementada.
- Menú lateral uniforme en las páginas principales, orden acordado:
  1. Inventario
  2. Ventas
  3. Producción
  4. Costes
- «Movimientos» NO debe ser una opción permanente del menú lateral. Se abre desde la acción Movimientos de un artículo del inventario y debe filtrar por ese producto.
- El nombre de usuario y cerrar sesión deben estar accesibles sin scroll innecesario.
- Evitar un destello de la pantalla de login al navegar entre páginas con sesión válida: ocultar superficies hasta terminar de verificar la sesión, pero siempre mostrar errores reales y evitar estados ocultos indefinidamente.
- Evitar `alert()` para flujos normales; usar páginas, mensajes y confirmaciones consistentes.

## Inventario
Tipos de artículo conocidos:
- Materia prima.
- Material recuperado.
- Producto en proceso.
- Producto terminado.

Una referencia/SKU representa una clase de artículo, no una unidad individual serializada.
- Coste unitario y precio de venta son campos distintos.
- Unidades discretas como unidad, rollo y paquete solo admiten cantidades enteras; metros, centímetros, kilogramos y gramos pueden admitir decimales.
- Validar las cantidades tanto en frontend como en backend. No redondear stock automáticamente.
- No permitir stock negativo.
- Si un artículo tiene cantidad fraccionaria, no permitir cambiarlo a una unidad discreta hasta corregir el stock mediante un movimiento trazable.
- Usar coste promedio ponderado para entradas valorizadas, si coincide con la implementación real actual.
- Los materiales recuperados deben incluir costes de recuperación, clasificación, limpieza y preparación, incluso cuando el material original fue donado.
- Los movimientos deben conservar fecha/hora, artículo, referencia, tipo, cantidad, saldo, coste aplicable, motivo, usuario y vínculo a entrada/venta/producción cuando corresponda.

## Producción
Preferencias confirmadas por el usuario:
- Fabricación por encargo y para inventario.
- Mano de obra calculada por horas.
- Gastos adicionales combinados: por lote, por unidad y gastos mensuales distribuidos entre producción prevista.

Fichas:
- Cada ficha debe vincularse obligatoriamente a un artículo existente de tipo «Producto terminado».
- Definir materiales, cantidad por unidad, merma prevista, horas previstas, tarifa por hora y costes adicionales.
- Backend debe validar la relación; no basta con filtrar opciones en la UI.
- Considerar versionado de fichas/recetas para conservar la historia si cambian materiales o costes.

Órdenes:
- Estados explícitos (p. ej. borrador, planificada, en proceso, finalizada, cancelada), según modelo real.
- Registrar cantidad, modalidad, consumos reales, horas reales y costes.
- Operaciones de finalizar/cancelar/reabrir deben ser transaccionales e idempotentes para evitar movimientos duplicados.
- Al finalizar: registrar consumos, descontar materiales, calcular coste final y registrar unidades buenas.
- Para producción de inventario, añadir producto terminado al stock. Para encargo, no incrementar stock disponible salvo decisión explícita.
- Registrar costes previstos y reales por separado. Definir el tratamiento de merma y unidades defectuosas.
- Gastos mensuales requieren una regla de asignación explícita, trazable y sin doble contabilización.

## Ventas y costes futuros
- Ventas presenciales y desde Instagram.
- Validar stock, impedir negativo, registrar descuentos/precio/cantidades y conservar el coste histórico de la venta.
- No recalcular ventas antiguas con el coste actual del inventario.
- Separar materiales, recuperación, mano de obra, costes indirectos, coste fabricación, precio, ingreso neto y márgenes.
- No construir informes sobre datos que no se registren de forma fiable.

## Autenticación y administración
La implementación conocida usa FastAPI, PostgreSQL, cookies HttpOnly y JWT firmado con un secreto de entorno; confirmar el código real antes de tocarlo.
- Contraseñas hasheadas (Argon2 fue mencionado en una versión previa).
- Admin puede crear usuarios, restablecer contraseñas, desactivar/reactivar cuentas y gestionar roles.
- Usuarios normales no deben poder llamar a endpoints administrativos.
- Nunca se puede recuperar una contraseña original en texto plano; se establece una nueva.
- Preferir desactivación lógica si existen movimientos asociados.
- Proteger contra desactivar la propia cuenta o al último administrador activo.
- Revisar el script `backend/app/promote_admin.py` si existe; no asumir que su versión histórica está instalada.

## Proceso de trabajo requerido
1. Auditoría de solo lectura antes de cambios.
2. Inspeccionar `git status`, archivos, dependencias, endpoints, tablas y migraciones.
3. Resumir el plan y riesgos.
4. Implementar cambios localizados y completos.
5. Añadir pruebas automatizadas para reglas de negocio, permisos y casos límite.
6. Validar migraciones, tests, build y configuración.
7. Documentar instalación, respaldo y rollback.
8. Para despliegue: respaldar solo archivos afectados; preservar `.env`, `compose.yaml`, volúmenes y otros servicios.
9. Mantener historial de costes/movimientos; nunca alterar silenciosamente datos históricos.
10. Si el estado real contradice estos requisitos, documentar la diferencia y pedir aclaración solo cuando sea una decisión de negocio imprescindible.

## Primera tarea
Auditoría de solo lectura, sin cambiar archivos ni DB: inventario del repositorio, arquitectura/API, esquema DB, autenticación/permisos, inventario/movimientos, fichas/órdenes, usuarios/admin, pruebas, divergencias, riesgos y plan priorizado. La fuente de verdad es el repositorio/servidor real, no este kit ni los ZIP históricos.

# Contexto maestro de Gatelier para Codex

## Resumen ejecutivo
Gatelier es una aplicación privada de gestión de un atelier textil consciente. Debe cubrir inventario, movimientos, producción, ventas, costes y administración de usuarios. Se despliega en Ubuntu usando Docker Compose. La arquitectura conocida es FastAPI + PostgreSQL + frontend HTML/CSS/JavaScript + Nginx, con Cloudflare Tunnel delante.

## Marca
- Marca: Gatelier.
- Subtítulo: «Atelier textil consciente».
- Color corporativo: KOLOR BL-019 Cremona.
- Regla crítica: nunca cambiar la tipografía ni las proporciones del logo.
- Logo conocido en actualizaciones anteriores: `frontend/assets/gatelier-logo-white.png`.

## Infraestructura descrita por el usuario
- SSH alias: `gatelier`; usuario/host vistos en registros: `jgiral95@server`.
- Carpeta de proyecto: `~/gatelier`.
- Dominio: `https://gatelier.jgiral95.casa`.
- Frontend en host port 8085.
- API FastAPI en port 8000 interno de Docker.
- Portainer usa host port 8000, por tanto no publicar la API en ese puerto.
- Otros contenedores comparten el servidor: Node-RED, InfluxDB, Mosquitto, Portainer, Jellyfin, Home Assistant, Grafana, Nextcloud/MariaDB, Immich, Cloudflared, Bazarr, Radarr, Sonarr, Prowlarr, Redis, Actual Budget, MongoDB, Agent DVR, ERPNext, qBittorrent, Glances, Jellyseerr y Overleaf. No tocarlos.
- Nunca compartir ni empaquetar `.env` real. Solo `.env.example` con placeholders si hace falta.

## Preferencias funcionales confirmadas
- Fabricación por encargo y para inventario.
- Mano de obra por horas.
- Gastos por lote, por unidad y mensuales distribuidos entre la producción prevista.
- Fichas ligadas a productos de tipo producto terminado.
- Menú uniforme: Inventario, Ventas, Producción, Costes.
- Movimientos no debe estar en el menú izquierdo; se abre desde el botón Movimientos de cada artículo del inventario.
- No mostrar fugazmente login cuando se navega con una sesión válida.
- Admins pueden crear usuarios, restablecer contraseñas y desactivar/reactivar cuentas; usuarios no-admin no deben acceder a esas operaciones.
- Unidades discretas (unidad, rollo, paquete) deben ser enteras; unidades físicas fraccionables pueden ser decimales.
- Coste separado del precio de venta.
- Coste promedio ponderado para entradas valorizadas.
- No stock negativo; todo ajuste debe ser movimiento trazable.

## Separación público / administración
- La portada pública para clientes se sirve en `/`; el panel interno existente se sirve bajo `/admin/`.
- La API pública del catálogo solo expone productos terminados, nombre, unidad, precio de venta y una señal booleana de disponibilidad; no expone coste, notas internas ni cantidad exacta.
- Las consultas de compra se hacen por WhatsApp. Esta modalidad no registra ventas ni reserva existencias.
- El número comercial se configura en `frontend/store-config.js` y es información pública.

## Historial de archivos/paquetes disponibles en esta conversación
Se incluyen en `history/updates/` los ZIP disponibles, sin secretos:
- `gatelier_login_inventario_mvp.zip`
- `gatelier_inventario_edicion_categorias_stock.zip`
- `gatelier_inventario_tipos_logo_v3.zip`
- `gatelier_inventario_tipos_logo_v3_1_sidebar.zip`
- `gatelier_entrada_valorizada_v4.zip`
- `gatelier_entrada_valorizada_v4_1.zip`
- `gatelier_movimientos_pagina_v5.zip`
- `gatelier_produccion_v1.zip`
- `gatelier_navegacion_sesion_v1.zip`
- `gatelier_fichas_producto_menu_uniforme_v1_2.zip`
- `gatelier_usuarios_admin_v1.zip`
- `gatelier-app-starter.zip`

`snaphots/latest_known_update/` contiene los archivos extraídos del ZIP de administración de usuarios, que es el paquete incremental más reciente disponible en esta conversación. No equivale necesariamente al estado instalado en el servidor.

`snapshots/starter_project/` contiene el starter inicial, no una versión completa y actual del sistema desplegado. Los dos snapshots no se han fusionado deliberadamente porque mezclar snapshots con esquemas diferentes puede producir una falsa imagen del estado actual.

## Importante: límites de este kit
Este ZIP reúne todos los archivos de Gatelier que estaban disponibles en esta conversación, los paquetes históricos disponibles y el contexto funcional/técnico para Codex. No incluye el repositorio Git completo del servidor, una copia de la base de datos, volúmenes Docker ni el `.env` real. Por tanto, Codex debe comenzar auditando el repositorio real y no tratar el ZIP más reciente como prueba de que está desplegado.

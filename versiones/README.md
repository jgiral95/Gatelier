# Archivo de versiones de Gatelier

Este directorio conserva los paquetes y snapshots de Gatelier disponibles en el kit compartido. Es un archivo de referencia para continuar el trabajo y mantener historial en Git; **no representa ni certifica el estado instalado en el servidor**.

## Contenido

- `paquetes/`: ZIP históricos, conservados sin modificar.
- `snapshots/latest_known_update/`: archivos extraídos del paquete de administración de usuarios, la actualización más reciente disponible en el kit.
- `contexto/`: requisitos y decisiones compartidas para orientar el mantenimiento. Son documentación de referencia, no evidencia del despliegue.

El snapshot `starter_project` no se incorporó como código activo porque su esquema y estructura corresponden a una etapa inicial y difieren del snapshot más reciente.

## Añadir una versión

1. Copia el ZIP recibido en `paquetes/` sin reemplazar paquetes anteriores.
2. Usa el nombre original si es único; si se repite, agrega la fecha de recepción al nombre.
3. Añade una entrada al registro de versiones con fecha, origen, contenido principal y estado de despliegue verificado o pendiente.
4. Si se prepara un snapshot legible, guárdalo en una carpeta nueva bajo `snapshots/` y documenta de qué paquete proviene. No mezcles snapshots de esquemas distintos.
5. Revisa `git status`, confirma que no se incluyeron secretos ni `.env` real, y crea un commit descriptivo.

## Registro inicial

| Origen | Contenido | Despliegue |
| --- | --- | --- |
| `paquetes/*.zip` | Paquetes incrementales históricos incluidos en `Gatelier_Codex_Kit_Completo.zip` | No verificado |
| `snapshots/latest_known_update/` | Backend y páginas del paquete `gatelier_usuarios_admin_v1.zip` | No verificado |

El kit también contenía una captura diagnóstica del servidor. No se archivó aquí porque incluye detalles operativos de otros servicios y no confirma el estado actual.

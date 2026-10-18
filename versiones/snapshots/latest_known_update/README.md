# Gatelier — Administración de usuarios v1

## Funciones
- Roles `admin` y `user`; las cuentas existentes se migran como `user` por seguridad.
- Página `usuarios.html` visible en el menú solo para administradores.
- Crear usuarios, restablecer contraseña y desactivar/reactivar cuentas.
- Desactivar conserva referencias históricas y no borra movimientos; las sesiones se revocan al resetear contraseña o desactivar una cuenta.
- La API protege cada operación administrativa. No se confía solo en ocultar botones.
- La migración agrega columnas `role` y `session_version`; no elimina ni recrea tablas existentes.

## Primer administrador
Después de desplegar, promueve tu usuario existente (no la contraseña) dentro del contenedor API:

```bash
docker compose exec api python -m app.promote_admin TU_USUARIO_EXISTENTE
```

Usa exactamente el nombre de usuario con el que inicias sesión. No crees un usuario nuevo para reemplazar la cuenta existente. Para dar rol de administrador a otro usuario existente, repite el comando con su nombre.

## Despliegue
Respaldar los archivos listados antes de copiarlos. Sustituye `backend/app/main.py`, `frontend/index.html`, `frontend/movimientos.html`, `frontend/produccion.html` y añade `frontend/usuarios.html`; añade `backend/app/promote_admin.py`. Ejecuta `docker compose config -q && docker compose up -d --build`. No sustituye `.env`, `compose.yaml`, el volumen PostgreSQL ni los archivos de logo.

## Seguridad
- Contraseñas guardadas mediante el hasher Argon2 ya utilizado por Gatelier; nunca se pueden consultar en texto plano.
- Contraseña mínima de 10 caracteres.
- Reset de contraseña incrementa `session_version` y revoca sesiones anteriores.
- No se permite desactivar la propia cuenta ni el último administrador activo.
- El botón “Desactivar” es un borrado lógico deliberado para conservar el historial de inventario y producción.

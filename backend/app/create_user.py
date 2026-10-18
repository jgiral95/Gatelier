import getpass
import re
import sys

import psycopg
from pwdlib import PasswordHash

from app.main import DATABASE_URL

if len(sys.argv) != 2:
    print("Uso: python -m app.create_user NOMBRE_USUARIO")
    sys.exit(1)

username = sys.argv[1].strip()

if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username):
    print("El usuario debe tener entre 3 y 80 caracteres: letras, números, punto, guion o guion bajo.")
    sys.exit(1)

password = getpass.getpass("Nueva contraseña: ")
confirmation = getpass.getpass("Repite la contraseña: ")

if password != confirmation:
    print("Las contraseñas no coinciden.")
    sys.exit(1)

if len(password) < 12:
    print("Usa una contraseña de al menos 12 caracteres.")
    sys.exit(1)

hashed = PasswordHash.recommended().hash(password)

try:
    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO app_users (username, password_hash)
            VALUES (%s, %s)
            """,
            (username, hashed),
        )
    print(f"Usuario '{username}' creado correctamente.")
except psycopg.errors.UniqueViolation:
    print("Ese usuario ya existe. No se modificó su contraseña.")
    sys.exit(1)

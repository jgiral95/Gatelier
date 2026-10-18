"""Promote an existing Gatelier account to administrator. Run inside the API container."""
import os, sys
import psycopg

def main():
    if len(sys.argv) != 2:
        print("Uso: python -m app.promote_admin NOMBRE_USUARIO", file=sys.stderr)
        raise SystemExit(2)
    username=sys.argv[1].strip()
    db=os.environ.get("DATABASE_URL")
    if not db:
        print("DATABASE_URL no está configurada", file=sys.stderr); raise SystemExit(2)
    with psycopg.connect(db) as c:
        row=c.execute("UPDATE app_users SET role='admin' WHERE username=%s RETURNING username,role,is_active",(username,)).fetchone()
    if not row:
        print(f"No existe el usuario {username}. Créalo primero o verifica el nombre.",file=sys.stderr); raise SystemExit(1)
    print(f"{row[0]} ahora tiene rol {row[1]} (activo={row[2]}).")
if __name__=="__main__": main()

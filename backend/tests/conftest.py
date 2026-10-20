"""Pruebas contra un PostgreSQL real y desechable.

Uso: define TEST_DATABASE_URL apuntando a un servidor de pruebas (nunca al de producción).
La base indicada se borra y se recrea al inicio de la sesión de pruebas.
"""
import os
import sys
from pathlib import Path

import psycopg
import pytest

TEST_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_URL:
    pytest.skip("Define TEST_DATABASE_URL para ejecutar las pruebas", allow_module_level=True)

base, _, dbname = TEST_URL.rpartition("/")
if not dbname.endswith("_test"):
    raise RuntimeError("Por seguridad, la base de pruebas debe terminar en _test")

with psycopg.connect(base + "/postgres", autocommit=True) as c:
    c.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    c.execute(f'CREATE DATABASE "{dbname}"')

os.environ["DATABASE_URL"] = TEST_URL
os.environ.setdefault("SESSION_SECRET", "solo-para-pruebas-" + "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, hasher  # noqa: E402

PASSWORD = "clave-de-prueba-123"


@pytest.fixture(scope="session")
def app_ready():
    with TestClient(app, base_url="https://testserver"):
        yield


def _ensure_user(username, role):
    with psycopg.connect(TEST_URL) as c:
        c.execute(
            """INSERT INTO app_users(username,password_hash,role) VALUES(%s,%s,%s)
            ON CONFLICT(username) DO UPDATE SET role=EXCLUDED.role,is_active=TRUE""",
            (username, hasher.hash(PASSWORD), role),
        )


def _client(username, role):
    _ensure_user(username, role)
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/auth/login", data={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def admin(app_ready):
    return _client("admin_pruebas", "admin")


@pytest.fixture
def user(app_ready):
    return _client("taller_pruebas", "user")


@pytest.fixture
def db():
    with psycopg.connect(TEST_URL) as c:
        yield c


_counter = {"n": 0}


def unique(prefix):
    _counter["n"] += 1
    return f"{prefix}-{os.getpid()}-{_counter['n']}"

"""
Phase 2B.1 #3 — SecurityEventDB is APPLICATION-append-only, not DB-enforced (see
models.py docstring and ENFORCEMENT_LEVELS.md). These tests prove the
application-level guarantee: no endpoint, and no line of backend code, ever
updates or deletes a SecurityEventDB row. They do NOT (and cannot) prove a
DB-level guarantee — see ENFORCEMENT_LEVELS.md for what that would require.
"""

import ast
import inspect
import pathlib

from backend.models import MemberRole
from backend.routers import admin as admin_router

from .conftest import auth_headers, login, seed_membership

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_no_route_exposes_update_or_delete_for_security_events():
    for route in admin_router.router.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "security-event" in path:
            assert methods == {"GET"}, f"{path} exposes {methods}, only GET is allowed"


def test_deleting_or_patching_security_events_endpoint_is_rejected(client, db_session):
    seed_membership(db_session, "TFakeAuditAdmin0000000000000001", role=MemberRole.ADMIN)
    admin_token = login(client, "TFakeAuditAdmin0000000000000001")

    delete_resp = client.delete("/admin/security-events/1", headers=auth_headers(admin_token))
    assert delete_resp.status_code in (404, 405)  # no existe la ruta en absoluto

    patch_resp = client.patch("/admin/security-events", headers=auth_headers(admin_token))
    assert patch_resp.status_code in (404, 405)


def test_no_backend_source_file_calls_update_or_delete_on_security_events():
    """Escaneo estatico (AST) de TODO backend/: ningun archivo (fuera de los
    tests) invoca `.update(...)` sobre un `update(SecurityEventDB)` de
    SQLAlchemy, ni `db.delete(...)` sobre una instancia de SecurityEventDB, ni
    `.query(SecurityEventDB)....delete()`. Es una prueba estructural, no
    exhaustiva contra ofuscacion deliberada — ver ENFORCEMENT_LEVELS.md."""
    offending: list[str] = []
    for path in BACKEND_ROOT.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts or "__pycache__" in path.parts or "alembic" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "SecurityEventDB" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("delete", "update") :
                    # unicamente marcamos si el codigo tambien menciona SecurityEventDB
                    # en el mismo archivo (heuristica conservadora, ver docstring)
                    offending.append(f"{path.name}: possible {node.func.attr}() call near SecurityEventDB usage")
    # el UNICO uso legitimo de "update"/"delete" en todo backend/ que tambien
    # menciona SecurityEventDB en el mismo archivo no deberia existir en absoluto
    assert offending == [], offending


def test_log_security_event_only_ever_adds_never_mutates():
    source = inspect.getsource(__import__("backend.security.audit", fromlist=["log_security_event"]))
    assert "db.add(event)" in source
    assert ".delete(" not in source
    assert "update(SecurityEventDB" not in source

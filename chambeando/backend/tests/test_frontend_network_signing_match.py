"""
Hallazgo de revision (2026-09-25): frontend/link/index.html solo aceptaba
"bsc" (luego renombrado a "evm", ver backend/chain/auth_networks.py --
personal_sign no liga la firma a ninguna cadena EVM especifica), pero JOIN
heredado sigue emitiendo un link a /link con network=tron -- roto de punta
a punta para TRON aunque el backend/router siguieran funcionando.
Corregido (la pagina ahora soporta ambas redes, cada una con su propio
metodo de firma). Este archivo prueba la conexion real entre las dos
puntas: toma la URL que el ConversationRouter REAL emite para cada comando
(JOIN, JOIN EVM), extrae `network`, y ejecuta el <script> inline REAL de
index.html (via frontend/link/test_signing_harness.mjs, Node +
vm.runInContext, wallets simuladas) para confirmar que la pagina:

1. Acepta esa red exacta (el boton se habilita, no queda en "Red no
   soportada").
2. Usa el metodo de firma que el verificador real del backend espera para
   esa familia de cadena -- signMessageV2 para tron, personal_sign para
   evm -- nunca el metodo deprecado tronWeb.trx.sign() (que produce una
   firma que el backend SIEMPRE rechazaria, ver comentario en index.html).

No reimplementa la logica de la pagina en Python -- ejecuta el archivo
real. Requiere `node` en PATH (ya usado en otras verificaciones de esta
fase).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.messaging.router import ConversationRouter
from backend.messaging.whatsapp_adapter import SandboxMetaClient, WhatsAppAdapter

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "link"
HARNESS = FRONTEND_DIR / "test_signing_harness.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node no esta en PATH en este entorno")


def _extract_network(text: str) -> str:
    match = re.search(r"[?&]network=([^&\s]+)", text)
    assert match, f"no se encontro `network=` en el mensaje del router: {text!r}"
    return match.group(1)


def _run_harness(network: str) -> dict:
    result = subprocess.run(
        ["node", str(HARNESS), network],
        cwd=str(FRONTEND_DIR),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"el arnes de node fallo (exit {result.returncode}): stderr={result.stderr!r}"
    return json.loads(result.stdout)


def test_tron_join_link_network_is_accepted_by_the_frontend_with_the_correct_signing_method(db_session):
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-frontend-match-tron-1", "JOIN")
    _, text = mock.sent[-1]
    network = _extract_network(text)
    assert network == "tron"

    result = _run_harness(network)
    assert result["loadError"] is None
    assert result["btnDisabledAfterLoad"] is False, "la pagina rechazo network=tron -- deberia aceptarla"
    assert result["clickError"] is None
    assert result["finalStatusText"] == "Wallet vinculada correctamente. Volve a WhatsApp para continuar."

    methods_called = [c[0] for c in result["calls"]]
    assert "tronWeb.trx.signMessageV2" in methods_called
    assert "tronWeb.trx.sign" not in methods_called  # metodo deprecado, incompatible con el verificador real
    assert not any("ethereum.request" in m for m in methods_called)  # nunca mezcla el flujo EVM para tron


def test_join_evm_link_network_is_accepted_by_the_frontend_with_the_correct_signing_method(db_session):
    mock = SandboxMetaClient()
    router = ConversationRouter(WhatsAppAdapter(mock), wallet_handoff_enabled=True)
    router.handle_inbound(db_session, "wa-frontend-match-evm-1", "JOIN EVM")
    _, text = mock.sent[-1]
    network = _extract_network(text)
    assert network == "evm"

    result = _run_harness(network)
    assert result["loadError"] is None
    assert result["btnDisabledAfterLoad"] is False, "la pagina rechazo network=evm -- deberia aceptarla"
    assert result["clickError"] is None
    assert result["finalStatusText"] == "Wallet vinculada correctamente. Volve a WhatsApp para continuar."

    methods_called = [c[0] for c in result["calls"]]
    assert any(m.startswith("ethereum.request:personal_sign") for m in methods_called)
    assert not any(m.startswith("tronWeb") or m.startswith("tronLink") for m in methods_called)  # nunca mezcla el flujo TRON para evm


def test_frontend_still_rejects_a_network_neither_join_command_ever_emits():
    """Control negativo: una red que ningun comando real produce (ej.
    "ethereum") sigue rechazada por la pagina -- el boton nunca se habilita,
    ninguna wallet se toca."""
    result = _run_harness("ethereum")
    assert result["btnDisabledAfterLoad"] is True
    assert result["calls"] == []


def test_frontend_rejects_bsc_specifically_preventing_a_false_bsc_claim():
    """Prueba negativa pedida explicitamente (hallazgo de revision
    posterior): la pagina NUNCA acepta "bsc" como red -- ni siquiera como
    valor de query string -- porque personal_sign no prueba ninguna cadena
    EVM especifica. Una wallet EVM con cualquier cadena seleccionada nunca
    puede producir una afirmacion de "bsc" a traves de esta pagina, porque
    "bsc" ya ni siquiera es un valor de red que la pagina reconozca."""
    result = _run_harness("bsc")
    assert result["btnDisabledAfterLoad"] is True
    assert result["calls"] == []

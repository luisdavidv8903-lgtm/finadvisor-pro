"""
Cifrado en reposo para datos de liquidacion (settlement details). Ver seccion 9
del informe de Phase 2B para el resumen de algoritmo/limitaciones/upgrade path.

ALGORITMO: Fernet (AES-128-CBC + HMAC-SHA256, de la libreria `cryptography`,
ya una dependencia transitiva via python-jose[cryptography], listada aqui de
forma explicita). Fernet fue elegido en vez de armar AEAD a mano porque es una
implementacion simple, auditada, de "un solo esquema" — exactamente el tipo de
"no escribas tu propio wrapper criptografico" que ya aplicamos con SafeTRC20 en
el contrato.

FUENTE DE LA CLAVE: variable de entorno `SETTLEMENT_ENCRYPTION_KEY` (obligatoria,
sin default — ver config.py). Nunca hardcodeada, nunca commiteada.

LO QUE SE CIFRA: unicamente `SettlementDetailDB.encrypted_payload` (el JSON con
los datos de cuenta/telefono/etc. de liquidacion fiat). Todo lo demas en esa
fila (payment_method generico, currency, active, timestamps) NO es sensible y
se guarda en claro a proposito, porque son campos que MEMBER puede ver.

LIMITACIONES DE ESTA FASE (produccion NO deberia quedarse aqui):
  - Clave unica y estatica por entorno — no hay rotacion de claves ni versionado
    del payload por clave usada. Rotar hoy implica re-cifrar todo a mano.
  - La clave vive en una variable de entorno del proceso backend — quien tenga
    acceso al proceso/host tiene la clave. No hay separacion de custodia (HSM/KMS).
  - Sin envelope encryption (data key por registro envuelta por una master key)
    — cada registro usa la misma clave simetrica directamente.

CAMINO DE UPGRADE A PRODUCCION: envelope encryption con AWS KMS / GCP KMS / un
HSM — la master key nunca sale del servicio gestionado, se genera una data key
por registro (o por owner_user_id), se cifra el payload con la data key, y se
guarda la data key envuelta junto al payload. Esto tambien habilita rotacion de
la master key sin re-cifrar cada fila. Ese trabajo queda deliberadamente fuera
de esta fase (ver "NOT AUTHORIZED: KYC vendor integration" / "no production
cloud dependencies unless necessary for an interface" en las instrucciones).
"""

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(settings.SETTLEMENT_ENCRYPTION_KEY.encode())
    return _fernet


def encrypt_settlement_payload(payload: dict[str, Any]) -> bytes:
    """Serializa `payload` (un dict JSON-compatible con los campos sensibles, p.ej.
    {"account_holder": "...", "account_number": "...", "bank": "..."}) y lo cifra.
    El caller nunca debe loguear ni `payload` ni el resultado."""
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _get_fernet().encrypt(plaintext)


def decrypt_settlement_payload(ciphertext: bytes) -> dict[str, Any]:
    """Lanza InvalidSettlementPayload si el ciphertext no es valido para la clave
    configurada (clave rotada, dato corrupto, etc.) — nunca devuelve datos parciales."""
    try:
        plaintext = _get_fernet().decrypt(bytes(ciphertext))
    except InvalidToken as exc:
        raise InvalidSettlementPayload("no se pudo descifrar el payload de liquidacion") from exc
    return json.loads(plaintext.decode("utf-8"))


class InvalidSettlementPayload(Exception):
    pass


def reset_fernet_cache_for_tests() -> None:
    """Solo para tests: fuerza releer SETTLEMENT_ENCRYPTION_KEY (util si un test
    cambia settings.SETTLEMENT_ENCRYPTION_KEY en tiempo de ejecucion)."""
    global _fernet
    _fernet = None

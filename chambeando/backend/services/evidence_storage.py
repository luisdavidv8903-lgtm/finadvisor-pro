"""
Abstraccion de almacenamiento de evidencia de disputas. `EvidenceStorage` es el
puerto; `LocalTestEvidenceStorage` es la unica implementacion en esta fase — en
memoria, NUNCA persiste entre reinicios, NUNCA cifra, NUNCA usar en produccion.
Sirve solo para probar que la capa de negocio no depende de un proveedor cloud
concreto (ver seccion 9 del pedido: "Do not integrate AWS/S3/cloud vendor yet").
Produccion reemplaza esto por un archivo nuevo (p.ej. S3EvidenceStorage con URLs
firmadas de corta duracion) sin tocar los routers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class EvidenceStorage(ABC):
    @abstractmethod
    def store(self, *, order_id: int, content: bytes, content_type: str) -> str:
        """Guarda `content` y devuelve un storage_ref opaco — nunca una URL publica."""

    @abstractmethod
    def retrieve(self, storage_ref: str) -> bytes:
        """Lanza KeyError si storage_ref no existe."""


class LocalTestEvidenceStorage(EvidenceStorage):
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._next_id = 1

    def store(self, *, order_id: int, content: bytes, content_type: str) -> str:
        ref = f"local-test://order/{order_id}/{self._next_id}"
        self._next_id += 1
        self._store[ref] = content
        return ref

    def retrieve(self, storage_ref: str) -> bytes:
        return self._store[storage_ref]


_storage: EvidenceStorage | None = None


def get_evidence_storage() -> EvidenceStorage:
    global _storage
    if _storage is None:
        _storage = LocalTestEvidenceStorage()
    return _storage


def set_evidence_storage_for_tests(storage: EvidenceStorage) -> None:
    global _storage
    _storage = storage

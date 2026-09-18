"""
Helper unico para escribir al log de auditoria application-append-only
(SecurityEventDB) — ver ENFORCEMENT_LEVELS.md sobre por que "application" y no
"DB-enforced": no existe rol de DB separado ni trigger que impida UPDATE/DELETE
a nivel de motor, solo el hecho de que ningun codigo del backend los invoca.
Todo el backend pasa por aqui en vez de instanciar SecurityEventDB directamente,
para que la regla "nunca el valor sensible" se aplique en un solo lugar.

Phase 2B.3: este helper YA NO hace commit. `db.add(event)` + `db.flush()`
(para poblar `event.id` sin cerrar la transaccion) y listo — el CALLER es
dueño de la transaccion completa y decide cuando hacer commit/rollback. Esto
es lo que permite que la escritura principal de una accion privilegiada y su
evento de auditoria vivan en la MISMA transaccion: el caller hace
`log_security_event(db, ...)` en medio de sus propios cambios, y un unico
`db.commit()` al final cubre todo junto. Si el flush falla (p.ej. una
violacion real de FK/CHECK), la excepcion se propaga sin capturarse — este
helper nunca traga errores de DB en silencio, y el rollback (implicito al
cerrar la sesion sin commit, o explicito si el caller lo hace) deshace tanto
el evento como la escritura principal.

`reason` es texto libre para contexto humano (p.ej. "3 intentos fallidos",
"usuario reporto no haber recibido el pago") — el CALLER es responsable de que
ese texto nunca contenga el dato sensible en si (numero de cuenta, contenido de
evidencia, etc.); este helper no puede adivinar que es sensible en texto libre
arbitrario, asi que la disciplina es: nunca interpolar un valor de
SettlementDetailDB/DisputeEvidenceDB en `reason`. Los tests de logging
verifican esto por comportamiento (strings sinteticos no aparecen), no
intentando filtrar el texto aqui.
"""

from sqlalchemy.orm import Session

from ..models import SecurityEventDB, SecurityEventType


def log_security_event(
    db: Session,
    *,
    action: SecurityEventType,
    actor_user_id: int | None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    reason: str | None = None,
) -> SecurityEventDB:
    """NO hace commit — ver docstring del modulo. El caller SIEMPRE debe hacer
    su propio db.commit() (usualmente uno solo, cubriendo esto y su escritura
    principal) despues de llamar esta funcion."""
    event = SecurityEventDB(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        reason=reason,
    )
    db.add(event)
    db.flush()  # puebla event.id dentro de la transaccion del caller, sin comprometerla
    return event

# Auditoría de Seguridad — Chambeando P2P Exchange

Ver [`AUDIT.md`](./AUDIT.md) para el reporte completo (race conditions en el escrow, fallas de autorización, modelado de datos financieros y checklist de producción).

`fixed_backend/` contiene una implementación de referencia con los fixes críticos aplicados:

- Identidad derivada exclusivamente del JWT (`deps.get_current_user`) en todos los endpoints de negocio — nunca de parámetros de request.
- Hash de contraseñas (`security.get_password_hash`) en el registro.
- `SECRET_KEY` y demás configuración vía variables de entorno (`config.py`, `.env.example`), nunca hardcodeada.
- Operaciones de balance y transición de estado de orden atómicas vía `UPDATE ... WHERE <guard>` (`escrow_service.py`), eliminando las race conditions de doble gasto / doble liberación descritas en la sección 1 del audit.
- `Numeric(18, 8)` / `Numeric(12, 2)` en vez de `Float` para todos los montos financieros.
- `UniqueConstraint(user_id, currency)` en `wallets` para evitar wallets duplicados por carrera.

No es un microservicio listo para producción por sí solo — es la corrección puntual de los hallazgos críticos del MVP original. Para producción, seguir además el checklist de la sección 4.2 del audit (Alembic, rate limiting, ledger inmutable, tests de concurrencia, Postgres).

Para correr localmente:

```bash
cd fixed_backend
pip install -r requirements.txt
cp .env.example .env   # editar SECRET_KEY con un valor aleatorio real
uvicorn main:app --reload --app-dir ..
```

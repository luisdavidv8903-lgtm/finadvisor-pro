# Auditoría de Seguridad — Chambeando P2P Exchange (MVP)

**Alcance:** `main.py`, `auth.py`, `api.js`, `App.jsx` (código provisto por el equipo, aún no versionado en este repo).
**Severidad global del MVP:** 🔴 **CRÍTICA — no apto para producción ni para manejar fondos reales en su estado actual.**

Resumen ejecutivo: el modelo de dominio (escrow P2P con estados PENDING/PAID/COMPLETED/CANCELLED/DISPUTED) es correcto conceptualmente, pero la implementación tiene fallos de autorización que permiten robo directo de fondos, race conditions que permiten doble gasto/doble liberación, y uso de `Float` para dinero. Ninguno de estos son defectos menores: son explotables por cualquier usuario con acceso a la API.

---

## 1. Race Conditions en el motor Escrow

### 1.1 Patrón read-modify-write sin atomicidad (crítico)

Todos los endpoints de wallet siguen el mismo antipatrón:

```python
wallet = db.query(WalletDB).filter(...).first()   # 1. LEE
wallet.balance -= order.crypto_amount              # 2. MODIFICA en memoria
db.commit()                                         # 3. ESCRIBE
```

Entre el paso 1 y el paso 3 no hay ningún lock. Con SQLite en modo `check_same_thread=False` y, sobre todo, en Postgres con el nivel de aislamiento por defecto (`READ COMMITTED`), dos requests concurrentes a `POST /p2p/orders/` del mismo vendedor pueden leer el mismo `balance` antes de que ninguna haga commit, y ambas restan el monto: el vendedor termina vendiendo más de lo que tiene, o el balance queda negativo.

Lo mismo aplica a `release_escrow` (doble liberación de fondos si el endpoint se llama dos veces casi simultáneamente — replay accidental o intencional del comprador/vendedor) y a `cancel_order`.

**Fix recomendado — UPDATE atómico condicionado (compare-and-swap a nivel SQL), portable entre SQLite y Postgres:**

```python
from sqlalchemy import update

result = db.execute(
    update(WalletDB)
    .where(
        WalletDB.user_id == seller_id,
        WalletDB.currency == "USDT",
        WalletDB.balance >= order.crypto_amount,   # guard atómico
    )
    .values(
        balance=WalletDB.balance - order.crypto_amount,
        locked_balance=WalletDB.locked_balance + order.crypto_amount,
    )
)
if result.rowcount == 0:
    db.rollback()
    raise HTTPException(status_code=400, detail="Saldo insuficiente o billetera no encontrada")
db.commit()
```

Este patrón evita la ventana de carrera sin necesitar locks explícitos: el `WHERE balance >= amount` se evalúa atómicamente por el motor de la base de datos como parte del `UPDATE`.

**Para Postgres en producción**, añadir además locking pesimista explícito cuando hay múltiples pasos que dependen del mismo registro (p. ej. release, que toca wallet del vendedor Y del comprador):

```python
seller_wallet = (
    db.query(WalletDB)
    .filter(WalletDB.user_id == order.seller_id, WalletDB.currency == "USDT")
    .with_for_update()          # SELECT ... FOR UPDATE — bloquea la fila hasta el commit
    .first()
)
```

Recomendación: adquirir los locks **siempre en el mismo orden** (p. ej. por `wallet.id` ascendente) para evitar deadlocks cuando dos transacciones bloquean wallets de vendedor/comprador en orden inverso.

### 1.2 Check-then-act sobre el estado de la orden (crítico)

`mark_order_as_paid`, `release_escrow` y `cancel_order` hacen:

```python
if order.status != OrderStatus.PAID: raise HTTPException(...)
# ... trabajo con wallets ...
order.status = OrderStatus.COMPLETED
db.commit()
```

Dos llamadas concurrentes a `release_escrow` para la misma orden pueden pasar ambas el `if` antes de que la primera haga commit, liberando el escrow **dos veces**. Igual con `cancel_order` llamado dos veces en paralelo.

**Fix:** condicionar la transición de estado en el mismo `UPDATE` atómico, usando el estado actual como guard, y solo proceder con el movimiento de fondos si `rowcount == 1`:

```python
result = db.execute(
    update(P2POrderDB)
    .where(P2POrderDB.id == order_id, P2POrderDB.status == OrderStatus.PAID)
    .values(status=OrderStatus.COMPLETED)
)
if result.rowcount == 0:
    db.rollback()
    raise HTTPException(status_code=409, detail="La orden ya no está en estado 'paid' (posible liberación duplicada)")
# a partir de aquí, esta request es la única que puede mover los fondos
```

Esto convierte la transición de estado en el "lock lógico" de la orden — cualquier request concurrente pierde la carrera en el `UPDATE` y recibe `409 Conflict` en vez de duplicar el efecto.

### 1.3 Falta de transacción explícita con rollback

Ningún endpoint envuelve las múltiples escrituras (wallet vendedor + wallet comprador + orden) en un bloque `try/except` con `db.rollback()`. Si `db.commit()` falla a mitad de camino (o una excepción ocurre entre dos `db.add`/mutaciones), el estado queda inconsistente en memoria y potencialmente parcialmente escrito. Usar:

```python
try:
    # todas las mutaciones + un solo db.commit() al final
    db.commit()
except Exception:
    db.rollback()
    raise
```

o mejor, usar `with db.begin():` para que SQLAlchemy gestione el commit/rollback automáticamente.

### 1.4 Falta de índice único en `WalletDB(user_id, currency)`

No hay `UniqueConstraint`. En `release_escrow`, si dos requests concurrentes son la primera compra del mismo `buyer_id`, ambas pueden no encontrar wallet, y ambas hacer `db.add(WalletDB(...))`, creando dos filas duplicadas para el mismo usuario/moneda — balance "partido" en dos registros. **Fix:** `UniqueConstraint("user_id", "currency")` a nivel de esquema, y usar `INSERT ... ON CONFLICT DO NOTHING` / `get_or_create` atómico al crear el wallet del comprador.

---

## 2. Autenticación y Autorización

| # | Endpoint | Problema | Severidad |
|---|----------|----------|-----------|
| 2.1 | `mark_order_as_paid` | `buyer_id` viene como parámetro de query/body, no del JWT. Cualquiera puede marcar una orden como pagada suplantando a cualquier comprador. | 🔴 Crítico |
| 2.2 | `release_escrow` | `seller_id` viene como parámetro, no del JWT (solo se compara `order.seller_id != seller_id`, pero el atacante simplemente manda el `seller_id` correcto sin necesidad de estar autenticado como ese usuario). **No hay ningún `Depends(get_current_user)` en el endpoint** — no requiere token en absoluto. | 🔴 Crítico |
| 2.3 | `cancel_order` | Mismo problema: `user_id` como parámetro libre, sin verificar contra el JWT. | 🔴 Crítico |
| 2.4 | `create_p2p_order` | `seller_id` en el body — un atacante puede crear órdenes "como" otro usuario, bloqueando fondos ajenos (si además hay bug de balance, podría manipular el saldo de un tercero). | 🔴 Crítico |
| 2.5 | `get_current_user` | `db: Session = Depends(SessionLocal)` — `SessionLocal` es la fábrica de sesiones, no un dependency-provider; debería ser `Depends(get_db)`. Tal como está, **no compila/funciona**. | 🟠 Alto (bug funcional, pero enmascara que ningún endpoint protegido puede funcionar hoy) |
| 2.6 | `register_user` | Guarda `hashed_password=user.password` — **contraseña en texto plano en la base de datos.** | 🔴 Crítico |
| 2.7 | JWT `SECRET_KEY` | Hardcodeado en el código fuente (`"tu_clave_secreta_super_segura_para_chambeando"`) — cualquiera con acceso al repo puede forjar tokens válidos para cualquier usuario. | 🔴 Crítico |
| 2.8 | Ningún endpoint P2P usa `Depends(get_current_user)` | Todos los endpoints de negocio (`create_p2p_order`, `pay`, `release`, `cancel`) son de facto públicos y no autenticados; la identidad se toma de parámetros controlados por el cliente. | 🔴 Crítico |
| 2.9 | Sin chequeo anti self-trade | Nada impide que `buyer_id == order.seller_id` — un usuario podría "comprarse a sí mismo" (útil para lavado/farming de reputación si se agregan incentivos luego). | 🟡 Medio |
| 2.10 | Frontend guarda el JWT en `localStorage` | Expuesto a robo de token vía XSS. Recomendado: cookie `httpOnly` + `Secure` + `SameSite=Strict`, con protección CSRF si se usa cookie, o al menos CSP estricta si se mantiene `localStorage`. | 🟠 Alto |

**Fix estructural (aplica a 2.1–2.4, 2.8):** todos los endpoints de negocio deben tomar la identidad exclusivamente de `current_user: UserDB = Depends(get_current_user)`, nunca de un parámetro de request. Ejemplo para `release_escrow`:

```python
@app.post("/p2p/orders/{order_id}/release")
def release_escrow(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: UserDB = Depends(get_current_user),
):
    order = db.query(P2POrderDB).filter(P2POrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    if order.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="No tienes autorización para liberar esta orden")
    ...
```

Ver implementación completa corregida en `fixed_backend/`.

---

## 3. Modelado de datos y manejo de excepciones

- **`Float` para montos financieros (`crypto_amount`, `fiat_amount`, `balance`, `locked_balance`)**: `Float` es binario IEEE-754 y acumula error de redondeo (`0.1 + 0.2 != 0.3`). Para un exchange esto es inaceptable — un usuario podría explotar el redondeo acumulado en múltiples órdenes pequeñas para "fugar" fracciones de saldo, o simplemente el sistema puede desviar el libro contable. **Fix:** `Numeric(18, 8)` para cripto (8 decimales, estándar BTC/USDT) y `Numeric(12, 2)` para fiat, mapeados a `Decimal` en Python (nunca `float`) — incluyendo en los schemas Pydantic (`condecimal(gt=0, decimal_places=8)`).
- **Sin validación de montos positivos**: `OrderCreate` no valida `crypto_amount > 0` ni `fiat_amount > 0`. Un monto negativo o cero pasaría la validación de Pydantic y podría usarse para invertir el efecto de las operaciones de balance.
- **Sin restricción de `fiat_currency` / `payment_method`**: cadenas libres sin whitelist — abre la puerta a inconsistencia de datos y a inputs no sanitizados llegando a reportes/UI. Usar `Enum` (fiat: USD/CUP/MLC..., payment_method: catálogo cerrado).
- **Excepciones inconsistentes**: `release_escrow` devuelve `500` para un caso que en realidad es una violación de invariante de negocio (`locked_balance` insuficiente) — un `500` sugiere error del servidor, cuando en realidad indica corrupción de datos que debería alertar (log crítico + alerta), no solo devolverse al cliente como si fuera un error genérico. Se recomienda una jerarquía de excepciones propia (`InsufficientBalanceError`, `InvalidOrderStateError`, `OrderNotFoundError`) capturada por un exception handler centralizado de FastAPI (`@app.exception_handler`), que traduzca a códigos HTTP consistentes y loguee con contexto (order_id, user_id) para auditoría.
- **Sin ledger inmutable**: el sistema solo mantiene el estado actual del balance, no un historial de movimientos. Para un exchange, es indispensable una tabla `ledger_entries` (append-only, nunca se actualiza ni se borra) que registre cada movimiento (`order_id`, `wallet_id`, `delta`, `balance_after`, `reason`, `created_at`) — permite reconciliación, auditoría y detectar corrupción de balances por diferencia entre "balance actual" y "suma de movimientos".
- **`datetime.utcnow()` deprecado** en versiones recientes de Python/SQLAlchemy — usar `datetime.now(timezone.utc)`.
- **Sin manejo de expiración de órdenes PENDING**: el comentario en el modelo dice "esperando... o expirada" pero no hay ningún mecanismo (job en background / TTL) que cancele automáticamente órdenes viejas y libere el escrow.

---

## 4. Refactorización y estructura para producción

### 4.1 Estructura de archivos propuesta

```
chambeando/
├── app/
│   ├── main.py                  # solo crea la app FastAPI e incluye routers
│   ├── core/
│   │   ├── config.py             # Settings (pydantic-settings), lee de .env
│   │   ├── security.py           # hashing, JWT (create/verify)
│   │   └── logging.py
│   ├── db/
│   │   ├── session.py            # engine, SessionLocal, get_db
│   │   └── base.py                # Base declarativa
│   ├── models/
│   │   ├── user.py
│   │   ├── wallet.py
│   │   ├── order.py
│   │   └── ledger.py
│   ├── schemas/
│   │   ├── user.py
│   │   ├── order.py
│   │   └── token.py
│   ├── api/
│   │   ├── deps.py                # get_current_user, get_current_active_user, require_role
│   │   └── v1/
│   │       ├── auth.py
│   │       ├── users.py
│   │       ├── orders.py
│   │       └── wallets.py
│   └── services/
│       ├── escrow_service.py     # toda la lógica de negocio + operaciones atómicas
│       └── ledger_service.py
├── alembic/                       # migraciones versionadas (nunca create_all en prod)
├── tests/
│   ├── test_escrow_race_conditions.py   # tests con asyncio.gather / hilos concurrentes
│   ├── test_authorization.py
│   └── conftest.py
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

### 4.2 Checklist producción

- **Variables de entorno**: `SECRET_KEY`, `DATABASE_URL`, `ACCESS_TOKEN_EXPIRE_MINUTES`, etc. vía `pydantic-settings` (`BaseSettings`), nunca hardcodeadas. La app debe fallar al arrancar (`fail-fast`) si `SECRET_KEY` no está seteada o tiene el valor por defecto de ejemplo.
- **JWT**: rotar `SECRET_KEY` periódicamente (o migrar a `RS256` con clave privada/pública si hay múltiples servicios verificando tokens); access token de vida corta (15 min) + refresh token separado con revocación (tabla `refresh_tokens` o Redis); incluir `jti` para permitir invalidar tokens comprometidos.
- **Rate limiting**: `slowapi` (wrapper de `limits` para FastAPI) — límites agresivos en `/token` (fuerza bruta de login) y en `/p2p/orders/*` (evitar spam de órdenes/fondos).
- **CORS**: `CORSMiddleware` con whitelist explícita de orígenes (nunca `allow_origins=["*"]` en producción, sobre todo con `allow_credentials=True`).
- **Validaciones Pydantic**: montos con `condecimal(gt=0)`, longitudes máximas en strings, `EmailStr` para email, enums cerrados para moneda/método de pago.
- **Migraciones con Alembic**: `Base.metadata.create_all` es aceptable solo para desarrollo local; producción requiere migraciones versionadas y reversibles.
- **Logging estructurado + monitoreo**: registrar cada movimiento de fondos con `order_id`/`user_id`/`amount` (JSON logs), e integrar Sentry (o similar) para excepciones no controladas, especialmente las que hoy devuelven `500` por corrupción de balance.
- **Tests de concurrencia**: usar `pytest` + `ThreadPoolExecutor`/`asyncio.gather` para lanzar N requests simultáneos contra `release_escrow`/`create_p2p_order` y verificar que el balance nunca queda negativo ni se duplica una liberación — estos tests son los que realmente validan el fix de la sección 1.
- **Base de datos**: migrar de SQLite a Postgres antes de producción — SQLite no soporta locking a nivel de fila de forma robusta bajo concurrencia real.
- **Contenerización**: `Dockerfile` multi-stage + `docker-compose.yml` con Postgres, Redis (para rate limiting/blacklist de tokens) y la API.
- **CI**: lint (`ruff`), type-check (`mypy`), tests, y un job específico de "concurrency tests" antes de cada merge a main.

---

## 5. Resumen de prioridades (orden de remediación)

1. 🔴 Autenticación real en todos los endpoints de negocio (derivar identidad del JWT, nunca de parámetros) — sin esto, todo lo demás es irrelevante porque el atacante ni siquiera necesita explotar una race condition para robar fondos.
2. 🔴 Hash de contraseñas en `register_user`.
3. 🔴 `SECRET_KEY` desde variable de entorno.
4. 🔴 Operaciones atómicas (`UPDATE ... WHERE` condicionado) para balances y transición de estado de orden.
5. 🟠 Migrar `Float` → `Numeric`/`Decimal`.
6. 🟠 Ledger inmutable de auditoría.
7. 🟡 Estructura modular, rate limiting, Alembic, tests de concurrencia — antes de ir a producción, pero no bloquean el MVP funcional.

Ver implementación de referencia con los fixes 1–4 aplicados en `fixed_backend/`.

# Chambeando v2 — Escrow P2P no-custodial (cripto-only)

Pivote respecto al MVP original auditado en `security-audits/chambeando/`: Chambeando deja de tocar rieles fiat/bancarios por completo. Las transferencias son wallet-to-wallet en USDT-TRC20 (Tron), con el escrow viviendo en un smart contract, no en la base de datos de Chambeando. El pie fiat (comprador paga al vendedor por Zelle/transferencia/efectivo) ocurre fuera de la plataforma, entre las partes — igual que HeavenEx.

**Pendiente crítico sin resolver, fuera del alcance de este diseño técnico:** qué entidad legal opera Chambeando. Un exchange cripto custodial/no-custodial sigue siendo actividad de VASP regulada independientemente de que no toque bancos cubanos, y DELIVERYLINK LLC (entidad de EE.UU.) no puede ser la operadora si el mercado objetivo es Cuba, por CACR/OFAC. Ver la conversación de auditoría para el detalle — esto necesita abogado de sanciones antes de cualquier lanzamiento real, en paralelo a este trabajo técnico.

## Estructura

```
chambeando/
├── contracts/
│   └── EscrowP2P.sol       # escrow no-custodial — NO AUDITADO, no usar con fondos reales sin auditoría profesional
└── backend/
    ├── main.py              # FastAPI: auth por firma, metadata de órdenes, evidencia de disputas
    ├── auth.py               # login por firma de wallet (challenge-response), sin contraseñas
    ├── chain.py               # cliente de lectura del contrato (tronpy)
    ├── indexer.py              # worker separado: sincroniza eventos on-chain -> Postgres
    ├── models.py                # P2POrderDB.onchain_status lo escribe SOLO el indexer
    ├── config.py, database.py, schemas.py, deps.py
    └── requirements.txt
```

## Principios de diseño

1. **El contrato es la fuente de verdad de fondos.** El backend nunca firma transacciones de negocio (crear orden, reclamar, confirmar pago, liberar) — esas siempre las firma el usuario desde su propia wallet. El backend solo actúa como `arbiter` en disputas, y ese rol debe ser un multisig, nunca la misma clave que corre el servidor.
2. **`onchain_status` es de solo escritura por el indexer.** Ningún endpoint de la API lo modifica a partir de un request de usuario — así se elimina por diseño la clase de bug de autorización/race-condition encontrada en el audit del MVP original.
3. **Identidad = wallet, no contraseña.** Login por firma de un nonce de un solo uso (`/auth/nonce` → `/auth/verify`), sin passwords que hashear ni filtrar.

## Cómo correr localmente (testnet)

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # SECRET_KEY con valor real, ESCROW_CONTRACT_ADDRESS tras deployar en Shasta

# API
uvicorn main:app --reload --app-dir .

# Indexer (proceso separado, no lo levanta uvicorn)
python -m indexer
```

El contrato (`contracts/EscrowP2P.sol`) se compila y despliega con Hardhat/TronBox — no incluido aquí. Antes de deployar a mainnet: (1) auditoría de seguridad profesional, (2) pruebas de volumen real en Shasta testnet, (3) `owner`/`arbiter` configurados como multisig, nunca EOAs sueltas.

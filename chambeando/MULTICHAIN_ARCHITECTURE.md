# Arquitectura multichain

Este documento describe el refactor que separa Chambeando de estar diseñado
alrededor de una sola blockchain (TRON) y lo deja preparado para soportar
USDT sobre varias redes. **Nada de lo descrito aquí activa fondos reales,
despliega contratos, ni cambia qué red usa producción hoy** (sigue siendo
TRON, vía `CHAIN_ADAPTER=tron`) — ver "Qué NO cambió" al final.

## Network Registry (`backend/chain/networks.py`)

Fuente de verdad central y tipada de "qué redes existen y en qué estado
operacional está cada una". Cada red es un `NetworkConfig` (dataclass
congelado) con, entre otros: `network_id`, `chain_family`, `chain_id`,
`usdt_asset_address` / `usdt_decimals` / `usdt_origin`, `rpc_url`,
`escrow_contract_address` / `_abi_path`, `status`, y una clasificación
obligatoria de procedencia (`source_status` + `source_reference`):

| `source_status` | Significa |
|---|---|
| `VERIFIED_FACT` | Verificado de forma independiente esta sesión (lectura on-chain directa via RPC público, y/o https://tether.to/en/supported-protocols/ como fuente primaria del emisor) |
| `UNKNOWN` | Entrada estructural, ningún campo no-trivial verificado |

**Ya no existen `PROPOSED`/`INFERENCE`** (revisión humana del 2026-09-25):
ningún campo operacional queda en un estado intermedio "probablemente
correcto pero no verificado" — o se verificó de forma independiente (RPC
on-chain, o la página oficial de Tether) y queda `VERIFIED_FACT` con el
campo poblado, o no se verificó y el campo queda `None`. La evidencia cruda
(URLs, llamadas RPC, respuestas) detrás de cada valor no-`None` vive en el
`source_reference` de esa entrada, en `networks.py` — no en un archivo
separado.

**Regla dura:** ninguna dirección de contrato, RPC, explorer, chain_id o
decimals se inventó. Donde no había evidencia verificable, el campo quedó
`None`. Ver `get_network()`/`list_networks()` para consultarlo.

### SUPPORTED ≠ ACTIVE

Cada red tiene un `NetworkStatus`: `DISABLED` (existe en el registry, no se
puede usar operacionalmente, PODRÍA activarse a futuro) → `TESTNET` → `PILOT`
→ `ACTIVE`. `NetworkConfig` hace **imposible construir en memoria** una red
`ACTIVE` sin `usdt_asset_address`, `usdt_decimals`, `rpc_url`,
`escrow_contract_address` y `escrow_contract_abi_path` (y, si es EVM,
`chain_id`) — `__post_init__` lanza `ValueError` si falta alguno.
`get_chain_adapter(network_id)`/`build_adapter_for_network()` además
rechazan en runtime cualquier red `DISABLED`.

Existe además `NetworkStatus.HISTORICAL`, distinto de `DISABLED`: `DISABLED`
significa "todavía no, pero podría activarse"; `HISTORICAL` significa "el
propio Tether dejó de emitir/redimir aquí, esto no se va a activar nunca"
(Algorand, Vaulta/EOS, Kusama AssetHub, Omni, BCH-SLP — ver "Deprecated Asset
Protocols" en la fuente oficial). Ninguna red `HISTORICAL` puede volverse
operacional — el gate de `build_adapter_for_network` rechaza `DISABLED` y
`HISTORICAL` por igual.

### Estado sembrado hoy (19 redes — todo lo que tether.to/en/supported-protocols lista hoy, más 5 históricas)

| Red | `chain_family` | `status` | `usdt_origin` | `usdt_asset_address` |
|---|---|---|---|---|
| `tron` | TRON | `TESTNET` | `TETHER_NATIVE` | `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` |
| `ethereum` | EVM | `DISABLED` | `TETHER_NATIVE` | `0xdAC17F958D2ee523a2206206994597C13D831Ec7` |
| `avalanche` | EVM | `DISABLED` | `TETHER_NATIVE` | `0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7` |
| `bsc` | EVM | `DISABLED` | **`BINANCE_PEG`** | `0x55d398326f99059fF775485246999027B3197955` (ver nota abajo) |
| `celo` | EVM | `DISABLED` | `TETHER_NATIVE` | `0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e` |
| `kava` | EVM | `DISABLED` | `TETHER_NATIVE` | `0x919C1c267BC06a7039e03fcc2eF738525769109c` |
| `kaia` | EVM | `DISABLED` | `TETHER_NATIVE` | `0xd077a400968890eacc75cdc901f0356c943e4fdb` |
| `liquid`, `solana`, `polkadot_assethub`, `tezos`, `near`, `ton`, `aptos` | familia propia, sin adapter | `DISABLED` | `TETHER_NATIVE` | verificado desde tether.to, ver `networks.py` |
| `algorand`, `vaulta`, `kusama_assethub`, `omni`, `bch_slp` | familia propia, sin adapter | **`HISTORICAL`** | `UNKNOWN` | `None` (identificador legacy documentado en `notes`, no cargado como campo operacional) |

TRON queda en `TESTNET`, no `ACTIVE`: `TRON_NODE_URL` por defecto apunta a
Shasta (testnet) y `ESCROW_CONTRACT_ADDRESS` está vacío en `.env.example` —
no hay evidencia de un deploy productivo, aunque la dirección de USDT-TRON ya
está verificada (ver tabla).

**BSC — clasificación corregida (revisión humana):** `tether.to/en/supported-protocols`
**no lista ninguna dirección USD₮ para BSC** (solo XAU₮, oro tokenizado) —
verificado explícitamente en esta sesión, no es una omisión de este
registry. El token ampliamente usado como "USDT" en BNB Smart Chain
(`0x55d398...`) es un puente emitido por **Binance**, no por Tether:
CoinGecko lo cataloga como "Binance Bridged USDT (BNB Smart Chain)" / ticker
`BSC-USD`, categoría "Bridged-Tokens". `usdt_decimals=18` (no 6, a diferencia
de todas las demás entradas) confirmado leyendo `decimals()` directamente
on-chain contra el contrato real. Por eso `usdt_origin=BINANCE_PEG`, nunca
`TETHER_NATIVE`, para esta red.

`chain_id`/`native_gas_token`/`explorer_url` de todas las redes EVM con
adapter (`ethereum`/`avalanche`/`bsc`/`celo`/`kava`/`kaia`) se verificaron
**on-chain** (llamada RPC real `eth_chainId` contra un nodo público) y
cruzados contra chainid.network — ya no son `PROPOSED`, son `VERIFIED_FACT`.
`rpc_url` se deja `None` en la `NetworkConfig` real mientras el `status` sea
`DISABLED` (el endpoint público verificado que se usó para la comprobación
queda documentado en `notes`, no se "activa" poblando el campo).

## Chain Families y Adapter Registry

```
EscrowChainAdapter (backend/chain/adapter.py)      <- interfaz, sin cambios
        ├── TronEscrowAdapter (tron_adapter.py)     <- preservado intacto
        └── EvmEscrowAdapter (evm_adapter.py)        <- nuevo, sirve a TODAS las redes EVM
```

`EvmEscrowAdapter` es **una sola clase para BSC/Ethereum/Avalanche/Celo/...**
— lo que cambia entre redes es el `NetworkConfig` que se le inyecta (RPC,
contrato, chain_id), nunca el código. Agregar una EVM nueva verificada es
agregar una entrada al registry, no un archivo nuevo.

`backend/chain/__init__.py` expone `get_chain_adapter(network_id=None)` con
dos rutas que conviven a propósito:

- **Sin argumento** (`get_chain_adapter()`): ruta LEGADO, exactamente el
  mismo comportamiento que antes de este refactor — un único adaptador
  global según `settings.CHAIN_ADAPTER`. `auth.py`/`routers/auth.py` la
  siguen llamando así hoy; no se tocaron (ver "Qué NO cambió").
- **Con `network_id`**: ruta nueva, resuelve por `ChainFamily` vía
  `build_adapter_for_network()` y falla cerrado si la red no existe o está
  `DISABLED`.

Agregar una familia de cadena no-EVM futura (Solana, TON, Aptos, NEAR,
Polkadot AssetHub, Tezos, ...) es: un `xxx_adapter.py` nuevo que implemente
`EscrowChainAdapter`, un valor nuevo en `ChainFamily`, y un branch en
`build_adapter_for_network()` — la lógica de negocio (auth, indexer, routers)
no cambia, porque solo conoce la interfaz.

## Aislamiento por librería de cadena

Misma regla para `web3`/`eth_account` que ya regía para `tronpy`:
**`evm_adapter.py` es el único archivo autorizado a importarlos**, y lo hace
de forma perezosa (`from web3 import Web3` dentro de `__init__`, nunca a
nivel de módulo) — así el resto del backend puede importar `chain.evm_adapter`
sin tener `web3` instalado; solo construir un adaptador real contra un RPC en
vivo lo requiere. `tests/test_chain_separation.py` hace cumplir esto
estáticamente hoy solo para `tronpy` (ningún network EVM está `ACTIVE`
todavía); extenderla a `web3` es trabajo natural de cuando `bsc` dejé de
estar `DISABLED`.

## Identidad de token y de wallet

- **Un contrato = un token permitido, fijado en el deploy** (`allowedToken`,
  `immutable`) — igual en TRON y en EVM. `usdt_asset_address` vive en el
  `NetworkConfig` de esa red, nunca hardcodeado en el contrato ni en el
  adaptador.
- **Ownership de wallet = firma, nunca clave privada.** El flujo
  `nonce → mensaje → firma → backend verifica` (`auth.py`) no cambió. Lo
  nuevo es que `EscrowChainAdapter.verify_wallet_signature` ahora tiene dos
  implementaciones intercambiables:
  - TRON: `tron_adapter.verify_tron_signature` (sin cambios).
  - EVM: `evm_adapter.verify_evm_signature` — recupera el firmante de un
    mensaje EIP-191 (`personal_sign`) vía `eth_account` y lo compara con la
    dirección esperada (checksummed). Nunca pide ni ve una clave privada.
- **WhatsApp identity ≠ wallet identity ≠ membership** sigue siendo verdad
  sin cambios — ver Fase 8 abajo.

## Escrow deployment per-network

Cada `NetworkConfig` tiene su propio `escrow_contract_address` +
`escrow_contract_abi_path` — dos redes nunca comparten contrato ni ABI. El
contrato EVM vive en `contracts/evm/EscrowP2P.sol` (mocks en
`contracts/evm/mocks/`), puerto de `contracts/EscrowP2P.sol` (TRON, **sin
tocar**) con las mismas garantías de seguridad: máquina de estados OPEN→
CLAIMED→PAID→{DISPUTED→RELEASED/REFUNDED}/CANCELLED, snapshots de fee y de
arbiter, modelo de disputa 2-de-3, `nonReentrant`, fondeo exacto,
single-token-per-deployment. Diferencias documentadas explícitamente en el
header de `contracts/evm/EscrowP2P.sol`:

1. `@openzeppelin/contracts` estándar en vez de `@openzeppelin/tron-contracts`.
2. `SafeERC20.safeTransfer` estándar reemplaza a `SafeTRC20.safeTransferChecked`
   — el problema que esa extensión TRON-only resuelve (un `transfer()` que
   devuelve `false` incluso en éxito, como USDT-TRON) ya lo maneja de fábrica
   el `SafeERC20` estándar de OpenZeppelin para EVM.
3. `owner`/`arbiter` deberían ser una wallet multi-firma EVM (p.ej. Gnosis
   Safe) — equivalente EVM al Account Permission Management (TIP-16) de TRON.

`test/EscrowP2P.evm.test.js` porta las mismas garantías críticas que
`test/EscrowP2P.test.js` (TRON) — estado, autorización, límite de autoridad
del arbiter, fees, poderes de admin, reentrancy, fondeo exacto, replay — la
única sección que no se porta es "USDT-TRON transfer() return-value quirk",
que no aplica a un ERC-20 estándar en EVM (ver diferencia #2 arriba).

## Security gates

1. `NetworkConfig.__post_init__` — `ACTIVE` exige configuración completa,
   en memoria, sin tocar red.
2. `build_adapter_for_network()` — rechaza cualquier red `DISABLED` o
   `HISTORICAL` **dentro del builder mismo**, no solo en el caller
   (`get_chain_adapter`) — así ningún caller presente o futuro puede
   saltarse la protección llamando al builder directamente. Ver
   `test_direct_builder_rejects_disabled_network`.
3. `get_network(network_id)` — `network_id` desconocido falla cerrado
   (`ValueError`), nunca devuelve un default silencioso.
4. `EvmEscrowAdapter.assert_chain_id_matches()` — antes de devolver un
   adapter EVM operacional, `build_adapter_for_network` compara el
   `chain_id` real que reporta el RPC configurado contra
   `NetworkConfig.chain_id`; un mismatch (p.ej. una `NetworkConfig` de "bsc"
   apuntando por error a un RPC de Ethereum) falla cerrado, nunca opera
   silenciosamente contra la red equivocada. **`chain_id is None` TAMBIÉN
   falla cerrado** (corregido en revisión humana del 2026-09-25 — la
   versión anterior trataba "sin chain_id" como "nada que comparar" y
   dejaba pasar; ahora una red EVM operacional sin `chain_id` verificado
   nunca puede llegar a construir un adapter). Las redes `DISABLED` no se
   ven afectadas: nunca llegan a este método, el gate #2 las detiene antes.
5. Ningún dato no verificado (dirección de contrato/USDT, RPC, explorer,
   chain_id, decimals) se inventó — ver tabla de `source_status` arriba.
6. Aislamiento de librería de cadena (`tronpy`/`web3`) por archivo,
   verificado estáticamente para `tronpy` en `test_chain_separation.py`.
7. **Wallet-link ligado server-side a un network_id** (ver sección
   "Wallet-link multichain" abajo) — el `network_id` de un deep link nunca
   es un query param libre. `issue_link_token`/`consume_link_token`
   (`messaging/identity.py`) usan la **misma** constante
   `NEVER_OPERATIONAL_STATUSES` (`chain/networks.py`) que el gate #2, en vez
   de repetir su propio criterio — corregido en la misma revisión, después
   de que una copia local del chequeo (`status == DISABLED`) dejara pasar
   `HISTORICAL` por no haberse actualizado cuando ese estado se agregó.

## Cómo agregar una red nueva

**EVM ya soportada por la familia (ej. activar BSC de verdad):**
1. Verificar `usdt_asset_address` real contra una fuente autoritativa
   (Tether, el explorer oficial de esa red) — nunca copiarla de memoria/blog.
2. Completar el `NetworkConfig` de esa red en `networks.py` (rpc_url real,
   `escrow_contract_address` tras un deploy real y auditado, etc.).
3. Subir `status` progresivamente: `DISABLED` → `TESTNET` (deploy en testnet,
   validación) → `PILOT` → `ACTIVE`. Cada salto es una decisión humana
   explícita, nunca automática.
4. `EvmEscrowAdapter` no cambia — ya sirve a cualquier red EVM.

**Familia no-EVM nueva (Solana, TON, ...):**
1. Nuevo valor en `ChainFamily`.
2. Nuevo `xxx_adapter.py` implementando `EscrowChainAdapter`.
3. Branch nuevo en `build_adapter_for_network()`.
4. Nuevo contrato de escrow para esa VM, si aplica (puede que ni sea EVM/Solidity).
5. La capa de negocio (auth, indexer, routers, models) no cambia.

## Wallet-link multichain (backend, sin frontend)

`WhatsAppLinkDB.link_token_network_id` (migración `8834a2893229`) fija el
`network_id` de un token de wallet-link **al emitirlo**
(`issue_link_token`), no al consumirlo. `consume_link_token` (y el endpoint
HTTP `POST /whatsapp/link`, cuyo `WhatsAppLinkRequest.network_id` ahora es
obligatorio) rechazan cualquier `network_id` que no coincida exactamente con
el que se guardó al emitir — un `&network=` en la URL del deep link nunca es
la fuente de verdad por sí solo, es simplemente lo que el backend YA decidió
y el frontend repite de vuelta. `router.py`'s `_handle_join` pasa
`network_id=settings.CHAIN_ADAPTER` (hoy siempre `"tron"`) tanto al emitir
el token como al construir la URL, para que ambos coincidan siempre.
`issue_link_token` también falla cerrado si el `network_id` no existe o está
`DISABLED`/`HISTORICAL` — nunca emite un token para una red que no puede
operar. El mensaje de firma EVM (`verify_evm_signature`) sigue demostrando
únicamente *ownership de la wallet* — la elección de red viene del link, no
de nada que el firmante declare en el mensaje.

## Qué sigue UNKNOWN

- `explorer_url` de las redes fuera de `ethereum`/`avalanche`/`bsc`/`celo`/`kava`/`kaia`/`tron`.
- `rpc_url` real (no testnet) de cualquier red EVM (el campo real de
  `NetworkConfig` se deja `None` mientras el `status` sea `DISABLED`, aunque
  se haya verificado un endpoint público funcional — ver `notes`).
- `wallet_connectors` reales del frontend (no existe integración de
  wallet-connector verificada en este repo todavía).
- `usdt_decimals` de las 7 redes sin `EscrowChainAdapter` implementado
  (liquid/solana/polkadot_assethub/tezos/near/ton/aptos) — solo se verificó
  `usdt_asset_address` (desde tether.to), no se verificó decimals para estas
  (fuera de alcance: son entradas representacionales, ningún adapter las usa).

## Límites pendientes (sin resolver, no ocultados)

- **`web3` (Python) no validado en runtime en esta máquina de desarrollo** —
  `eth-account` (firma/verificación) sí está instalado y probado; el resto
  de `EvmEscrowAdapter` (llamadas RPC reales vía `web3.Web3`) nunca se
  ejecutó de punta a punta contra un nodo real en ninguna sesión de este
  refactor (bloqueo: `lru-dict`, dependencia transitiva de `web3`, necesita
  un compilador C/MSVC que esta máquina Windows no tiene).
- **Ningún RPC EVM real fue usado para *operar* un adapter** — los RPCs
  públicos que se usaron para verificar `chain_id`/`usdt_decimals` (sección
  "Estado sembrado hoy") fueron llamadas puntuales de solo lectura para
  poblar el registry, no una prueba de `EvmEscrowAdapter` funcionando de
  punta a punta contra un nodo real.
- **`contracts/evm/EscrowP2P.sol` no tiene auditoría de seguridad externa**
  — mismo estado que `contracts/EscrowP2P.sol` (TRON), ver el header de
  cada contrato.
- **Cumplimiento legal pendiente** — ver "Pendiente crítico" en `README.md`
  (qué entidad puede operar esto legalmente sigue sin resolver, fuera del
  alcance técnico de este refactor).

## Qué NO cambió (fuera de alcance de esta fase, a propósito)

- **Producción sigue en TRON.** `settings.CHAIN_ADAPTER` sigue por defecto en
  `"tron"`, y `auth.py`/`routers/auth.py` siguen llamando
  `get_chain_adapter()` sin argumento — el refactor es aditivo, no reemplaza
  la ruta legado.
- **`wallet_handoff_enabled` sigue `False` cuando `WHATSAPP_PROVIDER=meta`.**
  Esa protección no se tocó.
- **Ningún frontend de wallet-signing/wallet-connect existe todavía** — el
  binding server-side de network_id (sección anterior) prepara el backend
  para cuando exista, pero no lo construye.
- **Ningún deploy, ninguna transacción, ningún fondo real.** Ver el reporte
  final de esta fase para la lista completa de garantías (`MAINNET_DEPLOYED
  = NO`, etc.).

"""
Gate SEPARADO y EXPLICITO de `NetworkStatus` (chain/networks.py) para redes
admitidas para AUTENTICACION (probar firma de wallet) unicamente.

Correccion humana (revision 2026-09-25): `NetworkStatus.TESTNET` controla
CAPACIDAD OPERACIONAL (poder construir un EscrowChainAdapter real contra un
nodo/contrato -- ver build_adapter_for_network). No debe reutilizarse como
permiso de login: una red podria ser valida para login sin que eso implique
nada sobre su capacidad de operar ordenes/escrow, y viceversa. Por eso este
modulo es una pregunta completamente distinta de la de `networks.py`:
"¿esta red puede usarse para PROBAR POSESION DE UNA WALLET?" -- nunca
"¿esta red puede operar escrow?".

Resolver una red aqui NUNCA construye un EscrowChainAdapter real (nunca
EvmEscrowAdapter/TronEscrowAdapter, nunca un RPC, nunca un contrato) --
solo identifica que ChainFamily usar para elegir la funcion PURA de
verificacion de firma correcta (verify_tron_signature / verify_evm_signature,
ver chain/__init__.py:verify_wallet_signature_for_auth), exactamente igual
que build_adapter_for_network usa ChainFamily para elegir la clase de
adapter -- pero sin construir nada con estado de red.

SEGUNDA correccion humana, mas importante (revision 2026-09-25, hallazgo de
revision posterior): el identificador "evm" (nunca "bsc" ni ningun otro
nombre de cadena especifica) es DELIBERADO, no un detalle cosmetico.

`personal_sign` (EIP-191) firma un MENSAJE, no una transaccion -- el hash
que se firma es `keccak256("\x19Ethereum Signed Message:\n" + len +
mensaje)`, que NO INCLUYE NINGUN campo de chainId en ninguna parte. La
MISMA clave privada produce la MISMA firma valida sobre el MISMO texto de
mensaje sin importar si la wallet tiene seleccionada Ethereum, BSC,
Polygon, Avalanche o cualquier otra red EVM -- `eth_requestAccounts` +
`personal_sign` prueban control de UNA DIRECCION/CLAVE con formato EVM
(curva secp256k1, direccion derivada via keccak256), nunca "esta wallet
opera en la cadena X". Llamar a esto "bsc" habria sido una afirmacion falsa
de lo que realmente se verifico -- ver EVM_AUTH_ONLY_LIMITATIONS mas abajo
para el detalle completo, incluido que haria falta (EIP-712 con chainId en
el domain separator) si algun dia se necesitara probar una cadena EVM
especifica, y por que ni siquiera eso demuestra fondos o capacidad
operativa.

Lista blanca deliberadamente chica: agregar una red aqui NUNCA habilita
ordenes/escrow/USDT para ella (eso sigue siendo, exclusivamente,
NetworkStatus != DISABLED/HISTORICAL en chain/networks.py, mas un
EscrowChainAdapter real construido y probado -- dos gates independientes que
deben pasar los dos, cada uno por su propia razon). "tron" es la red legado,
siempre disponible (mismo comportamiento que antes de esta fase). "evm" se
agrega aqui SOLO para probar el flujo de autenticacion/vinculo de WhatsApp
en entorno local con una wallet EVM (Trust Wallet u otra) -- ninguna entrada
NetworkStatus de chain/networks.py (bsc, ethereum, avalanche, ...) se
activa ni se referencia desde aca a proposito: "evm" no es una cadena real,
es una familia de firma."""

from __future__ import annotations

from .networks import ChainFamily

EVM_AUTH_ONLY_LIMITATIONS = """
Que SI prueba el flujo "evm" de autenticacion:
  - Que quien completo el flujo controla la clave privada correspondiente a
    la direccion EVM entregada (firma ECDSA/secp256k1 valida, recuperable a
    esa direccion exacta, sobre el nonce exacto emitido por el servidor).

Que NO prueba (a proposito, porque personal_sign no puede probarlo):
  - Que la wallet tenga seleccionada BSC (ni ninguna otra red EVM
    especifica) al momento de firmar -- la firma es identica sin importar
    la red seleccionada en la UI de la wallet.
  - Que la direccion tenga fondos en NINGUNA red.
  - Que la direccion pueda operar (enviar una transaccion, pagar gas) en
    BSC ni en ninguna otra cadena EVM especifica.
  - Un chainId de ningun tipo: EIP-191 personal_sign no incluye chainId en
    lo que se firma; comprobar `eth_chainId` del lado del NAVEGADOR no es
    un control de seguridad del SERVIDOR -- es un valor que el cliente
    reporta y que un cliente malicioso o con un bug puede simplemente no
    reportar honestamente, sin que eso afecte la validez de la firma.

Si en el futuro se necesitara probar criptograficamente una cadena EVM
ESPECIFICA (p.ej. "esta firma es valida SOLO para BSC, chainId=56"), la via
correcta es EIP-712 (typed data), firmando una estructura cuyo domain
separator incluya `chainId: 56` explicitamente -- eso hace que el HASH
firmado sea distinto para cada chainId, asi que una firma producida para
chainId=1 nunca recupera la misma direccion si el servidor la verifica
esperando chainId=56. Eso SI es evidencia criptografica firmada de una
declaracion de cadena. Incluso asi, esa firma seguiria sin probar fondos ni
capacidad operativa real en esa cadena -- solo que la wallet firmante
aprobo conscientemente una declaracion que incluia ese chainId. No
implementado en esta fase porque el requisito real declarado es
unicamente autenticacion (login), no verificacion de capacidad BSC.
"""


class AuthNetworkError(ValueError):
    """Red desconocida o no admitida para autenticacion -- falla cerrado."""


# Whitelist explicita, con su propia ChainFamily -- NO se deriva de
# chain/networks.py (a proposito: "evm" no es una entrada de ese registry,
# es una familia de firma, no una cadena especifica -- ver docstring del
# modulo). "tron" SI corresponde 1:1 con la red operacional homonima (TRON
# no tiene el problema de EVM: TIP-191 tampoco liga a un chain especifico,
# pero TRON en si es una unica red, no una familia con multiples cadenas).
AUTH_ONLY_NETWORKS: dict[str, ChainFamily] = {
    "tron": ChainFamily.TRON,
    "evm": ChainFamily.EVM,
}

AUTH_ONLY_NETWORK_IDS: frozenset[str] = frozenset(AUTH_ONLY_NETWORKS)


def is_auth_only_network(network_id: str) -> bool:
    return network_id in AUTH_ONLY_NETWORKS


def get_auth_network_chain_family(network_id: str) -> ChainFamily:
    """Falla cerrado: un network_id fuera de la whitelist nunca resuelve una
    ChainFamily -- nunca se usa un default silencioso. No construye ningun
    adapter ni toca red/contrato ni el registry operacional de
    chain/networks.py."""
    if network_id not in AUTH_ONLY_NETWORKS:
        raise AuthNetworkError(f"network_id {network_id!r} no esta habilitada para autenticacion")
    return AUTH_ONLY_NETWORKS[network_id]

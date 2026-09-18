// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/*
 * EscrowP2P — escrow no-custodial para el mercado P2P de Chambeando.
 *
 * IMPORTANTE ANTES DE MAINNET:
 *  - Este contrato NO ha sido auditado por una firma de seguridad externa.
 *    No debe manejar fondos reales de terceros sin pasar por una auditoría
 *    profesional y un periodo de pruebas en testnet (Shasta) con volumen real.
 *  - `owner` debe ser una cuenta TRON con Account Permission Management
 *    (TIP-16, multi-firma nativa por peso/umbral) — NUNCA una sola EOA. Esto
 *    es un requisito OPERACIONAL de despliegue, no algo que este contrato
 *    pueda forzar en Solidity.
 *  - `arbiter` debería, igualmente, ser una cuenta TRON multi-firma nativa —
 *    pero eso NO sustituye el modelo 2-de-3 a nivel de contrato implementado
 *    abajo (ver `voteDisputeResolution`). Son dos capas independientes:
 *    (1) quién controla la clave que actúa como "arbiter" (multisig de
 *    cuenta, fuera de este contrato), y (2) qué puede hacer esa clave sola
 *    una vez que actúa (nunca puede resolver una disputa sin el voto de
 *    comprador o vendedor — eso sí lo garantiza este contrato).
 *  - Usa `@openzeppelin/tron-contracts` (Ownable, ReentrancyGuard, SafeTRC20)
 *    en vez de implementaciones propias: son la librería oficial de
 *    OpenZeppelin adaptada a las peculiaridades de TRC-20/TVM, en particular
 *    el comportamiento de USDT-TRON (ver nota en SafeTRC20.safeTransferChecked).
 */

import {ITRC20} from "@openzeppelin/tron-contracts/token/TRC20/ITRC20.sol";
import {SafeTRC20} from "@openzeppelin/tron-contracts/token/TRC20/utils/SafeTRC20.sol";
import {Ownable} from "@openzeppelin/tron-contracts/access/Ownable.sol";
import {ReentrancyGuard} from "@openzeppelin/tron-contracts/utils/ReentrancyGuard.sol";

contract EscrowP2P is Ownable, ReentrancyGuard {
    using SafeTRC20 for ITRC20;

    /*
     * OPEN cubre lo que en el diseño conceptual serían dos estados
     * separados, "OPEN" y "FUNDED": en este contrato el depósito del USDT
     * ocurre de forma atómica dentro de `createOrder`, así que nunca existe
     * una orden "abierta pero sin fondear" — fondear y listar son la misma
     * transacción. Se conserva un único estado OPEN==FUNDED a propósito:
     * separar ambos no aportaría nada (no hay ventana en la que uno exista
     * sin el otro) y sí añadiría una transición de estado más para auditar.
     *
     * RELEASED y REFUNDED se escriben AMBOS explícitamente on-chain — nunca
     * se infieren fuera de la cadena a partir del `recipient` de un evento.
     */
    enum OrderStatus {
        OPEN, // == FUNDED: fondos depositados, esperando comprador
        CLAIMED, // == MATCHED: comprador asignado, esperando confirmacion de pago fiat
        PAID, // == FIAT_MARKED_PAID: comprador afirmo el pago fiat (off-chain, no verificable on-chain)
        DISPUTED, // en disputa — solo alcanzable desde PAID, via raiseDispute()
        RELEASED, // fondos liberados al comprador — escrito explicitamente, nunca inferido
        REFUNDED, // fondos devueltos al vendedor — escrito explicitamente, nunca inferido
        CANCELLED // orden cancelada por el vendedor antes de tener comprador, fondos devueltos
    }

    struct Order {
        address seller;
        address buyer;
        address token;
        uint256 amount; // EXACTAMENTE el monto solicitado — ver require de fondeo exacto en createOrder()
        uint64 createdAt;
        uint64 claimedAt;
        uint64 paidAt;
        uint16 feeBpsSnapshot; // fee vigente AL CREAR la orden — inmutable para esa orden desde entonces
        address feeCollectorSnapshot; // destinatario del fee vigente AL CREAR la orden — igualmente inmutable
        // arbiter vigente AL MATCHEAR la orden (claimOrder), NO al abrir la disputa — inmutable desde el
        // match en adelante. Fijarlo aqui (y no en raiseDispute) cierra la ventana en la que `owner` podia
        // cambiar `arbiter` despues de que comprador y vendedor ya estaban emparejados pero antes de que
        // existiera disputa, sustituyendo el arbitro de un trade ya en curso sin que ninguna de las partes
        // lo supiera.
        address arbiterSnapshot;
        OrderStatus status;
    }

    /// @notice tiempo maximo que un comprador tiene para confirmar pago tras reclamar una orden
    uint64 public constant CLAIM_TIMEOUT = 2 hours;

    /// @notice fee maximo permitido, en basis points (100 = 1.00%) — limite duro, no se puede subir por encima
    uint16 public constant MAX_FEE_BPS = 100;

    /// @notice unico token TRC-20 aceptado por esta instancia del contrato — fijado en el deploy, nunca modificable
    address public immutable allowedToken;

    mapping(uint256 => Order) public orders;
    uint256 public nextOrderId;

    /// @notice cuenta que actua como tercer voto en disputas — ver modelo 2-de-3 en voteDisputeResolution
    address public arbiter;
    address public feeCollector;
    uint16 public feeBps; // p.ej. 25 = 0.25%. Para beta/V1, desplegar con 0.

    /// @dev orderId => voter => recipient por el que ese voter voto (address(0) = sin voto)
    mapping(uint256 => mapping(address => address)) private _disputeVoteOf;

    event OrderCreated(uint256 indexed orderId, address indexed seller, address indexed token, uint256 amount, uint16 feeBpsSnapshot);
    event OrderClaimed(uint256 indexed orderId, address indexed buyer, address arbiterSnapshot);
    event ClaimExpired(uint256 indexed orderId);
    event PaidConfirmed(uint256 indexed orderId, address indexed buyer);
    event OrderCancelled(uint256 indexed orderId);
    event DisputeRaised(uint256 indexed orderId, address indexed raisedBy, address indexed lockedArbiter);
    event DisputeVoteCast(uint256 indexed orderId, address indexed voter, address indexed recipientVotedFor);
    event OrderSettled(uint256 indexed orderId, address indexed recipient, uint256 payout, uint256 fee, OrderStatus finalStatus);
    event ArbiterUpdated(address indexed newArbiter);
    event FeeUpdated(uint16 newFeeBps, address newFeeCollector);

    constructor(
        address initialOwner,
        address initialArbiter,
        address initialFeeCollector,
        uint16 initialFeeBps,
        address token_
    ) Ownable(initialOwner) {
        require(initialArbiter != address(0), "arbiter=0");
        require(initialFeeCollector != address(0), "feeCollector=0");
        require(initialFeeBps <= MAX_FEE_BPS, "fee too high");
        require(token_ != address(0), "token=0");
        arbiter = initialArbiter;
        feeCollector = initialFeeCollector;
        feeBps = initialFeeBps;
        allowedToken = token_;
    }

    // ---------------------------------------------------------------------
    // Camino normal: firmado siempre por el propio usuario (vendedor/comprador)
    // ---------------------------------------------------------------------

    /// @dev Fondeo exacto: una orden solo puede registrarse como fondeada si el escrow recibio EXACTAMENTE
    /// `amount`, medido por el delta de balance propio antes/despues de la transferencia — nunca por el
    /// valor de retorno del token (ver nota de USDT-TRON en el header). A proposito esto YA NO acepta
    /// tokens fee-on-transfer/deflacionarios que entreguen menos de lo solicitado: para V1, con un unico
    /// token permitido (USDT-TRON, que no cobra fee de transferencia), aceptar solo el monto exacto es mas
    /// seguro que adaptar silenciosamente `amount` a lo que sea que haya llegado.
    function createOrder(uint256 amount) external nonReentrant returns (uint256 orderId) {
        require(amount > 0, "amount=0");
        require(msg.sender != arbiter, "seller cannot be arbiter");

        ITRC20 token = ITRC20(allowedToken);
        uint256 balanceBefore = token.balanceOf(address(this));
        token.safeTransferFrom(msg.sender, address(this), amount);
        uint256 balanceAfter = token.balanceOf(address(this));

        require(balanceAfter >= balanceBefore, "balance decreased unexpectedly");
        require(balanceAfter - balanceBefore == amount, "short transfer: exact funding required");

        uint16 feeSnapshot = feeBps;
        address feeCollectorSnap = feeCollector;

        orderId = nextOrderId++;
        orders[orderId] = Order({
            seller: msg.sender,
            buyer: address(0),
            token: allowedToken,
            amount: amount,
            createdAt: uint64(block.timestamp),
            claimedAt: 0,
            paidAt: 0,
            feeBpsSnapshot: feeSnapshot,
            feeCollectorSnapshot: feeCollectorSnap,
            arbiterSnapshot: address(0), // se fija recien en claimOrder() — ver comentario en el struct
            status: OrderStatus.OPEN
        });

        emit OrderCreated(orderId, msg.sender, allowedToken, amount, feeSnapshot);
    }

    /// @dev Aqui se fija `arbiterSnapshot` (ver comentario en el struct Order): a partir de este punto un
    /// `setArbiter()` posterior del owner no afecta a ESTA orden, solo a matches futuros. Tambien es aqui
    /// —no en createOrder()— donde se verifica que el arbitro vigente no coincida con ninguna de las dos
    /// partes reales del trade, porque `arbiter` pudo haber cambiado entre createOrder() y este claim.
    function claimOrder(uint256 orderId) external {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.OPEN, "not open");
        require(msg.sender != o.seller, "seller cannot buy own order"); // tambien garantiza buyer != seller

        address currentArbiter = arbiter;
        require(msg.sender != currentArbiter, "buyer cannot be arbiter");
        require(o.seller != currentArbiter, "seller cannot be arbiter");

        o.buyer = msg.sender;
        o.claimedAt = uint64(block.timestamp);
        o.arbiterSnapshot = currentArbiter;
        o.status = OrderStatus.CLAIMED;

        emit OrderClaimed(orderId, msg.sender, currentArbiter);
    }

    /// @notice si el comprador reclama y desaparece sin confirmar pago, el vendedor recupera el anuncio.
    /// Esto NO mueve fondos: solo reabre la orden para que otro comprador la reclame. Es el unico timeout
    /// del contrato, y solo actua antes de que exista ninguna afirmacion de pago fiat.
    function claimTimeout(uint256 orderId) external {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.CLAIMED, "not claimed");
        require(msg.sender == o.seller, "only seller");
        require(block.timestamp >= o.claimedAt + CLAIM_TIMEOUT, "timeout not reached");

        o.buyer = address(0);
        o.claimedAt = 0;
        o.arbiterSnapshot = address(0); // vuelve a OPEN: se refijara en el proximo claimOrder() que la reclame
        o.status = OrderStatus.OPEN;

        emit ClaimExpired(orderId);
    }

    /// @notice afirmacion on-chain del comprador de que ya pago el fiat fuera de la plataforma.
    /// El contrato no puede verificar esto — es responsabilidad del vendedor confirmar antes de liberar.
    /// A PROPOSITO no existe ningun timeout que libere USDT automaticamente despues de este paso: un
    /// comprador podria marcar PAID falsamente, asi que el unico camino hacia fondos moviendose despues
    /// de PAID es release() (accion voluntaria del vendedor) o una resolucion formal de disputa.
    function confirmPaid(uint256 orderId) external {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.CLAIMED, "not claimed");
        require(msg.sender == o.buyer, "only buyer");

        o.paidAt = uint64(block.timestamp);
        o.status = OrderStatus.PAID;

        emit PaidConfirmed(orderId, msg.sender);
    }

    function release(uint256 orderId) external nonReentrant {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.PAID, "not paid");
        require(msg.sender == o.seller, "only seller");

        _settle(orderId, o.buyer);
    }

    function cancel(uint256 orderId) external nonReentrant {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.OPEN, "not open");
        require(msg.sender == o.seller, "only seller");

        uint256 amount = o.amount;
        address seller = o.seller;

        o.status = OrderStatus.CANCELLED; // effects antes de la interaccion externa
        o.amount = 0;

        ITRC20(allowedToken).safeTransferChecked(seller, amount);

        emit OrderCancelled(orderId);
    }

    // ---------------------------------------------------------------------
    // Disputa — modelo 2-de-3 (comprador / vendedor / arbiter). Ver
    // "ARBITRATION AUTHORITY MODEL" en el informe: el arbiter NUNCA puede
    // resolver una disputa por si solo, necesita el voto de una de las dos
    // partes reales de la orden.
    // ---------------------------------------------------------------------

    /// @notice cualquiera de las dos partes puede escalar a disputa una vez que el comprador afirmo haber
    /// pagado. Esta es la UNICA puerta de entrada a DISPUTED — el arbiter no puede tocar ninguna orden en
    /// OPEN, CLAIMED o PAID hasta que esta transicion ocurra.
    function raiseDispute(uint256 orderId) external {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.PAID, "must be paid to dispute");
        require(msg.sender == o.buyer || msg.sender == o.seller, "not a party");

        // el arbitro de esta disputa YA quedo fijado en claimOrder() (`arbiterSnapshot`) — no se toca aqui.
        o.status = OrderStatus.DISPUTED;

        emit DisputeRaised(orderId, msg.sender, o.arbiterSnapshot);
    }

    /// @notice comprador, vendedor o el arbiter fijado de esta disputa votan por quien deberia recibir los
    /// fondos. Se necesitan 2 de los 3 votos coincidiendo en el mismo destinatario para liquidar — el
    /// arbiter jamas puede decidir solo, y comprador+vendedor tambien pueden resolverlo entre ellos sin el
    /// arbiter si estan de acuerdo.
    function voteDisputeResolution(uint256 orderId, address recipient) external nonReentrant {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.DISPUTED, "not disputed");
        require(
            msg.sender == o.buyer || msg.sender == o.seller || msg.sender == o.arbiterSnapshot,
            "not an eligible voter for this dispute"
        );
        require(recipient == o.buyer || recipient == o.seller, "recipient must be a party");

        _disputeVoteOf[orderId][msg.sender] = recipient;
        emit DisputeVoteCast(orderId, msg.sender, recipient);

        if (_countVotesFor(orderId, o, o.buyer) >= 2) {
            _settle(orderId, o.buyer);
        } else if (_countVotesFor(orderId, o, o.seller) >= 2) {
            _settle(orderId, o.seller);
        }
    }

    /// @dev cuenta votos entre las direcciones DISTINTAS de {buyer, seller, arbiterSnapshot} — si dos de
    /// esos tres roles llegaran a coincidir en la misma direccion (p.ej. por un bug en la asignacion de
    /// arbiter), esa direccion sigue contando como UN solo voto, nunca dos, para que jamas baste con que
    /// una sola persona controle dos de los tres roles.
    function _countVotesFor(uint256 orderId, Order storage o, address candidate) private view returns (uint8 count) {
        address a1 = o.buyer;
        address a2 = o.seller;
        address a3 = o.arbiterSnapshot;
        if (_disputeVoteOf[orderId][a1] == candidate) count++;
        if (a2 != a1 && _disputeVoteOf[orderId][a2] == candidate) count++;
        if (a3 != a1 && a3 != a2 && _disputeVoteOf[orderId][a3] == candidate) count++;
    }

    function disputeVoteOf(uint256 orderId, address voter) external view returns (address) {
        return _disputeVoteOf[orderId][voter];
    }

    /// @dev liquida la orden. El fee SOLO se cobra cuando el destinatario es el comprador (venta
    /// efectivamente completada); un reembolso al vendedor (cancelacion de facto via disputa) nunca paga
    /// fee, sea por release() normal — que nunca puede terminar en reembolso — o por voto de disputa.
    function _settle(uint256 orderId, address recipient) private {
        Order storage o = orders[orderId];

        bool isSaleCompleted = (recipient == o.buyer);
        uint256 amount = o.amount;
        ITRC20 token = ITRC20(o.token);

        uint256 fee = isSaleCompleted ? (amount * o.feeBpsSnapshot) / 10_000 : 0;
        uint256 payout = amount - fee;

        OrderStatus finalStatus = isSaleCompleted ? OrderStatus.RELEASED : OrderStatus.REFUNDED;
        o.status = finalStatus; // effects antes de las interacciones externas
        o.amount = 0;

        if (fee > 0) {
            token.safeTransferChecked(o.feeCollectorSnapshot, fee);
        }
        token.safeTransferChecked(recipient, payout);

        emit OrderSettled(orderId, recipient, payout, fee, finalStatus);
    }

    // ---------------------------------------------------------------------
    // Administracion — ver tabla de poderes administrativos en el informe.
    // `owner` debe ser una cuenta TRON con multi-firma nativa (TIP-16),
    // nunca una EOA sola. Ninguna funcion de esta seccion mueve fondos de
    // usuarios ni puede redirigir el token permitido.
    // ---------------------------------------------------------------------

    function setArbiter(address newArbiter) external onlyOwner {
        require(newArbiter != address(0), "arbiter=0");
        arbiter = newArbiter;
        emit ArbiterUpdated(newArbiter);
        // Nota: solo afecta ordenes que TODAVIA no fueron matcheadas (siguen en OPEN). Cualquier orden ya
        // en CLAIMED/PAID/DISPUTED conserva su propio `arbiterSnapshot`, fijado en claimOrder() — ver
        // comentario en el struct Order.
    }

    function setFee(uint16 newFeeBps, address newFeeCollector) external onlyOwner {
        require(newFeeBps <= MAX_FEE_BPS, "fee too high");
        require(newFeeCollector != address(0), "feeCollector=0");
        feeBps = newFeeBps;
        feeCollector = newFeeCollector;
        emit FeeUpdated(newFeeBps, newFeeCollector);
        // Nota: no afecta ordenes ya creadas — cada orden conserva su propio `feeBpsSnapshot` y
        // `feeCollectorSnapshot`, fijados en createOrder().
    }

    // ---------------------------------------------------------------------
    // Lectura
    // ---------------------------------------------------------------------

    function getOrder(uint256 orderId) external view returns (Order memory) {
        return orders[orderId];
    }
}

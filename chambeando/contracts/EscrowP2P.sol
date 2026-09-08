// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/*
 * EscrowP2P — escrow no-custodial para el mercado P2P de Chambeando.
 *
 * IMPORTANTE ANTES DE MAINNET:
 *  - Este contrato NO ha sido auditado por una firma de seguridad. No debe
 *    manejar fondos reales de terceros sin pasar por una auditoría profesional
 *    y un periodo de pruebas en testnet (Shasta) con volumen real.
 *  - `owner` y `arbiter` deben ser multisig (p.ej. un contrato multisig N-de-M),
 *    nunca una sola EOA controlada por el servidor backend. Si la clave del
 *    backend se compromete y `arbiter` es esa misma clave, un atacante puede
 *    resolver disputas a su favor y drenar todo lo que esté en estado DISPUTED.
 *  - Los imports de ReentrancyGuard/Ownable de abajo son implementaciones
 *    mínimas propias para no depender de un package manager en este entorno.
 *    En producción, reemplazar por las versiones auditadas de OpenZeppelin
 *    (@openzeppelin/contracts) vía npm/hardhat.
 */

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function decimals() external view returns (uint8);
}

abstract contract ReentrancyGuardMinimal {
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _status = _NOT_ENTERED;

    modifier nonReentrant() {
        require(_status != _ENTERED, "reentrant call");
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }
}

abstract contract OwnableMinimal {
    address public owner;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    constructor(address initialOwner) {
        require(initialOwner != address(0), "owner=0");
        owner = initialOwner;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "owner=0");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
}

contract EscrowP2P is ReentrancyGuardMinimal, OwnableMinimal {
    enum OrderStatus {
        OPEN,       // fondos depositados, esperando comprador
        CLAIMED,    // comprador asignado, esperando confirmacion de pago fiat
        PAID,       // comprador confirmo el pago fiat (afirmacion off-chain, no verificable on-chain)
        COMPLETED,  // fondos liberados al comprador (o resueltos a su favor en disputa)
        CANCELLED,  // orden cancelada por el vendedor antes de tener comprador, fondos devueltos
        DISPUTED,   // en disputa, esperando resolucion del arbitro
        REFUNDED    // resuelto en disputa a favor del vendedor
    }

    struct Order {
        address seller;
        address buyer;
        address token;
        uint256 amount;      // neto realmente recibido por el contrato (soporta tokens fee-on-transfer)
        uint64 createdAt;
        uint64 claimedAt;
        uint64 paidAt;
        OrderStatus status;
    }

    /// @notice tiempo maximo que un comprador tiene para confirmar pago tras reclamar una orden
    uint64 public constant CLAIM_TIMEOUT = 2 hours;

    /// @notice fee maximo permitido, en basis points (100 = 1.00%) — limite duro, no se puede subir por encima
    uint16 public constant MAX_FEE_BPS = 100;

    mapping(uint256 => Order) public orders;
    uint256 public nextOrderId;

    address public arbiter;
    address public feeCollector;
    uint16 public feeBps; // p.ej. 25 = 0.25%, similar al fee estandar de QvaPay

    event OrderCreated(uint256 indexed orderId, address indexed seller, address indexed token, uint256 amount);
    event OrderClaimed(uint256 indexed orderId, address indexed buyer);
    event ClaimExpired(uint256 indexed orderId);
    event PaidConfirmed(uint256 indexed orderId, address indexed buyer);
    event OrderCancelled(uint256 indexed orderId);
    event DisputeRaised(uint256 indexed orderId, address indexed raisedBy);
    event OrderSettled(uint256 indexed orderId, address indexed recipient, uint256 payout, uint256 fee);
    event ArbiterUpdated(address indexed newArbiter);
    event FeeUpdated(uint16 newFeeBps, address newFeeCollector);

    constructor(address initialOwner, address initialArbiter, address initialFeeCollector, uint16 initialFeeBps)
        OwnableMinimal(initialOwner)
    {
        require(initialArbiter != address(0), "arbiter=0");
        require(initialFeeCollector != address(0), "feeCollector=0");
        require(initialFeeBps <= MAX_FEE_BPS, "fee too high");
        arbiter = initialArbiter;
        feeCollector = initialFeeCollector;
        feeBps = initialFeeBps;
    }

    // ---------------------------------------------------------------------
    // Camino normal: firmado siempre por el propio usuario (vendedor/comprador)
    // ---------------------------------------------------------------------

    function createOrder(address token, uint256 amount) external nonReentrant returns (uint256 orderId) {
        require(amount > 0, "amount=0");

        uint256 balBefore = IERC20(token).balanceOf(address(this));
        require(IERC20(token).transferFrom(msg.sender, address(this), amount), "transferFrom failed");
        uint256 received = IERC20(token).balanceOf(address(this)) - balBefore; // contabilidad por delta: soporta tokens fee-on-transfer
        require(received > 0, "nothing received");

        orderId = nextOrderId++;
        orders[orderId] = Order({
            seller: msg.sender,
            buyer: address(0),
            token: token,
            amount: received,
            createdAt: uint64(block.timestamp),
            claimedAt: 0,
            paidAt: 0,
            status: OrderStatus.OPEN
        });

        emit OrderCreated(orderId, msg.sender, token, received);
    }

    function claimOrder(uint256 orderId) external {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.OPEN, "not open");
        require(msg.sender != o.seller, "seller cannot buy own order");

        o.buyer = msg.sender;
        o.claimedAt = uint64(block.timestamp);
        o.status = OrderStatus.CLAIMED;

        emit OrderClaimed(orderId, msg.sender);
    }

    /// @notice si el comprador reclama y desaparece sin confirmar pago, el vendedor recupera el anuncio
    function claimTimeout(uint256 orderId) external {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.CLAIMED, "not claimed");
        require(msg.sender == o.seller, "only seller");
        require(block.timestamp >= o.claimedAt + CLAIM_TIMEOUT, "timeout not reached");

        o.buyer = address(0);
        o.claimedAt = 0;
        o.status = OrderStatus.OPEN;

        emit ClaimExpired(orderId);
    }

    /// @notice afirmacion on-chain del comprador de que ya pago el fiat fuera de la plataforma.
    /// El contrato no puede verificar esto — es responsabilidad del vendedor confirmar antes de liberar.
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
        address token = o.token;
        address seller = o.seller;

        o.status = OrderStatus.CANCELLED; // effects antes de la interaccion externa
        o.amount = 0;

        require(IERC20(token).transfer(seller, amount), "refund failed");

        emit OrderCancelled(orderId);
    }

    // ---------------------------------------------------------------------
    // Disputa
    // ---------------------------------------------------------------------

    function raiseDispute(uint256 orderId) external {
        Order storage o = orders[orderId];
        require(o.status == OrderStatus.PAID, "must be paid to dispute");
        require(msg.sender == o.buyer || msg.sender == o.seller, "not a party");

        o.status = OrderStatus.DISPUTED;

        emit DisputeRaised(orderId, msg.sender);
    }

    /// @notice solo el arbiter (debe ser multisig) puede resolver, y solo a favor de una de las dos partes
    function resolveDispute(uint256 orderId, address recipient) external nonReentrant {
        Order storage o = orders[orderId];
        require(msg.sender == arbiter, "only arbiter");
        require(o.status == OrderStatus.DISPUTED, "not disputed");
        require(recipient == o.buyer || recipient == o.seller, "invalid recipient");

        _settle(orderId, recipient);
    }

    function _settle(uint256 orderId, address recipient) internal {
        Order storage o = orders[orderId];

        uint256 amount = o.amount;
        uint256 fee = (amount * feeBps) / 10_000;
        uint256 payout = amount - fee;
        address token = o.token;

        o.status = OrderStatus.COMPLETED; // effects antes de las interacciones externas
        o.amount = 0;

        if (fee > 0) {
            require(IERC20(token).transfer(feeCollector, fee), "fee transfer failed");
        }
        require(IERC20(token).transfer(recipient, payout), "payout failed");

        emit OrderSettled(orderId, recipient, payout, fee);
    }

    // ---------------------------------------------------------------------
    // Administracion — owner debe ser multisig/timelock, nunca una EOA del backend
    // ---------------------------------------------------------------------

    function setArbiter(address newArbiter) external onlyOwner {
        require(newArbiter != address(0), "arbiter=0");
        arbiter = newArbiter;
        emit ArbiterUpdated(newArbiter);
    }

    function setFee(uint16 newFeeBps, address newFeeCollector) external onlyOwner {
        require(newFeeBps <= MAX_FEE_BPS, "fee too high");
        require(newFeeCollector != address(0), "feeCollector=0");
        feeBps = newFeeBps;
        feeCollector = newFeeCollector;
        emit FeeUpdated(newFeeBps, newFeeCollector);
    }

    // ---------------------------------------------------------------------
    // Lectura
    // ---------------------------------------------------------------------

    function getOrder(uint256 orderId) external view returns (Order memory) {
        return orders[orderId];
    }
}

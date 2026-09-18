// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/*
 * Token TRC-20 que entrega MENOS de lo solicitado en transferFrom — simula tanto un token
 * fee-on-transfer/deflacionario real como cualquier implementacion defectuosa que "recorte" la
 * transferencia. `feeBps` es configurable (basis points) para poder probar distintos recortes,
 * incluido un recorte minimo de 1 wei-equivalente. transferFrom debita `amount` completo de
 * `from` pero acredita solo `amount - fee` a `to` — exactamente el patron que el nuevo require de
 * fondeo exacto de EscrowP2P.createOrder() debe rechazar.
 */
contract MockFeeOnTransferTRC20 {
    string public name = "Mock Fee-On-Transfer Token";
    string public symbol = "mFOT";
    uint8 public decimals = 6;
    uint16 public feeBps; // 100 = 1%

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(uint16 feeBps_) {
        feeBps = feeBps_;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        uint256 fee = (amount * feeBps) / 10_000;
        uint256 net = amount - fee;
        require(balanceOf[msg.sender] >= amount, "insufficient balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += net;
        emit Transfer(msg.sender, to, net);
        return true;
    }

    /// @dev debita `amount` completo de `from` pero acredita solo `amount - fee` a `to` — el recorte que
    /// el require de fondeo exacto de EscrowP2P.createOrder() debe detectar y rechazar.
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount, "insufficient balance");
        require(allowance[from][msg.sender] >= amount, "insufficient allowance");
        uint256 fee = (amount * feeBps) / 10_000;
        uint256 net = amount - fee;
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += net;
        emit Transfer(from, to, net);
        return true;
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IEscrowForReentrancy {
    function release(uint256 orderId) external;
    function cancel(uint256 orderId) external;
}

/*
 * Token ERC-20 hostil de solo-tests — mismo rol que
 * contracts/mocks/MaliciousReentrantToken.sol (TRON): transferFrom/transfer
 * se comportan de forma estandar (para fondear la orden normalmente), pero
 * cuando el escrow le hace un `transfer` de SALIDA intenta reentrar
 * llamando de vuelta a `release`/`cancel` sobre la MISMA orden antes de que
 * la primera llamada termine. Sirve para demostrar que `nonReentrant`
 * bloquea el intento.
 */
contract MaliciousReentrantERC20 {
    string public name = "Malicious Reentrant Token";
    string public symbol = "EVIL";
    uint8 public decimals = 6;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    address public escrow;
    uint256 public targetOrderId;
    bool public attackViaRelease;
    bool public attacking;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function configureAttack(address escrow_, uint256 orderId_, bool viaRelease) external {
        escrow = escrow_;
        targetOrderId = orderId_;
        attackViaRelease = viaRelease;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount, "insufficient balance");
        require(allowance[from][msg.sender] >= amount, "insufficient allowance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }

    /// @dev al recibir el pago de salida del escrow, intenta reentrar antes de devolver el control.
    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "insufficient balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);

        if (escrow != address(0) && msg.sender == escrow && !attacking) {
            attacking = true;
            if (attackViaRelease) {
                IEscrowForReentrancy(escrow).release(targetOrderId);
            } else {
                IEscrowForReentrancy(escrow).cancel(targetOrderId);
            }
            attacking = false;
        }
        return true;
    }
}

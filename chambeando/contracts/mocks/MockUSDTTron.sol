// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/*
 * Replica deliberadamente el comportamiento NO estandar documentado de USDT
 * sobre TRON (ver @openzeppelin/tron-contracts SafeTRC20.sol, comentario de
 * safeTransferChecked): `transfer()` mueve el balance correctamente pero
 * devuelve `false` incluso cuando la operacion tuvo exito. `transferFrom()`,
 * en cambio, SI devuelve `true` correctamente — solo `transfer` esta
 * afectado, igual que el token real.
 *
 * Este mock existe para probar que EscrowP2P.sol usa `safeTransferChecked`
 * (verificacion por delta de balance) en vez de confiar en el valor de
 * retorno — si el contrato usara `safeTransfer` normal aqui, CADA pago de
 * este mock revertiria, exactamente como pasaria en produccion con el USDT
 * real de TRON.
 */
contract MockUSDTTron {
    string public name = "Mock USDT (TRON quirk)";
    string public symbol = "mUSDT";
    uint8 public decimals = 6;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    /// @dev mueve el balance correctamente pero SIEMPRE devuelve false en exito — el quirk real.
    /// Sigue revirtiendo en fallo real (saldo insuficiente), igual que el contrato real de TRON.
    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "insufficient balance");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);
        return false; // <-- el quirk: exito real, retorno false
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    /// @dev transferFrom SI devuelve true correctamente en TRON-USDT — no esta afectado por el quirk.
    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(balanceOf[from] >= amount, "insufficient balance");
        require(allowance[from][msg.sender] >= amount, "insufficient allowance");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }
}

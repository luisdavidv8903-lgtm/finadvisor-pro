// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {TRC20} from "@openzeppelin/tron-contracts/token/TRC20/TRC20.sol";

/// @notice Token TRC-20 estandar y cumplidor (transfer/transferFrom devuelven `true` en exito) —
/// usado como linea base "camino feliz" en los tests, y para demostrar que el contrato tambien
/// funciona con un token que SI sigue el estandar al pie de la letra, no solo con el mock de
/// USDT-TRON de abajo.
contract MockTRC20 is TRC20 {
    uint8 private immutable _decimals;

    constructor(string memory name_, string memory symbol_, uint8 decimals_) TRC20(name_, symbol_) {
        _decimals = decimals_;
    }

    function decimals() public view override returns (uint8) {
        return _decimals;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

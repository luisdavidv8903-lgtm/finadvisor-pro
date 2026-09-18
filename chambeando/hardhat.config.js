require("@nomicfoundation/hardhat-toolbox");

/*
 * NOTA IMPORTANTE DE ALCANCE: esta red es la red local EVM de Hardhat, no un
 * nodo TRON/TVM real. Se usa aqui unicamente para compilar y correr los
 * tests unitarios de la logica de negocio de EscrowP2P.sol (maquina de
 * estados, control de acceso, matematica de fees, reentrancy, manejo del
 * valor de retorno de tokens) de forma local y reproducible, sin desplegar
 * a ninguna red — cosa que esta fase EXPLICITAMENTE prohibe.
 *
 * TVM es compatible con el opcode set de la EVM para lo que este contrato
 * usa (no hay precompilados especificos de TRON en juego), asi que estos
 * tests son una base solida — pero NO sustituyen una validacion posterior
 * en Shasta testnet real (energia/bandwidth, direccion real de USDT-TRON,
 * comportamiento del nodo TRON), que queda pendiente para una fase de
 * despliegue separada y autorizada aparte.
 */
module.exports = {
  solidity: {
    version: "0.8.20",
    settings: {
      optimizer: { enabled: true, runs: 200 },
    },
  },
  networks: {
    hardhat: {},
  },
};

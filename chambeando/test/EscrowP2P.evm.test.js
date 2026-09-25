const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture, time } = require("@nomicfoundation/hardhat-network-helpers");

/*
 * Puerto EVM de test/EscrowP2P.test.js (TRON). Cubre las mismas garantias
 * criticas de seguridad (Fase 7, item 16 del encargo): maquina de estados,
 * control de acceso, matematica de fees, reentrancy, fondeo exacto. La
 * unica seccion que NO se porta es "USDT-TRON transfer() return-value
 * quirk": un ERC-20 estandar en EVM no tiene ese comportamiento, y
 * contracts/evm/EscrowP2P.sol usa el SafeERC20.safeTransfer estandar de
 * OpenZeppelin en vez de la variante TRON-only `safeTransferChecked` — ver
 * el comentario de diferencias al inicio de ese contrato.
 */

const Status = {
  OPEN: 0,
  CLAIMED: 1,
  PAID: 2,
  DISPUTED: 3,
  RELEASED: 4,
  REFUNDED: 5,
  CANCELLED: 6,
};

const ONE_TOKEN = 1_000_000n; // 6 decimals, como USDT real

async function baseFixture() {
  const [owner, arbiter, feeCollector, seller, buyer, buyer2, outsider, newArbiter] =
    await ethers.getSigners();

  const MockERC20 = await ethers.getContractFactory("MockERC20");
  const token = await MockERC20.deploy("Mock USDT", "mUSDT", 6);
  await token.waitForDeployment();

  const EscrowP2P = await ethers.getContractFactory("contracts/evm/EscrowP2P.sol:EscrowP2P");
  const escrow = await EscrowP2P.deploy(
    owner.address,
    arbiter.address,
    feeCollector.address,
    0, // fee = 0 para tests generales
    await token.getAddress()
  );
  await escrow.waitForDeployment();

  for (const acct of [seller, buyer2]) {
    await token.mint(acct.address, 1_000n * ONE_TOKEN);
    await token.connect(acct).approve(await escrow.getAddress(), ethers.MaxUint256);
  }

  return { owner, arbiter, feeCollector, seller, buyer, buyer2, outsider, newArbiter, token, escrow };
}

async function feeFixture() {
  const base = await baseFixture();
  const EscrowP2P = await ethers.getContractFactory("contracts/evm/EscrowP2P.sol:EscrowP2P");
  const feeEscrow = await EscrowP2P.deploy(
    base.owner.address,
    base.arbiter.address,
    base.feeCollector.address,
    100, // 1% = MAX_FEE_BPS
    await base.token.getAddress()
  );
  await feeEscrow.waitForDeployment();
  await base.token.connect(base.seller).approve(await feeEscrow.getAddress(), ethers.MaxUint256);
  return { ...base, feeEscrow };
}

async function createClaimedAndPaidOrder({ escrow, seller, buyer, tokenAmount = 100n * ONE_TOKEN }) {
  const tx = await escrow.connect(seller).createOrder(tokenAmount);
  const receipt = await tx.wait();
  const orderId = receipt.logs
    .map((l) => { try { return escrow.interface.parseLog(l); } catch { return null; } })
    .find((l) => l && l.name === "OrderCreated").args.orderId;
  await escrow.connect(buyer).claimOrder(orderId);
  await escrow.connect(buyer).confirmPaid(orderId);
  return orderId;
}

describe("EscrowP2P (EVM)", function () {
  describe("allowed token", function () {
    it("locks the contract to a single immutable token set at deploy", async function () {
      const { escrow, token } = await loadFixture(baseFixture);
      expect(await escrow.allowedToken()).to.equal(await token.getAddress());
    });

    it("two escrow instances with different allowedToken values stay fully segregated", async function () {
      const { owner, arbiter, feeCollector, seller } = await loadFixture(baseFixture);
      const MockERC20 = await ethers.getContractFactory("MockERC20");
      const tokenB = await MockERC20.deploy("Other Token", "OTH", 6);
      await tokenB.waitForDeployment();

      const EscrowP2P = await ethers.getContractFactory("contracts/evm/EscrowP2P.sol:EscrowP2P");
      const escrowB = await EscrowP2P.deploy(owner.address, arbiter.address, feeCollector.address, 0, await tokenB.getAddress());
      await escrowB.waitForDeployment();

      await tokenB.mint(seller.address, 100n * ONE_TOKEN);
      await tokenB.connect(seller).approve(await escrowB.getAddress(), ethers.MaxUint256);
      await escrowB.connect(seller).createOrder(10n * ONE_TOKEN);

      expect((await escrowB.getOrder(0)).token).to.equal(await tokenB.getAddress());
    });
  });

  describe("state transitions", function () {
    it("OPEN -> CLAIMED -> PAID -> RELEASED (normal seller release)", async function () {
      const { escrow, seller, buyer, token } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(100n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await escrow.connect(buyer).confirmPaid(0);

      const buyerBalBefore = await token.balanceOf(buyer.address);
      await escrow.connect(seller).release(0);
      const order = await escrow.getOrder(0);
      expect(order.status).to.equal(Status.RELEASED);
      expect(await token.balanceOf(buyer.address)).to.equal(buyerBalBefore + 100n * ONE_TOKEN);
    });

    it("OPEN -> CANCELLED refunds the seller in full, before any match", async function () {
      const { escrow, seller, token } = await loadFixture(baseFixture);
      const sellerBalBefore = await token.balanceOf(seller.address);
      await escrow.connect(seller).createOrder(50n * ONE_TOKEN);
      await escrow.connect(seller).cancel(0);
      expect((await escrow.getOrder(0)).status).to.equal(Status.CANCELLED);
      expect(await token.balanceOf(seller.address)).to.equal(sellerBalBefore);
    });

    it("CLAIMED -> (timeout) -> OPEN reopens the order without moving funds", async function () {
      const { escrow, seller, buyer, token } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(20n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      const contractBalBefore = await token.balanceOf(await escrow.getAddress());

      await time.increase(2 * 60 * 60 + 1);
      await escrow.connect(seller).claimTimeout(0);

      const order = await escrow.getOrder(0);
      expect(order.status).to.equal(Status.OPEN);
      expect(order.buyer).to.equal(ethers.ZeroAddress);
      expect(await token.balanceOf(await escrow.getAddress())).to.equal(contractBalBefore);
    });

    it("PAID -> DISPUTED -> RELEASED via 2-of-3 vote", async function () {
      const { escrow, seller, buyer, arbiter } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await escrow.connect(buyer).voteDisputeResolution(orderId, buyer.address);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address);
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.RELEASED);
    });

    it("PAID -> DISPUTED -> REFUNDED via 2-of-3 vote, written explicitly on-chain", async function () {
      const { escrow, seller, buyer, arbiter, token } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      const sellerBalBefore = await token.balanceOf(seller.address);

      await escrow.connect(seller).raiseDispute(orderId);
      await escrow.connect(seller).voteDisputeResolution(orderId, seller.address);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, seller.address);

      expect((await escrow.getOrder(orderId)).status).to.equal(Status.REFUNDED);
      expect(await token.balanceOf(seller.address)).to.equal(sellerBalBefore + 100n * ONE_TOKEN);
    });
  });

  describe("illegal transitions revert", function () {
    it("cannot confirmPaid before claim", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await expect(escrow.connect(buyer).confirmPaid(0)).to.be.revertedWith("not claimed");
    });

    it("cannot release before paid", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await expect(escrow.connect(seller).release(0)).to.be.revertedWith("not paid");
    });

    it("cannot raiseDispute before paid", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await expect(escrow.connect(buyer).raiseDispute(0)).to.be.revertedWith("must be paid to dispute");
    });

    it("cannot claimTimeout before the timeout window elapses", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await time.increase(2 * 60 * 60 - 5);
      await expect(escrow.connect(seller).claimTimeout(0)).to.be.revertedWith("timeout not reached");
    });
  });

  describe("authorization", function () {
    it("seller cannot buy their own order", async function () {
      const { escrow, seller } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await expect(escrow.connect(seller).claimOrder(0)).to.be.revertedWith("seller cannot buy own order");
    });

    it("the arbiter cannot create an order as seller", async function () {
      const { escrow, arbiter, token } = await loadFixture(baseFixture);
      await token.mint(arbiter.address, 10n * ONE_TOKEN);
      await token.connect(arbiter).approve(await escrow.getAddress(), ethers.MaxUint256);
      await expect(escrow.connect(arbiter).createOrder(10n * ONE_TOKEN)).to.be.revertedWith("seller cannot be arbiter");
    });

    it("only the buyer can confirmPaid — seller and outsiders cannot", async function () {
      const { escrow, seller, buyer, outsider } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await expect(escrow.connect(seller).confirmPaid(0)).to.be.revertedWith("only buyer");
      await expect(escrow.connect(outsider).confirmPaid(0)).to.be.revertedWith("only buyer");
    });

    it("only the seller can release", async function () {
      const { escrow, seller, buyer, outsider } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await expect(escrow.connect(buyer).release(orderId)).to.be.revertedWith("only seller");
      await expect(escrow.connect(outsider).release(orderId)).to.be.revertedWith("only seller");
    });

    it("an outsider cannot vote on a dispute", async function () {
      const { escrow, seller, buyer, outsider } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await expect(escrow.connect(outsider).voteDisputeResolution(orderId, buyer.address))
        .to.be.revertedWith("not an eligible voter for this dispute");
    });
  });

  describe("arbiter authority boundary", function () {
    it("arbiter vote alone (1 of 3) never settles the dispute", async function () {
      const { escrow, seller, buyer, arbiter } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address);
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.DISPUTED);
    });

    it("buyer + seller can resolve a dispute between themselves, with zero votes from the arbiter", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await escrow.connect(buyer).voteDisputeResolution(orderId, buyer.address);
      await escrow.connect(seller).voteDisputeResolution(orderId, buyer.address);
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.RELEASED);
    });

    it("changing the global arbiter mid-dispute does not affect an already-open dispute", async function () {
      const { escrow, owner, seller, buyer, arbiter, newArbiter } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await escrow.connect(owner).setArbiter(newArbiter.address);

      await expect(escrow.connect(newArbiter).voteDisputeResolution(orderId, buyer.address))
        .to.be.revertedWith("not an eligible voter for this dispute");

      await escrow.connect(buyer).voteDisputeResolution(orderId, buyer.address);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address);
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.RELEASED);
    });
  });

  describe("fees", function () {
    it("release fee math: fee is charged only on a completed sale, at the exact bps", async function () {
      const { feeEscrow, seller, buyer, feeCollector, token } = await loadFixture(feeFixture);
      const amount = 500n * ONE_TOKEN;
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      const feeCollectorBalBefore = await token.balanceOf(feeCollector.address);
      const buyerBalBefore = await token.balanceOf(buyer.address);
      await feeEscrow.connect(seller).release(orderId);

      const expectedFee = (amount * 100n) / 10_000n;
      expect(await token.balanceOf(feeCollector.address)).to.equal(feeCollectorBalBefore + expectedFee);
      expect(await token.balanceOf(buyer.address)).to.equal(buyerBalBefore + (amount - expectedFee));
    });

    it("refund-to-seller always has zero platform fee, even when a live fee is configured", async function () {
      const { feeEscrow, seller, buyer, arbiter, feeCollector, token } = await loadFixture(feeFixture);
      const amount = 500n * ONE_TOKEN;
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      const feeCollectorBalBefore = await token.balanceOf(feeCollector.address);
      const sellerBalBefore = await token.balanceOf(seller.address);

      await feeEscrow.connect(seller).raiseDispute(orderId);
      await feeEscrow.connect(seller).voteDisputeResolution(orderId, seller.address);
      await feeEscrow.connect(arbiter).voteDisputeResolution(orderId, seller.address);

      expect(await token.balanceOf(feeCollector.address)).to.equal(feeCollectorBalBefore);
      expect(await token.balanceOf(seller.address)).to.equal(sellerBalBefore + amount);
    });

    it("fee is snapshotted per order at creation — a later setFee() never changes an in-flight order's economics", async function () {
      const { feeEscrow, owner, seller, buyer, feeCollector, token } = await loadFixture(feeFixture);
      const amount = 500n * ONE_TOKEN;
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      await feeEscrow.connect(owner).setFee(50, feeCollector.address);

      const feeCollectorBalBefore = await token.balanceOf(feeCollector.address);
      await feeEscrow.connect(seller).release(orderId);
      const expectedFee = (amount * 100n) / 10_000n; // sigue el snapshot de 100bps, no el nuevo 50bps
      expect(await token.balanceOf(feeCollector.address)).to.equal(feeCollectorBalBefore + expectedFee);
    });
  });

  describe("admin powers cannot redirect user funds", function () {
    it("only owner can call setArbiter / setFee", async function () {
      const { escrow, outsider, feeCollector } = await loadFixture(baseFixture);
      await expect(escrow.connect(outsider).setArbiter(outsider.address)).to.be.reverted;
      await expect(escrow.connect(outsider).setFee(50, feeCollector.address)).to.be.reverted;
    });

    it("owner cannot release or cancel someone else's order", async function () {
      const { escrow, owner, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await expect(escrow.connect(owner).cancel(0)).to.be.revertedWith("only seller");
      await escrow.connect(buyer).claimOrder(0);
      await escrow.connect(buyer).confirmPaid(0);
      await expect(escrow.connect(owner).release(0)).to.be.revertedWith("only seller");
    });

    it("compromised owner cannot exceed the hard fee cap even by repeated setFee calls", async function () {
      const { escrow, owner, feeCollector } = await loadFixture(baseFixture);
      for (const bad of [101, 500, 10000, 65535]) {
        await expect(escrow.connect(owner).setFee(bad, feeCollector.address)).to.be.revertedWith("fee too high");
      }
    });
  });

  describe("reentrancy", function () {
    async function reentrancyFixture() {
      const [owner, arbiter, feeCollector, seller, buyer] = await ethers.getSigners();
      const Evil = await ethers.getContractFactory("MaliciousReentrantERC20");
      const evilToken = await Evil.deploy();
      await evilToken.waitForDeployment();

      const EscrowP2P = await ethers.getContractFactory("contracts/evm/EscrowP2P.sol:EscrowP2P");
      const escrow = await EscrowP2P.deploy(owner.address, arbiter.address, feeCollector.address, 0, await evilToken.getAddress());
      await escrow.waitForDeployment();

      await evilToken.mint(seller.address, 1000n * ONE_TOKEN);
      await evilToken.connect(seller).approve(await escrow.getAddress(), ethers.MaxUint256);

      return { owner, arbiter, feeCollector, seller, buyer, evilToken, escrow };
    }

    it("blocks a malicious token from reentering release() during payout", async function () {
      const { seller, buyer, evilToken, escrow } = await loadFixture(reentrancyFixture);
      await escrow.connect(seller).createOrder(100n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await escrow.connect(buyer).confirmPaid(0);

      await evilToken.configureAttack(await escrow.getAddress(), 0, true);
      await expect(escrow.connect(seller).release(0)).to.be.reverted;
      expect((await escrow.getOrder(0)).status).to.equal(Status.PAID);

      await evilToken.configureAttack(ethers.ZeroAddress, 0, false);
      await expect(escrow.connect(seller).release(0)).to.not.be.reverted;
      expect((await escrow.getOrder(0)).status).to.equal(Status.RELEASED);
    });

    it("blocks a malicious token from reentering cancel() during refund", async function () {
      const { seller, evilToken, escrow } = await loadFixture(reentrancyFixture);
      await escrow.connect(seller).createOrder(100n * ONE_TOKEN);
      await evilToken.configureAttack(await escrow.getAddress(), 0, false);

      await expect(escrow.connect(seller).cancel(0)).to.be.reverted;
      expect((await escrow.getOrder(0)).status).to.equal(Status.OPEN);

      await evilToken.configureAttack(ethers.ZeroAddress, 0, false);
      await expect(escrow.connect(seller).cancel(0)).to.not.be.reverted;
    });
  });

  describe("exact funding check", function () {
    async function shortTransferFixture(feeBpsOnToken) {
      const [owner, arbiter, feeCollector, seller, buyer] = await ethers.getSigners();
      const Mock = await ethers.getContractFactory("MockFeeOnTransferERC20");
      const shortToken = await Mock.deploy(feeBpsOnToken);
      await shortToken.waitForDeployment();

      const EscrowP2P = await ethers.getContractFactory("contracts/evm/EscrowP2P.sol:EscrowP2P");
      const escrow = await EscrowP2P.deploy(owner.address, arbiter.address, feeCollector.address, 0, await shortToken.getAddress());
      await escrow.waitForDeployment();

      await shortToken.mint(seller.address, 1000n * ONE_TOKEN);
      await shortToken.connect(seller).approve(await escrow.getAddress(), ethers.MaxUint256);

      return { owner, arbiter, feeCollector, seller, buyer, shortToken, escrow };
    }

    it("createOrder reverts when the token delivers less than requested (fee-on-transfer token)", async function () {
      const { seller, escrow } = await shortTransferFixture(100);
      await expect(escrow.connect(seller).createOrder(100n * ONE_TOKEN))
        .to.be.revertedWith("short transfer: exact funding required");
    });

    it("no order is registered when funding is short", async function () {
      const { seller, escrow, shortToken } = await shortTransferFixture(100);
      const contractBalBefore = await shortToken.balanceOf(await escrow.getAddress());
      await expect(escrow.connect(seller).createOrder(100n * ONE_TOKEN)).to.be.reverted;
      expect(await escrow.nextOrderId()).to.equal(0);
      expect(await shortToken.balanceOf(await escrow.getAddress())).to.equal(contractBalBefore);
    });

    it("createOrder succeeds normally when the token has zero skim", async function () {
      const { seller, escrow, shortToken } = await shortTransferFixture(0);
      await expect(escrow.connect(seller).createOrder(100n * ONE_TOKEN)).to.not.be.reverted;
      expect((await escrow.getOrder(0)).amount).to.equal(100n * ONE_TOKEN);
    });
  });

  describe("replay / double execution", function () {
    it("double claim: second claimant is rejected once the order is CLAIMED", async function () {
      const { escrow, seller, buyer, buyer2 } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await expect(escrow.connect(buyer2).claimOrder(0)).to.be.revertedWith("not open");
    });

    it("double release: second call reverts, funds are not paid out twice", async function () {
      const { escrow, seller, buyer, token } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(seller).release(orderId);
      const buyerBalAfterFirst = await token.balanceOf(buyer.address);

      await expect(escrow.connect(seller).release(orderId)).to.be.revertedWith("not paid");
      expect(await token.balanceOf(buyer.address)).to.equal(buyerBalAfterFirst);
    });
  });
});

const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture, time } = require("@nomicfoundation/hardhat-network-helpers");

// OrderStatus enum, mirrors EscrowP2P.sol
const Status = {
  OPEN: 0,
  CLAIMED: 1,
  PAID: 2,
  DISPUTED: 3,
  RELEASED: 4,
  REFUNDED: 5,
  CANCELLED: 6,
};

const ONE_TOKEN = 1_000_000n; // 6 decimals, like real USDT-TRC20

async function baseFixture() {
  const [owner, arbiter, feeCollector, seller, buyer, buyer2, outsider, newArbiter] =
    await ethers.getSigners();

  const MockTRC20 = await ethers.getContractFactory("MockTRC20");
  const token = await MockTRC20.deploy("Mock USDT", "mUSDT", 6);
  await token.waitForDeployment();

  const EscrowP2P = await ethers.getContractFactory("EscrowP2P");
  const escrow = await EscrowP2P.deploy(
    owner.address,
    arbiter.address,
    feeCollector.address,
    0, // fee = 0 for beta/general tests
    await token.getAddress()
  );
  await escrow.waitForDeployment();

  // fund seller + buyer2 generously, approve escrow
  for (const acct of [seller, buyer2]) {
    await token.mint(acct.address, 1_000n * ONE_TOKEN);
    await token.connect(acct).approve(await escrow.getAddress(), ethers.MaxUint256);
  }

  return { owner, arbiter, feeCollector, seller, buyer, buyer2, outsider, newArbiter, token, escrow };
}

async function feeFixture() {
  const base = await baseFixture();
  // separate escrow instance with a live 1% fee, to isolate fee math tests
  const EscrowP2P = await ethers.getContractFactory("EscrowP2P");
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

/** Drives an order all the way to PAID, returns orderId. */
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

describe("EscrowP2P", function () {
  // -------------------------------------------------------------------
  // Token restriction
  // -------------------------------------------------------------------
  describe("allowed token", function () {
    it("locks the contract to a single immutable token set at deploy", async function () {
      const { escrow, token } = await loadFixture(baseFixture);
      expect(await escrow.allowedToken()).to.equal(await token.getAddress());
    });

    it("createOrder always uses the allowed token, never an arbitrary one (no token param exists)", async function () {
      const { escrow, seller, token } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      const order = await escrow.getOrder(0);
      expect(order.token).to.equal(await token.getAddress());
    });

    it("two escrow instances with different allowedToken values stay fully segregated", async function () {
      const { owner, arbiter, feeCollector, seller } = await loadFixture(baseFixture);
      const MockTRC20 = await ethers.getContractFactory("MockTRC20");
      const tokenB = await MockTRC20.deploy("Other Token", "OTH", 6);
      await tokenB.waitForDeployment();

      const EscrowP2P = await ethers.getContractFactory("EscrowP2P");
      const escrowB = await EscrowP2P.deploy(owner.address, arbiter.address, feeCollector.address, 0, await tokenB.getAddress());
      await escrowB.waitForDeployment();

      await tokenB.mint(seller.address, 100n * ONE_TOKEN);
      await tokenB.connect(seller).approve(await escrowB.getAddress(), ethers.MaxUint256);
      await escrowB.connect(seller).createOrder(10n * ONE_TOKEN);

      expect((await escrowB.getOrder(0)).token).to.equal(await tokenB.getAddress());
    });
  });

  // -------------------------------------------------------------------
  // State machine: happy paths
  // -------------------------------------------------------------------
  describe("state transitions", function () {
    it("OPEN -> CLAIMED -> PAID -> RELEASED (normal seller release)", async function () {
      const { escrow, seller, buyer, token } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(100n * ONE_TOKEN);
      expect((await escrow.getOrder(0)).status).to.equal(Status.OPEN);

      await escrow.connect(buyer).claimOrder(0);
      expect((await escrow.getOrder(0)).status).to.equal(Status.CLAIMED);

      await escrow.connect(buyer).confirmPaid(0);
      expect((await escrow.getOrder(0)).status).to.equal(Status.PAID);

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
      const order = await escrow.getOrder(0);
      expect(order.status).to.equal(Status.CANCELLED);
      expect(await token.balanceOf(seller.address)).to.equal(sellerBalBefore); // full round trip, no fee
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
      expect(await token.balanceOf(await escrow.getAddress())).to.equal(contractBalBefore); // untouched
    });

    it("PAID -> DISPUTED -> RELEASED via 2-of-3 vote", async function () {
      const { escrow, seller, buyer, arbiter, token } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.DISPUTED);

      await escrow.connect(buyer).voteDisputeResolution(orderId, buyer.address);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address);

      const order = await escrow.getOrder(orderId);
      expect(order.status).to.equal(Status.RELEASED);
    });

    it("PAID -> DISPUTED -> REFUNDED via 2-of-3 vote, written explicitly on-chain (not inferred off-chain)", async function () {
      const { escrow, seller, buyer, arbiter, token } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      const sellerBalBefore = await token.balanceOf(seller.address);

      await escrow.connect(seller).raiseDispute(orderId);
      await escrow.connect(seller).voteDisputeResolution(orderId, seller.address);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, seller.address);

      const order = await escrow.getOrder(orderId);
      expect(order.status).to.equal(Status.REFUNDED); // real enum value, not derived from event.recipient
      expect(await token.balanceOf(seller.address)).to.equal(sellerBalBefore + 100n * ONE_TOKEN);
    });
  });

  // -------------------------------------------------------------------
  // Illegal transitions revert
  // -------------------------------------------------------------------
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

    it("cannot claimTimeout before the timeout window elapses (one second early)", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await time.increase(2 * 60 * 60 - 5); // just under 2h
      await expect(escrow.connect(seller).claimTimeout(0)).to.be.revertedWith("timeout not reached");
    });

    it("claimTimeout succeeds exactly at the boundary (>=), not only strictly after", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      const tx = await escrow.connect(buyer).claimOrder(0);
      const claimedAt = (await ethers.provider.getBlock(tx.blockNumber)).timestamp;
      await time.increaseTo(claimedAt + 2 * 60 * 60); // exactly the boundary
      await expect(escrow.connect(seller).claimTimeout(0)).to.not.be.reverted;
    });
  });

  // -------------------------------------------------------------------
  // Access control / authorization
  // -------------------------------------------------------------------
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

    it("the arbiter cannot claim an order as buyer", async function () {
      const { escrow, seller, arbiter } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await expect(escrow.connect(arbiter).claimOrder(0)).to.be.revertedWith("buyer cannot be arbiter");
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

    it("only buyer or seller can raiseDispute — arbiter and outsiders cannot", async function () {
      const { escrow, seller, buyer, arbiter, outsider } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await expect(escrow.connect(arbiter).raiseDispute(orderId)).to.be.revertedWith("not a party");
      await expect(escrow.connect(outsider).raiseDispute(orderId)).to.be.revertedWith("not a party");
    });

    it("an outsider (not buyer/seller/arbiter) cannot vote on a dispute", async function () {
      const { escrow, seller, buyer, outsider } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await expect(escrow.connect(outsider).voteDisputeResolution(orderId, buyer.address))
        .to.be.revertedWith("not an eligible voter for this dispute");
    });

    it("a vote for an address that is not a party to the order reverts", async function () {
      const { escrow, seller, buyer, outsider } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await expect(escrow.connect(buyer).voteDisputeResolution(orderId, outsider.address))
        .to.be.revertedWith("recipient must be a party");
    });
  });

  // -------------------------------------------------------------------
  // D) Arbiter power boundary — arbiter is powerless outside DISPUTED,
  //    and can never single-handedly decide a dispute.
  // -------------------------------------------------------------------
  describe("arbiter authority boundary", function () {
    it("arbiter cannot act on an OPEN order (no function it can call has any effect)", async function () {
      const { escrow, seller, arbiter } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      // arbiter has no admin/release/cancel path over someone else's order pre-dispute
      await expect(escrow.connect(arbiter).release(0)).to.be.revertedWith("not paid");
      await expect(escrow.connect(arbiter).cancel(0)).to.be.revertedWith("only seller");
    });

    it("arbiter vote alone (1 of 3) never settles the dispute", async function () {
      const { escrow, seller, buyer, arbiter, token } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);

      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address);

      const order = await escrow.getOrder(orderId);
      expect(order.status).to.equal(Status.DISPUTED); // still disputed — one vote is not enough
    });

    it("buyer + seller can resolve a dispute between themselves, with zero votes from the arbiter", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);

      await escrow.connect(buyer).voteDisputeResolution(orderId, buyer.address);
      await escrow.connect(seller).voteDisputeResolution(orderId, buyer.address);

      expect((await escrow.getOrder(orderId)).status).to.equal(Status.RELEASED);
    });

    it("a voter can change their vote before the threshold is reached, and the tally reflects the latest vote", async function () {
      const { escrow, seller, buyer, arbiter } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(seller).raiseDispute(orderId);

      await escrow.connect(seller).voteDisputeResolution(orderId, seller.address); // 1 for seller
      await escrow.connect(seller).voteDisputeResolution(orderId, buyer.address); // seller changes mind: now 1 for buyer
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.DISPUTED); // still just 1 vote

      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address); // 2 for buyer
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.RELEASED);
    });

    it("changing the global arbiter mid-dispute does not affect an already-open dispute (locked disputeArbiter)", async function () {
      const { escrow, owner, seller, buyer, arbiter, newArbiter } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);

      await escrow.connect(owner).setArbiter(newArbiter.address);

      // the NEW arbiter is not a valid voter for a dispute opened before the swap
      await expect(escrow.connect(newArbiter).voteDisputeResolution(orderId, buyer.address))
        .to.be.revertedWith("not an eligible voter for this dispute");

      // the ORIGINAL arbiter, locked at raiseDispute() time, still can
      await escrow.connect(buyer).voteDisputeResolution(orderId, buyer.address);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address);
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.RELEASED);
    });
  });

  // -------------------------------------------------------------------
  // Fees (G)
  // -------------------------------------------------------------------
  describe("fees", function () {
    it("release fee math: fee is charged only on a completed sale, at the exact bps", async function () {
      const { feeEscrow, seller, buyer, feeCollector, token } = await loadFixture(feeFixture);
      const amount = 500n * ONE_TOKEN;
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      const feeCollectorBalBefore = await token.balanceOf(feeCollector.address);
      const buyerBalBefore = await token.balanceOf(buyer.address);

      await feeEscrow.connect(seller).release(orderId);

      const expectedFee = (amount * 100n) / 10_000n; // 1%
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

      expect(await token.balanceOf(feeCollector.address)).to.equal(feeCollectorBalBefore); // untouched
      expect(await token.balanceOf(seller.address)).to.equal(sellerBalBefore + amount); // full refund
    });

    it("fee upper bound: setFee reverts above MAX_FEE_BPS, succeeds exactly at it", async function () {
      const { escrow, owner, feeCollector } = await loadFixture(baseFixture);
      await expect(escrow.connect(owner).setFee(101, feeCollector.address)).to.be.revertedWith("fee too high");
      await expect(escrow.connect(owner).setFee(100, feeCollector.address)).to.not.be.reverted;
    });

    it("fee is snapshotted per order at creation — a later setFee() never changes an in-flight order's economics", async function () {
      const { feeEscrow, owner, seller, buyer, feeCollector, token } = await loadFixture(feeFixture);
      const amount = 500n * ONE_TOKEN;
      // order created while fee = 100 bps (1%)
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      // owner bumps the fee up to the new cap right before settlement — should NOT affect this order
      await feeEscrow.connect(owner).setFee(100, feeCollector.address); // still 1%, same value, sanity
      await feeEscrow.connect(owner).setFee(50, feeCollector.address); // lower it — order should still use its 100bps snapshot

      const feeCollectorBalBefore = await token.balanceOf(feeCollector.address);
      await feeEscrow.connect(seller).release(orderId);
      const expectedFee = (amount * 100n) / 10_000n; // still the snapshot value, not the new 50bps
      expect(await token.balanceOf(feeCollector.address)).to.equal(feeCollectorBalBefore + expectedFee);
    });
  });

  // -------------------------------------------------------------------
  // Admin power / compromised-admin threat model (H)
  // -------------------------------------------------------------------
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

    it("owner (even if it becomes the new arbiter's controller) cannot vote on a dispute it is not a party to", async function () {
      const { escrow, owner, seller, buyer } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await expect(escrow.connect(owner).voteDisputeResolution(orderId, buyer.address))
        .to.be.revertedWith("not an eligible voter for this dispute");
    });

    it("there is no function with an arbitrary-withdraw/rescue selector — an unknown call reverts, no fallback exists", async function () {
      const { escrow, owner } = await loadFixture(baseFixture);
      // fabricate a plausible admin-drain selector (withdraw(address,uint256)) that does NOT exist on this contract
      const fakeIface = new ethers.Interface(["function withdraw(address to, uint256 amount)"]);
      const data = fakeIface.encodeFunctionData("withdraw", [owner.address, 1]);
      await expect(
        owner.sendTransaction({ to: await escrow.getAddress(), data })
      ).to.be.reverted; // no matching selector, no fallback/receive -> reverts
    });

    it("compromised owner cannot exceed the hard fee cap even by repeated setFee calls", async function () {
      const { escrow, owner, feeCollector } = await loadFixture(baseFixture);
      for (const bad of [101, 500, 10000, 65535]) {
        await expect(escrow.connect(owner).setFee(bad, feeCollector.address)).to.be.revertedWith("fee too high");
      }
    });
  });

  // -------------------------------------------------------------------
  // Reentrancy
  // -------------------------------------------------------------------
  describe("reentrancy", function () {
    async function reentrancyFixture() {
      const [owner, arbiter, feeCollector, seller, buyer] = await ethers.getSigners();
      const Evil = await ethers.getContractFactory("MaliciousReentrantToken");
      const evilToken = await Evil.deploy();
      await evilToken.waitForDeployment();

      const EscrowP2P = await ethers.getContractFactory("EscrowP2P");
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

      await evilToken.configureAttack(await escrow.getAddress(), 0, true); // attack via release()

      await expect(escrow.connect(seller).release(0)).to.be.reverted; // reentrant call bubbles a revert
      expect((await escrow.getOrder(0)).status).to.equal(Status.PAID); // whole tx rolled back, nothing moved

      // disable the attack and confirm a legitimate release still works afterwards
      await evilToken.configureAttack(ethers.ZeroAddress, 0, false);
      await expect(escrow.connect(seller).release(0)).to.not.be.reverted;
      expect((await escrow.getOrder(0)).status).to.equal(Status.RELEASED);
    });

    it("blocks a malicious token from reentering cancel() during refund", async function () {
      const { seller, evilToken, escrow } = await loadFixture(reentrancyFixture);
      await escrow.connect(seller).createOrder(100n * ONE_TOKEN);
      await evilToken.configureAttack(await escrow.getAddress(), 0, false); // attack via cancel()

      await expect(escrow.connect(seller).cancel(0)).to.be.reverted;
      expect((await escrow.getOrder(0)).status).to.equal(Status.OPEN);

      await evilToken.configureAttack(ethers.ZeroAddress, 0, false);
      await expect(escrow.connect(seller).cancel(0)).to.not.be.reverted;
    });
  });

  // -------------------------------------------------------------------
  // TRON/USDT non-standard transfer() return value (B)
  // -------------------------------------------------------------------
  describe("USDT-TRON transfer() return-value quirk", function () {
    async function usdtTronFixture() {
      const [owner, arbiter, feeCollector, seller, buyer] = await ethers.getSigners();
      const MockUSDTTron = await ethers.getContractFactory("MockUSDTTron");
      const usdt = await MockUSDTTron.deploy();
      await usdt.waitForDeployment();

      const EscrowP2P = await ethers.getContractFactory("EscrowP2P");
      const escrow = await EscrowP2P.deploy(owner.address, arbiter.address, feeCollector.address, 0, await usdt.getAddress());
      await escrow.waitForDeployment();

      await usdt.mint(seller.address, 1000n * ONE_TOKEN);
      await usdt.connect(seller).approve(await escrow.getAddress(), ethers.MaxUint256);

      return { owner, arbiter, feeCollector, seller, buyer, usdt, escrow };
    }

    it("sanity check: the mock really does return false from transfer() on a successful move", async function () {
      const { seller, buyer, usdt } = await loadFixture(usdtTronFixture);
      const ok = await usdt.connect(seller).transfer.staticCall(buyer.address, 1n * ONE_TOKEN);
      expect(ok).to.equal(false);
      await usdt.connect(seller).transfer(buyer.address, 1n * ONE_TOKEN); // still moves balance despite returning false
      expect(await usdt.balanceOf(buyer.address)).to.equal(1n * ONE_TOKEN);
    });

    it("release() still pays out correctly against a token whose transfer() returns false on success", async function () {
      const { seller, buyer, usdt, escrow } = await loadFixture(usdtTronFixture);
      await escrow.connect(seller).createOrder(50n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await escrow.connect(buyer).confirmPaid(0);

      await expect(escrow.connect(seller).release(0)).to.not.be.reverted; // would revert here if using safeTransfer instead of safeTransferChecked
      expect(await usdt.balanceOf(buyer.address)).to.equal(50n * ONE_TOKEN);
      expect((await escrow.getOrder(0)).status).to.equal(Status.RELEASED);
    });

    it("cancel() still refunds correctly against the same quirky token", async function () {
      const { seller, usdt, escrow } = await loadFixture(usdtTronFixture);
      const before = await usdt.balanceOf(seller.address);
      await escrow.connect(seller).createOrder(30n * ONE_TOKEN);
      await expect(escrow.connect(seller).cancel(0)).to.not.be.reverted;
      expect(await usdt.balanceOf(seller.address)).to.equal(before);
    });
  });

  // -------------------------------------------------------------------
  // Stuck-fund scenario (documents the intentional limitation from spec
  // item E: no automatic release exists after PAID, by design)
  // -------------------------------------------------------------------
  describe("stuck-fund scenario after PAID with no action", function () {
    it("funds remain locked indefinitely if seller never releases and nobody disputes — no auto-release exists, and nobody but the seller (or a resolved dispute) can move them", async function () {
      const { escrow, owner, seller, buyer, arbiter, outsider, token } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });

      await time.increase(365 * 24 * 60 * 60); // a full year of silence

      // nobody except the seller can move these funds, and there is no timeout that does it for them
      await expect(escrow.connect(buyer).release(orderId)).to.be.revertedWith("only seller");
      await expect(escrow.connect(outsider).release(orderId)).to.be.revertedWith("only seller");
      await expect(escrow.connect(arbiter).release(orderId)).to.be.revertedWith("only seller");
      await expect(escrow.connect(owner).release(orderId)).to.be.revertedWith("only seller");

      expect((await escrow.getOrder(orderId)).status).to.equal(Status.PAID); // still stuck, safely

      // the buyer's only forward path is to escalate — which they can do any time, no timeout needed
      await escrow.connect(buyer).raiseDispute(orderId);
      expect((await escrow.getOrder(orderId)).status).to.equal(Status.DISPUTED);
    });
  });

  // -------------------------------------------------------------------
  // Replay / double execution
  // -------------------------------------------------------------------
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
      expect(await token.balanceOf(buyer.address)).to.equal(buyerBalAfterFirst); // unchanged
    });

    it("double cancel reverts on the second call", async function () {
      const { escrow, seller } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(seller).cancel(0);
      await expect(escrow.connect(seller).cancel(0)).to.be.revertedWith("not open");
    });

    it("cancel is forbidden once an order has been claimed (matched)", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      await expect(escrow.connect(seller).cancel(0)).to.be.revertedWith("not open");
    });

    it("double raiseDispute reverts on the second call", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await expect(escrow.connect(seller).raiseDispute(orderId)).to.be.revertedWith("must be paid to dispute");
    });

    it("voting again after settlement has no effect (status guard blocks it)", async function () {
      const { escrow, seller, buyer, arbiter } = await loadFixture(baseFixture);
      const orderId = await createClaimedAndPaidOrder({ escrow, seller, buyer });
      await escrow.connect(buyer).raiseDispute(orderId);
      await escrow.connect(buyer).voteDisputeResolution(orderId, buyer.address);
      await escrow.connect(arbiter).voteDisputeResolution(orderId, buyer.address); // settles RELEASED

      await expect(escrow.connect(seller).voteDisputeResolution(orderId, seller.address))
        .to.be.revertedWith("not disputed");
    });
  });

  // -------------------------------------------------------------------
  // Phase 2A.1 #1 — arbiterSnapshot moved from raiseDispute() to claimOrder()
  // -------------------------------------------------------------------
  describe("arbiter snapshot timing (fixed at MATCHED/CLAIMED, not at DISPUTED)", function () {
    it("changing the global arbiter AFTER an order is claimed (matched) does not change that order's arbiter, even long before any dispute exists", async function () {
      const { escrow, owner, seller, buyer, arbiter, newArbiter } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0); // arbiterSnapshot fixed here, at match time

      // owner swaps the global arbiter well before any dispute is even raised
      await escrow.connect(owner).setArbiter(newArbiter.address);

      await escrow.connect(buyer).confirmPaid(0);
      await escrow.connect(buyer).raiseDispute(0);

      // the NEW arbiter (in effect since before this trade was even paid) is still not eligible —
      // this is exactly the window the previous design (snapshot at raiseDispute) left open.
      await expect(escrow.connect(newArbiter).voteDisputeResolution(0, buyer.address))
        .to.be.revertedWith("not an eligible voter for this dispute");

      // the ORIGINAL arbiter, locked in at claimOrder() time, still decides
      await escrow.connect(buyer).voteDisputeResolution(0, buyer.address);
      await escrow.connect(arbiter).voteDisputeResolution(0, buyer.address);
      expect((await escrow.getOrder(0)).status).to.equal(Status.RELEASED);
    });

    it("changing the global arbiter DOES affect future matches (orders not yet claimed)", async function () {
      const { escrow, owner, seller, buyer, newArbiter } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN); // still OPEN, unmatched

      await escrow.connect(owner).setArbiter(newArbiter.address);

      const tx = await escrow.connect(buyer).claimOrder(0);
      await expect(tx).to.emit(escrow, "OrderClaimed").withArgs(0, buyer.address, newArbiter.address);
      expect((await escrow.getOrder(0)).arbiterSnapshot).to.equal(newArbiter.address);
    });

    it("an OPEN order always reports arbiterSnapshot == address(0) until it is actually claimed", async function () {
      const { escrow, seller } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      expect((await escrow.getOrder(0)).arbiterSnapshot).to.equal(ethers.ZeroAddress);
    });

    it("admin cannot smuggle the (already-listed) seller in as arbiter for that seller's own existing order — claiming is blocked until arbiter is fixed", async function () {
      const { escrow, owner, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN); // order already exists, listed by `seller`

      await escrow.connect(owner).setArbiter(seller.address); // owner tries to make the seller the arbiter

      // nobody can match this order while seller == arbiter — it fails safe, it does not silently let
      // the seller become both a trade party AND the arbitrator of their own trade
      await expect(escrow.connect(buyer).claimOrder(0)).to.be.revertedWith("seller cannot be arbiter");

      // fixing the arbiter back unblocks the match, and the CORRECT (non-conflicted) arbiter gets snapshotted
      await escrow.connect(owner).setArbiter(buyer.address); // now try making the buyer-to-be the arbiter instead
      await expect(escrow.connect(buyer).claimOrder(0)).to.be.revertedWith("buyer cannot be arbiter");
    });

    it("createOrder itself already rejects a seller who currently IS the arbiter (defense in depth, belt-and-suspenders with the claimOrder check)", async function () {
      const { escrow, arbiter, token } = await loadFixture(baseFixture);
      await token.mint(arbiter.address, 10n * ONE_TOKEN);
      await token.connect(arbiter).approve(await escrow.getAddress(), ethers.MaxUint256);
      await expect(escrow.connect(arbiter).createOrder(10n * ONE_TOKEN)).to.be.revertedWith("seller cannot be arbiter");
    });

    it("claimTimeout clears the stale arbiterSnapshot back to zero when an order reopens", async function () {
      const { escrow, seller, buyer } = await loadFixture(baseFixture);
      await escrow.connect(seller).createOrder(10n * ONE_TOKEN);
      await escrow.connect(buyer).claimOrder(0);
      expect((await escrow.getOrder(0)).arbiterSnapshot).to.not.equal(ethers.ZeroAddress);

      await time.increase(2 * 60 * 60 + 1);
      await escrow.connect(seller).claimTimeout(0);
      expect((await escrow.getOrder(0)).arbiterSnapshot).to.equal(ethers.ZeroAddress);
    });
  });

  // -------------------------------------------------------------------
  // Phase 2A.1 #2 — feeCollectorSnapshot, alongside the existing feeBpsSnapshot
  // -------------------------------------------------------------------
  describe("fee collector snapshot", function () {
    it("a later setFee() cannot redirect an existing order's fee to the new collector", async function () {
      const { feeEscrow, owner, seller, buyer, feeCollector, token } = await loadFixture(feeFixture);
      const [, , , , , , , , attackerCollector] = await ethers.getSigners();
      const amount = 500n * ONE_TOKEN;
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      // owner changes the fee collector after the order was already created
      await feeEscrow.connect(owner).setFee(100, attackerCollector.address);

      const originalCollectorBalBefore = await token.balanceOf(feeCollector.address);
      const attackerCollectorBalBefore = await token.balanceOf(attackerCollector.address);

      await feeEscrow.connect(seller).release(orderId);

      const expectedFee = (amount * 100n) / 10_000n;
      expect(await token.balanceOf(feeCollector.address)).to.equal(originalCollectorBalBefore + expectedFee); // ORIGINAL collector, snapshotted
      expect(await token.balanceOf(attackerCollector.address)).to.equal(attackerCollectorBalBefore); // new collector gets nothing from this order
    });

    it("a later setFee() cannot change an existing order's fee bps (re-confirms feeBpsSnapshot alongside the new collector snapshot)", async function () {
      const { feeEscrow, owner, seller, buyer, feeCollector, token } = await loadFixture(feeFixture);
      const amount = 500n * ONE_TOKEN;
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      await feeEscrow.connect(owner).setFee(0, feeCollector.address); // owner drops the fee to 0 after creation

      const feeCollectorBalBefore = await token.balanceOf(feeCollector.address);
      await feeEscrow.connect(seller).release(orderId);
      const expectedFee = (amount * 100n) / 10_000n; // still the 1% snapshotted at creation, not the new 0%
      expect(await token.balanceOf(feeCollector.address)).to.equal(feeCollectorBalBefore + expectedFee);
    });

    it("a fresh order created AFTER setFee() picks up the new bps and new collector", async function () {
      const { feeEscrow, owner, seller, buyer, token } = await loadFixture(feeFixture);
      const [, , , , , , , , newCollector] = await ethers.getSigners();
      await feeEscrow.connect(owner).setFee(50, newCollector.address);

      const amount = 200n * ONE_TOKEN;
      const orderId = await createClaimedAndPaidOrder({ escrow: feeEscrow, seller, buyer, tokenAmount: amount });

      const newCollectorBalBefore = await token.balanceOf(newCollector.address);
      await feeEscrow.connect(seller).release(orderId);
      const expectedFee = (amount * 50n) / 10_000n;
      expect(await token.balanceOf(newCollector.address)).to.equal(newCollectorBalBefore + expectedFee);
    });
  });

  // -------------------------------------------------------------------
  // Phase 2A.1 #3 — exact funding check: createOrder must reject any
  // short transfer (fee-on-transfer / deflationary / buggy token).
  // -------------------------------------------------------------------
  describe("exact funding check", function () {
    async function shortTransferFixture(feeBpsOnToken) {
      const [owner, arbiter, feeCollector, seller, buyer] = await ethers.getSigners();
      const Mock = await ethers.getContractFactory("MockFeeOnTransferTRC20");
      const shortToken = await Mock.deploy(feeBpsOnToken);
      await shortToken.waitForDeployment();

      const EscrowP2P = await ethers.getContractFactory("EscrowP2P");
      const escrow = await EscrowP2P.deploy(owner.address, arbiter.address, feeCollector.address, 0, await shortToken.getAddress());
      await escrow.waitForDeployment();

      await shortToken.mint(seller.address, 1000n * ONE_TOKEN);
      await shortToken.connect(seller).approve(await escrow.getAddress(), ethers.MaxUint256);

      return { owner, arbiter, feeCollector, seller, buyer, shortToken, escrow };
    }

    it("createOrder reverts when the token delivers less than requested (1% skim, e.g. a fee-on-transfer token)", async function () {
      const { seller, escrow } = await shortTransferFixture(100); // 1% skimmed on every transfer
      await expect(escrow.connect(seller).createOrder(100n * ONE_TOKEN))
        .to.be.revertedWith("short transfer: exact funding required");
    });

    it("createOrder reverts even on a tiny short transfer (0.01% skim, smallest bps unit)", async function () {
      const { seller, escrow } = await shortTransferFixture(1); // 0.01% skimmed — still within the seller's minted balance
      await expect(escrow.connect(seller).createOrder(500n * ONE_TOKEN))
        .to.be.revertedWith("short transfer: exact funding required");
    });

    it("no order is registered (nextOrderId does not advance, no tokens are stuck in escrow) when funding is short", async function () {
      const { seller, escrow, shortToken } = await shortTransferFixture(100);
      const contractBalBefore = await shortToken.balanceOf(await escrow.getAddress());
      await expect(escrow.connect(seller).createOrder(100n * ONE_TOKEN)).to.be.reverted;
      expect(await escrow.nextOrderId()).to.equal(0);
      // the whole tx reverted, so even the "short" tokens that would have arrived never actually moved
      expect(await shortToken.balanceOf(await escrow.getAddress())).to.equal(contractBalBefore);
    });

    it("createOrder succeeds normally when the token has zero skim (0 bps) — exact-funding check does not break compliant tokens", async function () {
      const { seller, escrow, shortToken } = await shortTransferFixture(0);
      await expect(escrow.connect(seller).createOrder(100n * ONE_TOKEN)).to.not.be.reverted;
      expect((await escrow.getOrder(0)).amount).to.equal(100n * ONE_TOKEN);
    });

    it("still passes against the USDT-TRON quirk mock (false-on-success transfer, but transferFrom moves the exact amount) — exact-funding check does not reject the real target token's behavior", async function () {
      const [owner, arbiter, feeCollector, seller] = await ethers.getSigners();
      const MockUSDTTron = await ethers.getContractFactory("MockUSDTTron");
      const usdt = await MockUSDTTron.deploy();
      await usdt.waitForDeployment();
      const EscrowP2P = await ethers.getContractFactory("EscrowP2P");
      const usdtEscrow = await EscrowP2P.deploy(owner.address, arbiter.address, feeCollector.address, 0, await usdt.getAddress());
      await usdtEscrow.waitForDeployment();
      await usdt.mint(seller.address, 100n * ONE_TOKEN);
      await usdt.connect(seller).approve(await usdtEscrow.getAddress(), ethers.MaxUint256);

      await expect(usdtEscrow.connect(seller).createOrder(100n * ONE_TOKEN)).to.not.be.reverted;
      expect((await usdtEscrow.getOrder(0)).amount).to.equal(100n * ONE_TOKEN);
    });
  });
});

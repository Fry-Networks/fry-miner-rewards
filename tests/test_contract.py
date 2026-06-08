"""Unit tests for FryMinerRewardPool contract."""

import pytest
from algopy import Account, Application, Asset, UInt64, arc4
from algopy_testing import algopy_testing_context

from smart_contracts.fry_miner_reward_pool.contract import FryMinerRewardPool, WalletState


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def setup():
    """Standard test setup: context, accounts, assets, deployed contract."""
    with algopy_testing_context() as ctx:
        authority = ctx.any.account(balance=10_000_000)
        user1 = ctx.any.account(balance=10_000_000)
        user2 = ctx.any.account(balance=10_000_000)
        fee_receiver = ctx.any.account(balance=10_000_000)

        tfry = ctx.any.asset(
            asset_id=2681521901,
            name=b"tFRY",
            unit_name=b"tFRY",
            total=10_000_000_000_000,
            decimals=6,
        )
        fnode = ctx.any.asset(
            asset_id=2485202024,
            name=b"fNODE",
            unit_name=b"fNODE",
            total=10_000_000_000_000,
            decimals=6,
        )

        # Create contract
        with ctx.txn.create_group(
            active_txn_overrides={"sender": authority},
        ):
            contract = FryMinerRewardPool()
            contract.create(
                tfry_id=arc4.UInt64(tfry.id),
                fnode_id=arc4.UInt64(fnode.id),
                fee_address=arc4.Address(fee_receiver.bytes),
                fee_bps=arc4.UInt64(3000),
                maturation_epochs=arc4.UInt64(4),
            )

        yield {
            "ctx": ctx,
            "contract": contract,
            "authority": authority,
            "user1": user1,
            "user2": user2,
            "fee_receiver": fee_receiver,
            "tfry": tfry,
            "fnode": fnode,
        }


def _make_mbr_pay(ctx, sender, contract):
    """Helper: create an MBR payment transaction."""
    app_addr = ctx.ledger.get_app(contract).address
    return ctx.any.txn.payment(
        sender=sender,
        receiver=app_addr,
        amount=100_000,
    )


def _publish(ctx, contract, authority, wallet, entitled_tfry, entitled_fnode,
             matured_tfry, matured_fnode, epoch):
    """Helper: publish rewards for a wallet."""
    mbr_pay = _make_mbr_pay(ctx, authority, contract)
    app = Application(contract.__app_id__)
    app_call = ctx.any.txn.application_call(sender=authority, app_id=app)
    with ctx.txn.create_group(
        gtxns=[mbr_pay, app_call],
        active_txn_index=1,
    ):
        contract.publish_rewards(
            wallet=wallet,
            entitled_tfry=arc4.UInt64(entitled_tfry),
            entitled_fnode=arc4.UInt64(entitled_fnode),
            matured_tfry=arc4.UInt64(matured_tfry),
            matured_fnode=arc4.UInt64(matured_fnode),
            epoch=arc4.UInt64(epoch),
            mbr_pay=mbr_pay,
        )


# ── Contract Creation ────────────────────────────────────────────────


class TestContractCreation:
    def test_initial_state(self, setup):
        c = setup["contract"]
        assert c.tfry_id == 2681521901
        assert c.fnode_id == 2485202024
        assert c.fee_bps == 3000
        assert c.maturation_epochs == 4
        assert c.current_epoch == 0
        assert c.paused == 0
        assert c.total_distributed_tfry == 0
        assert c.total_distributed_fnode == 0
        assert c.total_fees_tfry == 0
        assert c.total_fees_fnode == 0


# ── Reward Publishing ────────────────────────────────────────────────


class TestPublishRewards:
    def test_publish_creates_new_box(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]

        _publish(ctx, contract, authority, user1, 1000, 500, 0, 0, 0)

        state = contract.wallets[user1].copy()
        assert state.entitled_tfry.native == 1000
        assert state.entitled_fnode.native == 500
        assert state.matured_tfry.native == 0
        assert state.matured_fnode.native == 0
        assert state.claimed_tfry.native == 0
        assert state.claimed_fnode.native == 0

    def test_publish_updates_existing_preserves_claimed(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]

        # First publish
        _publish(ctx, contract, authority, user1, 1000, 500, 0, 0, 0)

        # Simulate a claim by directly setting box (testing publish preservation)
        contract.wallets[user1] = WalletState(
            entitled_tfry=arc4.UInt64(1000),
            entitled_fnode=arc4.UInt64(500),
            matured_tfry=arc4.UInt64(0),
            matured_fnode=arc4.UInt64(0),
            claimed_tfry=arc4.UInt64(300),
            claimed_fnode=arc4.UInt64(100),
            last_update_epoch=arc4.UInt64(0),
            last_claim_time=arc4.UInt64(12345),
        )

        # Second publish should preserve claimed fields
        _publish(ctx, contract, authority, user1, 2000, 800, 1000, 500, 1)

        state = contract.wallets[user1].copy()
        assert state.entitled_tfry.native == 2000
        assert state.entitled_fnode.native == 800
        assert state.matured_tfry.native == 1000
        assert state.matured_fnode.native == 500
        assert state.claimed_tfry.native == 300  # preserved
        assert state.claimed_fnode.native == 100  # preserved
        assert state.last_update_epoch.native == 1
        assert state.last_claim_time.native == 12345  # preserved

    def test_publish_rejects_non_authority(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        user1 = setup["user1"]
        user2 = setup["user2"]

        mbr_pay = _make_mbr_pay(ctx, user2, contract)
        app = Application(contract.__app_id__)
        app_call = ctx.any.txn.application_call(sender=user2, app_id=app)

        with pytest.raises(AssertionError, match="unauthorized"):
            with ctx.txn.create_group(
                gtxns=[mbr_pay, app_call],
                active_txn_index=1,
            ):
                contract.publish_rewards(
                    wallet=user1,
                    entitled_tfry=arc4.UInt64(100),
                    entitled_fnode=arc4.UInt64(0),
                    matured_tfry=arc4.UInt64(0),
                    matured_fnode=arc4.UInt64(0),
                    epoch=arc4.UInt64(0),
                    mbr_pay=mbr_pay,
                )

    def test_publish_rejects_entitled_less_than_matured(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]

        with pytest.raises(AssertionError, match="entitled >= matured"):
            _publish(ctx, contract, authority, user1, 100, 0, 200, 0, 0)


# ── Epoch Management ─────────────────────────────────────────────────


class TestEpochManagement:
    def test_advance_epoch(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]

        assert contract.current_epoch == 0
        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.advance_epoch()
        assert contract.current_epoch == 1

    def test_advance_epoch_rejects_non_authority(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        user1 = setup["user1"]

        with pytest.raises(AssertionError, match="unauthorized"):
            with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
                contract.advance_epoch()


# ── Claiming ─────────────────────────────────────────────────────────


class TestClaim:
    def test_claim_all_recent_with_fee(self, setup):
        """All rewards are recent (no maturation yet) → 30% fee on everything."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        # Publish 1000 tFRY, 500 fNODE, all recent (matured=0)
        _publish(ctx, contract, authority, user1, 1000, 500, 0, 0, 0)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        state = contract.wallets[user1].copy()
        assert state.claimed_tfry.native == 1000
        assert state.claimed_fnode.native == 500

        # Fee: 1000 * 3000 / 10000 = 300 tFRY
        # User gets: 1000 - 300 = 700 tFRY
        # Fee: 500 * 3000 / 10000 = 150 fNODE
        # User gets: 500 - 150 = 350 fNODE
        assert contract.total_fees_tfry == 300
        assert contract.total_fees_fnode == 150
        assert contract.total_distributed_tfry == 1000
        assert contract.total_distributed_fnode == 500

    def test_claim_all_matured_no_fee(self, setup):
        """All rewards matured → 0% fee."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        # Publish 1000 tFRY, 500 fNODE, ALL matured
        _publish(ctx, contract, authority, user1, 1000, 500, 1000, 500, 4)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        assert contract.total_fees_tfry == 0
        assert contract.total_fees_fnode == 0
        assert contract.total_distributed_tfry == 1000
        assert contract.total_distributed_fnode == 500

    def test_claim_fifo_matured_first(self, setup):
        """Mixed: 600 matured + 400 recent tFRY. FIFO: 600 free, 400 with fee."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        # Publish: entitled=1000, matured=600, all tFRY only
        _publish(ctx, contract, authority, user1, 1000, 0, 600, 0, 4)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        # FIFO: 600 matured (free) + 400 recent (30% fee = 120)
        # User gets: 600 + (400-120) = 880
        # Fee: 120
        assert contract.total_fees_tfry == 120
        assert contract.total_distributed_tfry == 1000  # 880 user + 120 fee

    def test_claim_fifo_after_partial_claim(self, setup):
        """After partial claim, FIFO still works correctly."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        # Publish: entitled=1000, matured=600
        _publish(ctx, contract, authority, user1, 1000, 0, 600, 0, 4)

        # Simulate previous claim of 200 (all from matured, FIFO)
        contract.wallets[user1] = WalletState(
            entitled_tfry=arc4.UInt64(1000),
            entitled_fnode=arc4.UInt64(0),
            matured_tfry=arc4.UInt64(600),
            matured_fnode=arc4.UInt64(0),
            claimed_tfry=arc4.UInt64(200),
            claimed_fnode=arc4.UInt64(0),
            last_update_epoch=arc4.UInt64(4),
            last_claim_time=arc4.UInt64(0),
        )

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        # Remaining: total_claimable = 1000-200 = 800
        # matured_portion = max(0, 600-200) = 400 (free)
        # recent_portion = 800 - 400 = 400 (30% fee = 120)
        assert contract.total_fees_tfry == 120
        assert contract.total_distributed_tfry == 800

    def test_claim_nothing_to_claim(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        # Publish then claim everything
        _publish(ctx, contract, authority, user1, 1000, 0, 0, 0, 0)
        contract.wallets[user1] = WalletState(
            entitled_tfry=arc4.UInt64(1000),
            entitled_fnode=arc4.UInt64(0),
            matured_tfry=arc4.UInt64(0),
            matured_fnode=arc4.UInt64(0),
            claimed_tfry=arc4.UInt64(1000),  # fully claimed
            claimed_fnode=arc4.UInt64(0),
            last_update_epoch=arc4.UInt64(0),
            last_claim_time=arc4.UInt64(0),
        )

        with pytest.raises(AssertionError, match="nothing to claim"):
            with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
                contract.claim(tfry=tfry, fnode=fnode)

    def test_claim_no_rewards_box(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        with pytest.raises(AssertionError, match="no rewards"):
            with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
                contract.claim(tfry=tfry, fnode=fnode)

    def test_claim_blocked_when_paused(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        _publish(ctx, contract, authority, user1, 1000, 0, 0, 0, 0)

        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.pause()

        with pytest.raises(AssertionError, match="paused"):
            with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
                contract.claim(tfry=tfry, fnode=fnode)

    def test_claim_tfry_only(self, setup):
        """Wallet with tFRY but no fNODE."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        _publish(ctx, contract, authority, user1, 500, 0, 500, 0, 4)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        assert contract.total_distributed_tfry == 500
        assert contract.total_distributed_fnode == 0
        assert contract.total_fees_tfry == 0  # all matured


class TestClaimMaturedOnly:
    def test_claim_matured_only_skips_recent(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        # 1000 entitled, 600 matured → claim only 600 (free)
        _publish(ctx, contract, authority, user1, 1000, 500, 600, 300, 4)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim_matured_only(tfry=tfry, fnode=fnode)

        state = contract.wallets[user1].copy()
        assert state.claimed_tfry.native == 600
        assert state.claimed_fnode.native == 300
        assert contract.total_fees_tfry == 0
        assert contract.total_fees_fnode == 0
        assert contract.total_distributed_tfry == 600
        assert contract.total_distributed_fnode == 300

    def test_claim_matured_only_nothing_matured(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        # All recent, nothing matured
        _publish(ctx, contract, authority, user1, 1000, 500, 0, 0, 0)

        with pytest.raises(AssertionError, match="nothing matured"):
            with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
                contract.claim_matured_only(tfry=tfry, fnode=fnode)


# ── Admin Methods ────────────────────────────────────────────────────


class TestAdminMethods:
    def test_set_fee(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]

        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.set_fee(arc4.UInt64(1500))
        assert contract.fee_bps == 1500

    def test_set_fee_rejects_too_high(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]

        with pytest.raises(AssertionError, match="fee too high"):
            with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
                contract.set_fee(arc4.UInt64(6000))

    def test_set_fee_rejects_non_authority(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        user1 = setup["user1"]

        with pytest.raises(AssertionError, match="unauthorized"):
            with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
                contract.set_fee(arc4.UInt64(1500))

    def test_set_maturation(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]

        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.set_maturation(arc4.UInt64(8))
        assert contract.maturation_epochs == 8

    def test_pause_unpause(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]

        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.pause()
        assert contract.paused == 1

        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.unpause()
        assert contract.paused == 0

    def test_set_authority(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]

        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.set_authority(arc4.Address(user1.bytes))
        assert contract.authority == user1


# ── Read-Only Methods ────────────────────────────────────────────────


class TestReadOnly:
    def test_get_wallet_state(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]

        _publish(ctx, contract, authority, user1, 1000, 500, 600, 300, 4)

        state = contract.get_wallet_state(user1)
        assert state.entitled_tfry.native == 1000
        assert state.entitled_fnode.native == 500
        assert state.matured_tfry.native == 600
        assert state.matured_fnode.native == 300

    def test_get_claimable_breakdown(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]

        # entitled=1000, matured=600, claimed=200
        _publish(ctx, contract, authority, user1, 1000, 500, 600, 300, 4)
        contract.wallets[user1] = WalletState(
            entitled_tfry=arc4.UInt64(1000),
            entitled_fnode=arc4.UInt64(500),
            matured_tfry=arc4.UInt64(600),
            matured_fnode=arc4.UInt64(300),
            claimed_tfry=arc4.UInt64(200),
            claimed_fnode=arc4.UInt64(100),
            last_update_epoch=arc4.UInt64(4),
            last_claim_time=arc4.UInt64(0),
        )

        result = contract.get_claimable(user1)
        matured_free_tfry = result[0].native
        recent_tfry = result[1].native
        matured_free_fnode = result[2].native
        recent_fnode = result[3].native

        # tFRY: total=800, matured_portion=max(0, 600-200)=400, recent=400
        assert matured_free_tfry == 400
        assert recent_tfry == 400
        # fNODE: total=400, matured_portion=max(0, 300-100)=200, recent=200
        assert matured_free_fnode == 200
        assert recent_fnode == 200

    def test_get_claimable_no_box(self, setup):
        contract = setup["contract"]
        user1 = setup["user1"]

        with pytest.raises(AssertionError, match="no rewards"):
            contract.get_claimable(user1)


# ── Fee Math Edge Cases ──────────────────────────────────────────────


class TestFeeMath:
    def test_fee_rounding_small_amount(self, setup):
        """Fee on 1 unit: 1 * 3000 / 10000 = 0 (floor division). User gets 1."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        _publish(ctx, contract, authority, user1, 1, 0, 0, 0, 0)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        # 1 * 3000 // 10000 = 0 fee → user gets 1
        assert contract.total_fees_tfry == 0
        assert contract.total_distributed_tfry == 1

    def test_fee_rounding_3_units(self, setup):
        """Fee on 3 units: 3 * 3000 / 10000 = 0 (floor). User gets 3."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        _publish(ctx, contract, authority, user1, 3, 0, 0, 0, 0)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        assert contract.total_fees_tfry == 0
        assert contract.total_distributed_tfry == 3

    def test_fee_rounding_10_units(self, setup):
        """Fee on 10 units: 10 * 3000 / 10000 = 3. User gets 7."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        _publish(ctx, contract, authority, user1, 10, 0, 0, 0, 0)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        assert contract.total_fees_tfry == 3
        assert contract.total_distributed_tfry == 10

    def test_zero_fee_rate(self, setup):
        """Fee set to 0% → no fee at all, even on recent rewards."""
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        with ctx.txn.create_group(active_txn_overrides={"sender": authority}):
            contract.set_fee(arc4.UInt64(0))

        _publish(ctx, contract, authority, user1, 1000, 0, 0, 0, 0)

        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        assert contract.total_fees_tfry == 0
        assert contract.total_distributed_tfry == 1000


# ── Multi-Wallet ─────────────────────────────────────────────────────


class TestMultiWallet:
    def test_two_wallets_independent(self, setup):
        ctx = setup["ctx"]
        contract = setup["contract"]
        authority = setup["authority"]
        user1 = setup["user1"]
        user2 = setup["user2"]
        tfry = setup["tfry"]
        fnode = setup["fnode"]

        _publish(ctx, contract, authority, user1, 1000, 0, 1000, 0, 4)
        _publish(ctx, contract, authority, user2, 500, 200, 0, 0, 4)

        # User1 claims (all matured, no fee)
        with ctx.txn.create_group(active_txn_overrides={"sender": user1}):
            contract.claim(tfry=tfry, fnode=fnode)

        # User2 claims (all recent, 30% fee)
        with ctx.txn.create_group(active_txn_overrides={"sender": user2}):
            contract.claim(tfry=tfry, fnode=fnode)

        state1 = contract.wallets[user1].copy()
        state2 = contract.wallets[user2].copy()
        assert state1.claimed_tfry.native == 1000
        assert state2.claimed_tfry.native == 500
        assert state2.claimed_fnode.native == 200

        # Fees only from user2's recent claims
        assert contract.total_fees_tfry == 150  # 500 * 0.30
        assert contract.total_fees_fnode == 60   # 200 * 0.30

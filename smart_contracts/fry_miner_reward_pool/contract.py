"""FryMinerRewardPool — Smart contract reward distribution for Fry Networks miners.

Replaces the old dbrewards/hotwallet system with on-chain settlement.
Admin publishes weekly per-wallet reward entitlements via direct box writes.
Users claim non-custodially with FIFO maturation (matured=free, recent=30% fee).
Holds both tFRY and fNODE in a single contract.
"""

from algopy import (
    ARC4Contract,
    Account,
    Asset,
    BoxMap,
    Bytes,
    Global,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    op,
    subroutine,
    urange,
)


class WalletState(arc4.Struct):
    """Per-wallet box storage (64 bytes = 8 x UInt64)."""

    entitled_tfry: arc4.UInt64
    entitled_fnode: arc4.UInt64
    matured_tfry: arc4.UInt64
    matured_fnode: arc4.UInt64
    claimed_tfry: arc4.UInt64
    claimed_fnode: arc4.UInt64
    last_update_epoch: arc4.UInt64
    last_claim_time: arc4.UInt64


class FryMinerRewardPool(ARC4Contract):
    """Miner reward pool with FIFO maturation and dual-asset support."""

    def __init__(self) -> None:
        self.authority = Global.creator_address
        self.tfry_id = UInt64(0)
        self.fnode_id = UInt64(0)
        self.fee_address = Global.creator_address
        self.fee_bps = UInt64(3000)
        self.maturation_epochs = UInt64(4)
        self.current_epoch = UInt64(0)
        self.total_distributed_tfry = UInt64(0)
        self.total_distributed_fnode = UInt64(0)
        self.total_fees_tfry = UInt64(0)
        self.total_fees_fnode = UInt64(0)
        self.paused = UInt64(0)
        self.wallets = BoxMap(Account, WalletState, key_prefix=b"w")
        self.merkle_root = Bytes(b"\x00" * 32)

    # ── Admin: Initialization ────────────────────────────────────────

    @arc4.abimethod(create="require")
    def create(
        self,
        tfry_id: arc4.UInt64,
        fnode_id: arc4.UInt64,
        fee_address: arc4.Address,
        fee_bps: arc4.UInt64,
        maturation_epochs: arc4.UInt64,
    ) -> None:
        """Initialize the reward pool with token IDs and fee config."""
        self.tfry_id = tfry_id.native
        self.fnode_id = fnode_id.native
        self.fee_address = Account(fee_address.bytes)
        self.fee_bps = fee_bps.native
        self.maturation_epochs = maturation_epochs.native

    @arc4.abimethod
    def opt_in_assets(
        self,
        tfry: Asset,
        fnode: Asset,
        mbr_pay: gtxn.PaymentTransaction,
    ) -> None:
        """Opt the contract into tFRY and fNODE ASAs. Caller funds MBR."""
        assert Txn.sender == self.authority, "unauthorized"
        assert mbr_pay.receiver == Global.current_application_address, "mbr to app"
        itxn.AssetTransfer(
            xfer_asset=tfry,
            asset_receiver=Global.current_application_address,
            asset_amount=0,
            fee=0,
        ).submit()
        itxn.AssetTransfer(
            xfer_asset=fnode,
            asset_receiver=Global.current_application_address,
            asset_amount=0,
            fee=0,
        ).submit()

    # ── Admin: Reward Publishing ─────────────────────────────────────

    @arc4.abimethod
    def publish_rewards(
        self,
        wallet: Account,
        entitled_tfry: arc4.UInt64,
        entitled_fnode: arc4.UInt64,
        matured_tfry: arc4.UInt64,
        matured_fnode: arc4.UInt64,
        epoch: arc4.UInt64,
        mbr_pay: gtxn.PaymentTransaction,
    ) -> None:
        """Set cumulative entitled + matured amounts for a wallet.

        Admin calls this weekly for each wallet with rewards.
        Creates box on first call (mbr_pay covers MBR).
        Does NOT touch claimed fields — those are user-controlled.
        """
        assert Txn.sender == self.authority, "unauthorized"
        assert entitled_tfry.native >= matured_tfry.native, "entitled >= matured tfry"
        assert entitled_fnode.native >= matured_fnode.native, "entitled >= matured fnode"
        assert epoch.native >= self.current_epoch, "epoch >= current"
        assert mbr_pay.receiver == Global.current_application_address, "mbr to app"

        if wallet in self.wallets:
            existing = self.wallets[wallet].copy()
            self.wallets[wallet] = WalletState(
                entitled_tfry=entitled_tfry,
                entitled_fnode=entitled_fnode,
                matured_tfry=matured_tfry,
                matured_fnode=matured_fnode,
                claimed_tfry=existing.claimed_tfry,
                claimed_fnode=existing.claimed_fnode,
                last_update_epoch=epoch,
                last_claim_time=existing.last_claim_time,
            )
        else:
            self.wallets[wallet] = WalletState(
                entitled_tfry=entitled_tfry,
                entitled_fnode=entitled_fnode,
                matured_tfry=matured_tfry,
                matured_fnode=matured_fnode,
                claimed_tfry=arc4.UInt64(0),
                claimed_fnode=arc4.UInt64(0),
                last_update_epoch=epoch,
                last_claim_time=arc4.UInt64(0),
            )

    @arc4.abimethod
    def advance_epoch(self) -> None:
        """Increment current epoch. Called after weekly publish batch."""
        assert Txn.sender == self.authority, "unauthorized"
        self.current_epoch += 1

    # ── Admin: Merkle Root ────────────────────────────────────────────

    @arc4.abimethod
    def set_merkle_root(self, root: arc4.DynamicBytes) -> None:
        """Store the preseed Merkle root (32 bytes)."""
        assert Txn.sender == self.authority, "unauthorized"
        assert root.native.length == 32, "root must be 32 bytes"
        self.merkle_root = root.native

    # ── Prototype: Merkle Proof Verification (no state changes) ─────

    @arc4.abimethod
    def verify_merkle_proof(
        self,
        entitled_tfry: arc4.UInt64,
        entitled_fnode: arc4.UInt64,
        matured_tfry: arc4.UInt64,
        matured_fnode: arc4.UInt64,
        proof: arc4.DynamicBytes,
        leaf_index: arc4.UInt64,
    ) -> None:
        """Prototype: verify a Merkle proof against stored root.

        No box creation, no token transfer — just proof verification.
        Leaf = SHA256(sender || entitled_tfry || entitled_fnode || matured_tfry || matured_fnode)
        """
        sender = Txn.sender

        # Reconstruct leaf hash
        leaf_data = (
            sender.bytes
            + entitled_tfry.bytes
            + entitled_fnode.bytes
            + matured_tfry.bytes
            + matured_fnode.bytes
        )
        computed = op.sha256(leaf_data)

        # Verify proof path (12 levels for up to 4096 leaves)
        idx = leaf_index.native
        proof_bytes = proof.native
        assert proof_bytes.length == 384, "proof must be 384 bytes (12 x 32)"

        for level in urange(12):
            sibling = op.extract(proof_bytes, level * 32, 32)
            if idx & (UInt64(1) << level):
                computed = op.sha256(sibling + computed)
            else:
                computed = op.sha256(computed + sibling)

        assert computed == self.merkle_root, "invalid proof"

    # ── Admin: Pool Management ───────────────────────────────────────

    @arc4.abimethod
    def fund_pool(self, axfer: gtxn.AssetTransferTransaction) -> None:
        """Deposit tFRY or fNODE into the reward pool."""
        assert Txn.sender == self.authority, "unauthorized"
        assert axfer.asset_receiver == Global.current_application_address, "to app"
        asset_id = axfer.xfer_asset.id
        assert asset_id == self.tfry_id or asset_id == self.fnode_id, "wrong asset"

    @arc4.abimethod
    def set_fee(self, fee_bps: arc4.UInt64) -> None:
        """Adjust fee rate (basis points, max 5000 = 50%)."""
        assert Txn.sender == self.authority, "unauthorized"
        assert fee_bps.native <= 5000, "fee too high"
        self.fee_bps = fee_bps.native

    @arc4.abimethod
    def set_fee_address(self, addr: arc4.Address) -> None:
        """Change fee destination address."""
        assert Txn.sender == self.authority, "unauthorized"
        self.fee_address = Account(addr.bytes)

    @arc4.abimethod
    def set_maturation(self, epochs: arc4.UInt64) -> None:
        """Adjust maturation period (number of epochs before fee-free)."""
        assert Txn.sender == self.authority, "unauthorized"
        self.maturation_epochs = epochs.native

    @arc4.abimethod
    def set_authority(self, new_authority: arc4.Address) -> None:
        """Transfer authority to a new address."""
        assert Txn.sender == self.authority, "unauthorized"
        self.authority = Account(new_authority.bytes)

    @arc4.abimethod
    def pause(self) -> None:
        """Emergency pause — blocks all user claims."""
        assert Txn.sender == self.authority, "unauthorized"
        self.paused = UInt64(1)

    @arc4.abimethod
    def unpause(self) -> None:
        """Resume normal operation."""
        assert Txn.sender == self.authority, "unauthorized"
        self.paused = UInt64(0)

    @arc4.abimethod
    def withdraw_excess(self, asset: Asset, amount: arc4.UInt64, to: Account) -> None:
        """Admin withdraws excess/unused tokens from pool."""
        assert Txn.sender == self.authority, "unauthorized"
        itxn.AssetTransfer(
            xfer_asset=asset,
            asset_receiver=to,
            asset_amount=amount.native,
            fee=0,
        ).submit()

    # ── User: Claiming ───────────────────────────────────────────────

    @arc4.abimethod
    def claim(self, tfry: Asset, fnode: Asset) -> None:
        """Claim ALL available rewards (matured free + recent with fee).

        FIFO: matured rewards claimed first (fee-free), then recent (30% fee).
        Combined into minimum inner txns: 1 send per token to user + 1 fee per token.
        """
        assert self.paused == 0, "paused"
        sender = Txn.sender
        assert sender in self.wallets, "no rewards"

        state = self.wallets[sender].copy()

        entitled_tfry = state.entitled_tfry.native
        entitled_fnode = state.entitled_fnode.native
        matured_tfry = state.matured_tfry.native
        matured_fnode = state.matured_fnode.native
        claimed_tfry = state.claimed_tfry.native
        claimed_fnode = state.claimed_fnode.native

        total_claimable_tfry = entitled_tfry - claimed_tfry
        total_claimable_fnode = entitled_fnode - claimed_fnode
        assert total_claimable_tfry > 0 or total_claimable_fnode > 0, "nothing to claim"

        fee_bps = self.fee_bps
        total_user_tfry = UInt64(0)
        total_fee_tfry = UInt64(0)
        total_user_fnode = UInt64(0)
        total_fee_fnode = UInt64(0)

        if total_claimable_tfry > 0:
            user_amt, fee_amt = _compute_claim(
                total_claimable_tfry, matured_tfry, claimed_tfry, fee_bps
            )
            total_user_tfry = user_amt
            total_fee_tfry = fee_amt

        if total_claimable_fnode > 0:
            user_amt, fee_amt = _compute_claim(
                total_claimable_fnode, matured_fnode, claimed_fnode, fee_bps
            )
            total_user_fnode = user_amt
            total_fee_fnode = fee_amt

        # Send tokens to user
        if total_user_tfry > 0:
            itxn.AssetTransfer(
                xfer_asset=tfry,
                asset_receiver=sender,
                asset_amount=total_user_tfry,
                fee=0,
            ).submit()
        if total_user_fnode > 0:
            itxn.AssetTransfer(
                xfer_asset=fnode,
                asset_receiver=sender,
                asset_amount=total_user_fnode,
                fee=0,
            ).submit()

        # Send fees
        fee_addr = self.fee_address
        if total_fee_tfry > 0:
            itxn.AssetTransfer(
                xfer_asset=tfry,
                asset_receiver=fee_addr,
                asset_amount=total_fee_tfry,
                fee=0,
            ).submit()
        if total_fee_fnode > 0:
            itxn.AssetTransfer(
                xfer_asset=fnode,
                asset_receiver=fee_addr,
                asset_amount=total_fee_fnode,
                fee=0,
            ).submit()

        # Update box — mark all as claimed
        self.wallets[sender] = WalletState(
            entitled_tfry=state.entitled_tfry,
            entitled_fnode=state.entitled_fnode,
            matured_tfry=state.matured_tfry,
            matured_fnode=state.matured_fnode,
            claimed_tfry=arc4.UInt64(entitled_tfry),
            claimed_fnode=arc4.UInt64(entitled_fnode),
            last_update_epoch=state.last_update_epoch,
            last_claim_time=arc4.UInt64(Global.latest_timestamp),
        )

        # Update global counters
        self.total_distributed_tfry += total_user_tfry + total_fee_tfry
        self.total_distributed_fnode += total_user_fnode + total_fee_fnode
        self.total_fees_tfry += total_fee_tfry
        self.total_fees_fnode += total_fee_fnode

    @arc4.abimethod
    def claim_matured_only(self, tfry: Asset, fnode: Asset) -> None:
        """Claim ONLY matured (fee-free) portion. Skips recent rewards."""
        assert self.paused == 0, "paused"
        sender = Txn.sender
        assert sender in self.wallets, "no rewards"

        state = self.wallets[sender].copy()

        matured_tfry = state.matured_tfry.native
        matured_fnode = state.matured_fnode.native
        claimed_tfry = state.claimed_tfry.native
        claimed_fnode = state.claimed_fnode.native

        matured_claimable_tfry = UInt64(0)
        matured_claimable_fnode = UInt64(0)

        if matured_tfry > claimed_tfry:
            matured_claimable_tfry = matured_tfry - claimed_tfry
        if matured_fnode > claimed_fnode:
            matured_claimable_fnode = matured_fnode - claimed_fnode

        assert matured_claimable_tfry > 0 or matured_claimable_fnode > 0, "nothing matured"

        if matured_claimable_tfry > 0:
            itxn.AssetTransfer(
                xfer_asset=tfry,
                asset_receiver=sender,
                asset_amount=matured_claimable_tfry,
                fee=0,
            ).submit()
        if matured_claimable_fnode > 0:
            itxn.AssetTransfer(
                xfer_asset=fnode,
                asset_receiver=sender,
                asset_amount=matured_claimable_fnode,
                fee=0,
            ).submit()

        # Update claimed to include only what was just claimed (matured portion)
        new_claimed_tfry = claimed_tfry + matured_claimable_tfry
        new_claimed_fnode = claimed_fnode + matured_claimable_fnode

        self.wallets[sender] = WalletState(
            entitled_tfry=state.entitled_tfry,
            entitled_fnode=state.entitled_fnode,
            matured_tfry=state.matured_tfry,
            matured_fnode=state.matured_fnode,
            claimed_tfry=arc4.UInt64(new_claimed_tfry),
            claimed_fnode=arc4.UInt64(new_claimed_fnode),
            last_update_epoch=state.last_update_epoch,
            last_claim_time=arc4.UInt64(Global.latest_timestamp),
        )

        self.total_distributed_tfry += matured_claimable_tfry
        self.total_distributed_fnode += matured_claimable_fnode

    # ── Read-only ────────────────────────────────────────────────────

    @arc4.abimethod(readonly=True)
    def get_wallet_state(self, wallet: Account) -> WalletState:
        """Read a wallet's full reward state."""
        assert wallet in self.wallets, "no rewards"
        return self.wallets[wallet].copy()

    @arc4.abimethod(readonly=True)
    def get_claimable(self, wallet: Account) -> arc4.Tuple[arc4.UInt64, arc4.UInt64, arc4.UInt64, arc4.UInt64]:
        """Returns (matured_free_tfry, recent_fee_tfry, matured_free_fnode, recent_fee_fnode)."""
        assert wallet in self.wallets, "no rewards"
        state = self.wallets[wallet].copy()

        entitled_tfry = state.entitled_tfry.native
        matured_tfry = state.matured_tfry.native
        claimed_tfry = state.claimed_tfry.native
        entitled_fnode = state.entitled_fnode.native
        matured_fnode = state.matured_fnode.native
        claimed_fnode = state.claimed_fnode.native

        total_tfry = entitled_tfry - claimed_tfry
        matured_free_tfry = UInt64(0)
        if matured_tfry > claimed_tfry:
            matured_free_tfry = matured_tfry - claimed_tfry
            if matured_free_tfry > total_tfry:
                matured_free_tfry = total_tfry
        recent_tfry = total_tfry - matured_free_tfry

        total_fnode = entitled_fnode - claimed_fnode
        matured_free_fnode = UInt64(0)
        if matured_fnode > claimed_fnode:
            matured_free_fnode = matured_fnode - claimed_fnode
            if matured_free_fnode > total_fnode:
                matured_free_fnode = total_fnode
        recent_fnode = total_fnode - matured_free_fnode

        return arc4.Tuple(
            (arc4.UInt64(matured_free_tfry), arc4.UInt64(recent_tfry),
             arc4.UInt64(matured_free_fnode), arc4.UInt64(recent_fnode))
        )


@subroutine
def _compute_claim(
    total_claimable: UInt64,
    matured: UInt64,
    claimed: UInt64,
    fee_bps: UInt64,
) -> tuple[UInt64, UInt64]:
    """Compute user amount and fee for a single token using FIFO maturation.

    Returns (user_receives, fee_amount).
    Matured portion: claimed first, no fee.
    Recent portion: 30% fee (or whatever fee_bps is).
    """
    matured_portion = UInt64(0)
    if matured > claimed:
        matured_portion = matured - claimed
        if matured_portion > total_claimable:
            matured_portion = total_claimable

    recent_portion = total_claimable - matured_portion

    # Recent portion: deduct fee
    recent_fee = UInt64(0)
    recent_net = UInt64(0)
    if recent_portion > 0:
        recent_fee = recent_portion * fee_bps // UInt64(10000)
        recent_net = recent_portion - recent_fee

    # User gets matured (full) + recent net (after fee)
    user_total = matured_portion + recent_net
    return user_total, recent_fee

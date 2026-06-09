"""E2E integration test for FryMinerRewardPool on algokit localnet.

Full lifecycle: deploy → ASA setup → fund pool → merkle preseed →
user claims with proof → edge cases → weekly update → FIFO fee claim →
final balance verification.

Requires: algokit localnet running (algod@4001, kmd@4002).
Run: uv run python -m pytest tests/test_e2e_localnet.py -v --tb=long -s
"""

from __future__ import annotations

import base64
import struct
from pathlib import Path

import pytest
from algosdk import account, encoding, transaction
from algosdk.abi import Method
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
    TransactionWithSigner,
)
from algosdk.error import AlgodHTTPError
from algosdk.kmd import KMDClient
from algosdk.v2client import algod

from calculator.merkle_tree import MerkleTree, hash_leaf

# ── Constants ────────────────────────────────────────────────────────────

ALGOD_URL = "http://localhost:4001"
ALGOD_TOKEN = "a" * 64
KMD_URL = "http://localhost:4002"
KMD_TOKEN = "a" * 64

TEAL_DIR = Path("smart_contracts/fry_miner_reward_pool")
APPROVAL_TEAL = TEAL_DIR / "FryMinerRewardPool.approval.teal"
CLEAR_TEAL = TEAL_DIR / "FryMinerRewardPool.clear.teal"

FEE_BPS = 3000
MATURATION_EPOCHS = 4
BOX_MBR = 100_000  # microALGO per wallet box
ASA_OPT_IN_MBR = 300_000  # microALGO for 2 ASA opt-ins

POOL_TFRY = 1_000_000_000_000  # 1M tFRY in microunits
POOL_FNODE = 500_000_000_000  # 500k fNODE in microunits

# ABI method signatures (from ARC56)
METHODS = {
    "create": Method.from_signature("create(uint64,uint64,address,uint64,uint64)void"),
    "opt_in_assets": Method.from_signature("opt_in_assets(uint64,uint64,pay)void"),
    "fund_pool": Method.from_signature("fund_pool(axfer)void"),
    "set_merkle_root": Method.from_signature("set_merkle_root(byte[])void"),
    "claim_preseed": Method.from_signature(
        "claim_preseed(uint64,uint64,byte[],uint64,pay,uint64,uint64)void"
    ),
    "publish_rewards": Method.from_signature(
        "publish_rewards(address,uint64,uint64,uint64,uint64,uint64,pay)void"
    ),
    "advance_epoch": Method.from_signature("advance_epoch()void"),
    "claim": Method.from_signature("claim(uint64,uint64)void"),
}


# ── Helpers ──────────────────────────────────────────────────────────────


def app_address(app_id: int) -> str:
    return encoding.encode_address(
        encoding.checksum(b"appID" + app_id.to_bytes(8, "big"))
    )


def wait_for_confirmation(client: algod.AlgodClient, txid: str) -> dict:
    return transaction.wait_for_confirmation(client, txid, 4)


def get_sp(client: algod.AlgodClient, fee: int = 1000) -> transaction.SuggestedParams:
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = fee
    return sp


def get_asset_balance(client: algod.AlgodClient, addr: str, asset_id: int) -> int:
    info = client.account_info(addr)
    for a in info.get("assets", []):
        if a["asset-id"] == asset_id:
            return a["amount"]
    return 0


def read_global_state(client: algod.AlgodClient, app_id: int) -> dict:
    app_info = client.application_info(app_id)
    gs = {}
    for kv in app_info["params"]["global-state"]:
        key = base64.b64decode(kv["key"]).decode("utf-8", errors="replace")
        val = kv["value"]
        if val["type"] == 2:  # uint
            gs[key] = val["uint"]
        else:
            gs[key] = base64.b64decode(val.get("bytes", ""))
    return gs


def read_box_state(client: algod.AlgodClient, app_id: int, wallet_addr: str) -> dict:
    """Read WalletState box: 8 x uint64 big-endian."""
    wallet_bytes = encoding.decode_address(wallet_addr)
    box_name = b"w" + wallet_bytes
    box_resp = client.application_box_by_name(app_id, box_name)
    raw = base64.b64decode(box_resp["value"])
    assert len(raw) == 64, f"box value must be 64 bytes, got {len(raw)}"
    fields = struct.unpack(">8Q", raw)
    return {
        "entitled_tfry": fields[0],
        "entitled_fnode": fields[1],
        "matured_tfry": fields[2],
        "matured_fnode": fields[3],
        "claimed_tfry": fields[4],
        "claimed_fnode": fields[5],
        "last_update_epoch": fields[6],
        "last_claim_time": fields[7],
    }


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def algod_client():
    client = algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL)
    status = client.status()
    assert status["last-round"] >= 0, "algod not responsive"
    return client


@pytest.fixture(scope="module")
def kmd_client():
    return KMDClient(KMD_TOKEN, KMD_URL)


@pytest.fixture(scope="module")
def accounts(algod_client, kmd_client):
    """Get 2 KMD accounts (admin, fee_addr) + generate 3 user accounts."""
    wallets = kmd_client.list_wallets()
    default = next(w for w in wallets if w["name"] == "unencrypted-default-wallet")
    handle = kmd_client.init_wallet_handle(default["id"], "")
    addrs = kmd_client.list_keys(handle)

    result = {}
    # Admin = KMD account 0
    admin_sk = kmd_client.export_key(handle, "", addrs[0])
    result["admin"] = {
        "addr": addrs[0],
        "sk": admin_sk,
        "signer": AccountTransactionSigner(admin_sk),
    }
    # Fee address = KMD account 1
    fee_sk = kmd_client.export_key(handle, "", addrs[1])
    result["fee_addr"] = {
        "addr": addrs[1],
        "sk": fee_sk,
        "signer": AccountTransactionSigner(fee_sk),
    }
    kmd_client.release_wallet_handle(handle)

    # Generate 3 user accounts and fund from admin
    for name in ("user1", "user2", "user3"):
        sk, addr = account.generate_account()
        result[name] = {
            "addr": addr,
            "sk": sk,
            "signer": AccountTransactionSigner(sk),
        }
        # Fund with 10 ALGO
        sp = algod_client.suggested_params()
        txn = transaction.PaymentTxn(
            sender=result["admin"]["addr"], sp=sp, receiver=addr, amt=10_000_000
        )
        stxn = txn.sign(result["admin"]["sk"])
        txid = algod_client.send_transaction(stxn)
        wait_for_confirmation(algod_client, txid)

    return result


@pytest.fixture(scope="module")
def test_asas(algod_client, accounts):
    """Create 2 test ASAs on localnet (tFRY-test, fNODE-test)."""
    admin = accounts["admin"]
    sp = algod_client.suggested_params()
    ids = {}

    for name, unit in [("tFRY-test", "tFRY"), ("fNODE-test", "fNODE")]:
        txn = transaction.AssetCreateTxn(
            sender=admin["addr"],
            sp=sp,
            total=10_000_000_000_000,
            decimals=6,
            default_frozen=False,
            asset_name=name,
            unit_name=unit,
        )
        stxn = txn.sign(admin["sk"])
        txid = algod_client.send_transaction(stxn)
        info = wait_for_confirmation(algod_client, txid)
        ids[unit] = info["asset-index"]

    return ids  # {"tFRY": id, "fNODE": id}


@pytest.fixture(scope="module")
def opt_in_all(algod_client, accounts, test_asas):
    """Opt in fee_addr + all users to both test ASAs."""
    for name in ("fee_addr", "user1", "user2", "user3"):
        acct = accounts[name]
        for asa_id in (test_asas["tFRY"], test_asas["fNODE"]):
            sp = algod_client.suggested_params()
            txn = transaction.AssetTransferTxn(
                sender=acct["addr"],
                sp=sp,
                receiver=acct["addr"],
                amt=0,
                index=asa_id,
            )
            stxn = txn.sign(acct["sk"])
            txid = algod_client.send_transaction(stxn)
            wait_for_confirmation(algod_client, txid)
    return True


@pytest.fixture(scope="module")
def deployed_app(algod_client, accounts, test_asas, opt_in_all):
    """Deploy contract, opt in ASAs, fund pool. Returns app_id."""
    admin = accounts["admin"]
    tfry_id = test_asas["tFRY"]
    fnode_id = test_asas["fNODE"]

    # Compile TEAL
    approval_src = APPROVAL_TEAL.read_text()
    clear_src = CLEAR_TEAL.read_text()
    approval_result = algod_client.compile(approval_src)
    clear_result = algod_client.compile(clear_src)
    approval_bytes = base64.b64decode(approval_result["result"])
    clear_bytes = base64.b64decode(clear_result["result"])
    extra_pages = max(0, (len(approval_bytes) - 2048 + 2047) // 2048)

    # Store TEAL size for verdict
    _state["approval_size"] = len(approval_bytes)
    _state["extra_pages"] = extra_pages

    # Deploy
    sp = get_sp(algod_client, fee=3000)
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=METHODS["create"],
        sender=admin["addr"],
        sp=sp,
        signer=admin["signer"],
        method_args=[tfry_id, fnode_id, accounts["fee_addr"]["addr"], FEE_BPS, MATURATION_EPOCHS],
        approval_program=approval_bytes,
        clear_program=clear_bytes,
        global_schema=transaction.StateSchema(num_uints=10, num_byte_slices=3),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=extra_pages,
        on_complete=transaction.OnComplete.NoOpOC,
    )
    result = atc.execute(algod_client, 4)
    txinfo = algod_client.pending_transaction_info(result.tx_ids[0])
    app_id = txinfo["application-index"]
    app_addr = app_address(app_id)

    # Fund app with ALGO for MBR (opt-in + boxes)
    sp = algod_client.suggested_params()
    fund_txn = transaction.PaymentTxn(
        sender=admin["addr"], sp=sp, receiver=app_addr, amt=1_000_000
    )
    stxn = fund_txn.sign(admin["sk"])
    txid = algod_client.send_transaction(stxn)
    wait_for_confirmation(algod_client, txid)

    # Opt in ASAs
    sp = get_sp(algod_client, fee=3000)
    mbr_pay = transaction.PaymentTxn(
        sender=admin["addr"],
        sp=algod_client.suggested_params(),
        receiver=app_addr,
        amt=ASA_OPT_IN_MBR,
    )
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=METHODS["opt_in_assets"],
        sender=admin["addr"],
        sp=sp,
        signer=admin["signer"],
        method_args=[tfry_id, fnode_id, TransactionWithSigner(mbr_pay, admin["signer"])],
        foreign_assets=[tfry_id, fnode_id],
    )
    atc.execute(algod_client, 4)

    # Fund pool with tFRY
    sp = get_sp(algod_client, fee=2000)
    axfer = transaction.AssetTransferTxn(
        sender=admin["addr"],
        sp=algod_client.suggested_params(),
        receiver=app_addr,
        amt=POOL_TFRY,
        index=tfry_id,
    )
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=METHODS["fund_pool"],
        sender=admin["addr"],
        sp=sp,
        signer=admin["signer"],
        method_args=[TransactionWithSigner(axfer, admin["signer"])],
        foreign_assets=[tfry_id, fnode_id],
    )
    atc.execute(algod_client, 4)

    # Fund pool with fNODE
    sp = get_sp(algod_client, fee=2000)
    axfer = transaction.AssetTransferTxn(
        sender=admin["addr"],
        sp=algod_client.suggested_params(),
        receiver=app_addr,
        amt=POOL_FNODE,
        index=fnode_id,
    )
    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=app_id,
        method=METHODS["fund_pool"],
        sender=admin["addr"],
        sp=sp,
        signer=admin["signer"],
        method_args=[TransactionWithSigner(axfer, admin["signer"])],
        foreign_assets=[tfry_id, fnode_id],
    )
    atc.execute(algod_client, 4)

    # Deploy budget helper app (noop — just approves)
    # Needed because claim_preseed's 12-round SHA256 loop exceeds 700 opcode budget.
    # Adding this app call to the group pools an extra 700 budget (total 1400).
    budget_approval = algod_client.compile("#pragma version 10\nint 1")
    budget_clear = algod_client.compile("#pragma version 10\nint 1")
    budget_approval_bytes = base64.b64decode(budget_approval["result"])
    budget_clear_bytes = base64.b64decode(budget_clear["result"])
    sp = algod_client.suggested_params()
    budget_txn = transaction.ApplicationCreateTxn(
        sender=admin["addr"],
        sp=sp,
        on_complete=transaction.OnComplete.NoOpOC,
        approval_program=budget_approval_bytes,
        clear_program=budget_clear_bytes,
        global_schema=transaction.StateSchema(0, 0),
        local_schema=transaction.StateSchema(0, 0),
    )
    stxn = budget_txn.sign(admin["sk"])
    txid = algod_client.send_transaction(stxn)
    info = wait_for_confirmation(algod_client, txid)
    _state["budget_app_id"] = info["application-index"]

    return app_id


# ── Shared State (module-level) ──────────────────────────────────────────

# Store merkle tree and preseed data across ordered tests
_state: dict = {}


def _make_budget_pad(algod_client, sender, signer):
    """Create a noop app call to pad the opcode budget by 700."""
    sp = algod_client.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000
    txn = transaction.ApplicationCallTxn(
        sender=sender,
        sp=sp,
        index=_state["budget_app_id"],
        on_complete=transaction.OnComplete.NoOpOC,
    )
    return TransactionWithSigner(txn, signer)


# ── Test Class (ordered) ────────────────────────────────────────────────


class TestE2ELifecycle:
    """Tests run in alphabetical order — each phase builds on the previous."""

    # ── Phase A: Deploy & Setup ──────────────────────────────────────

    def test_a_deploy_and_setup(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)

        # Verify global state
        gs = read_global_state(algod_client, app_id)
        assert gs.get("tfry_id") == tfry_id
        assert gs.get("fnode_id") == fnode_id
        assert gs.get("fee_bps") == FEE_BPS
        assert gs.get("maturation_epochs") == MATURATION_EPOCHS
        assert gs.get("current_epoch") == 0
        assert gs.get("paused") == 0
        assert gs.get("total_distributed_tfry") == 0
        assert gs.get("total_distributed_fnode") == 0

        # Verify pool balances
        assert get_asset_balance(algod_client, app_addr, tfry_id) == POOL_TFRY
        assert get_asset_balance(algod_client, app_addr, fnode_id) == POOL_FNODE

        print(f"\n  App ID: {app_id}")
        print(f"  App addr: {app_addr}")
        print(f"  TEAL approval size: {_state.get('approval_size', '?')} bytes")
        print(f"  Extra pages: {_state.get('extra_pages', '?')}")
        print(f"  tFRY pool: {POOL_TFRY / 1_000_000:,.2f}")
        print(f"  fNODE pool: {POOL_FNODE / 1_000_000:,.2f}")

    # ── Phase B: Set Merkle Root ─────────────────────────────────────

    def test_b_set_merkle_root(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        admin = accounts["admin"]

        # Build preseed dataset with known amounts
        preseed = {
            accounts["user1"]["addr"]: {"tfry": 100_000_000, "fnode": 50_000_000},
            accounts["user2"]["addr"]: {"tfry": 200_000_000, "fnode": 100_000_000},
            accounts["user3"]["addr"]: {"tfry": 50_000_000, "fnode": 25_000_000},
        }
        tree, wallet_index = MerkleTree.from_preseed(preseed)

        # Store for later tests
        _state["tree"] = tree
        _state["wallet_index"] = wallet_index
        _state["preseed"] = preseed

        # Set merkle root
        sp = get_sp(algod_client, fee=2000)
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=app_id,
            method=METHODS["set_merkle_root"],
            sender=admin["addr"],
            sp=sp,
            signer=admin["signer"],
            method_args=[tree.root],
        )
        result = atc.execute(algod_client, 4)

        # Verify root stored
        gs = read_global_state(algod_client, app_id)
        assert gs.get("merkle_root") == tree.root, "merkle root mismatch"

        print(f"\n  Merkle root: {tree.root.hex()}")
        print(f"  Wallets in tree: {len(wallet_index)}")
        print(f"  Set root txn: {result.tx_ids[0]}")

    # ── Phase C: User Claims Preseed ─────────────────────────────────

    def test_c_user_claims_preseed(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)
        tree = _state["tree"]
        wallet_index = _state["wallet_index"]
        preseed = _state["preseed"]

        total_tfry_claimed = 0
        total_fnode_claimed = 0

        for name in ("user1", "user2", "user3"):
            user = accounts[name]
            addr = user["addr"]
            amounts = preseed[addr]
            e_tfry = amounts["tfry"]
            e_fnode = amounts["fnode"]
            leaf_idx = wallet_index[addr]
            proof = tree.get_proof(leaf_idx)

            # Balance before
            tfry_before = get_asset_balance(algod_client, addr, tfry_id)
            fnode_before = get_asset_balance(algod_client, addr, fnode_id)

            # Build claim_preseed ATC
            sp = get_sp(algod_client, fee=3000)
            mbr_pay = transaction.PaymentTxn(
                sender=addr,
                sp=algod_client.suggested_params(),
                receiver=app_addr,
                amt=BOX_MBR,
            )
            wallet_bytes = encoding.decode_address(addr)
            atc = AtomicTransactionComposer()
            atc.add_transaction(_make_budget_pad(algod_client, addr, user["signer"]))
            atc.add_method_call(
                app_id=app_id,
                method=METHODS["claim_preseed"],
                sender=addr,
                sp=sp,
                signer=user["signer"],
                method_args=[
                    e_tfry,
                    e_fnode,
                    proof.to_bytes(),
                    leaf_idx,
                    TransactionWithSigner(mbr_pay, user["signer"]),
                    tfry_id,
                    fnode_id,
                ],
                foreign_assets=[tfry_id, fnode_id],
                boxes=[(app_id, b"w" + wallet_bytes)],
            )
            result = atc.execute(algod_client, 4)

            # Verify balances changed
            tfry_after = get_asset_balance(algod_client, addr, tfry_id)
            fnode_after = get_asset_balance(algod_client, addr, fnode_id)
            assert tfry_after - tfry_before == e_tfry, f"{name} tFRY balance mismatch"
            assert fnode_after - fnode_before == e_fnode, f"{name} fNODE balance mismatch"

            # Verify box state (fully claimed)
            box = read_box_state(algod_client, app_id, addr)
            assert box["entitled_tfry"] == e_tfry
            assert box["entitled_fnode"] == e_fnode
            assert box["matured_tfry"] == e_tfry
            assert box["matured_fnode"] == e_fnode
            assert box["claimed_tfry"] == e_tfry
            assert box["claimed_fnode"] == e_fnode
            assert box["last_update_epoch"] == 0

            total_tfry_claimed += e_tfry
            total_fnode_claimed += e_fnode
            print(f"\n  {name} claimed: {e_tfry / 1e6:.2f} tFRY, {e_fnode / 1e6:.2f} fNODE (txn: {result.tx_ids[0]})")

        # Verify global counters
        gs = read_global_state(algod_client, app_id)
        assert gs["total_distributed_tfry"] == total_tfry_claimed
        assert gs["total_distributed_fnode"] == total_fnode_claimed

        # Verify pool decreased
        pool_tfry = get_asset_balance(algod_client, app_addr, tfry_id)
        pool_fnode = get_asset_balance(algod_client, app_addr, fnode_id)
        assert pool_tfry == POOL_TFRY - total_tfry_claimed
        assert pool_fnode == POOL_FNODE - total_fnode_claimed

        _state["total_preseed_tfry"] = total_tfry_claimed
        _state["total_preseed_fnode"] = total_fnode_claimed

    # ── Phase D: Double Claim Rejected ───────────────────────────────

    def test_d_double_claim_rejected(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)
        tree = _state["tree"]
        wallet_index = _state["wallet_index"]
        preseed = _state["preseed"]

        user = accounts["user1"]
        addr = user["addr"]
        amounts = preseed[addr]
        leaf_idx = wallet_index[addr]
        proof = tree.get_proof(leaf_idx)

        sp = get_sp(algod_client, fee=3000)
        mbr_pay = transaction.PaymentTxn(
            sender=addr,
            sp=algod_client.suggested_params(),
            receiver=app_addr,
            amt=BOX_MBR,
        )
        wallet_bytes = encoding.decode_address(addr)
        atc = AtomicTransactionComposer()
        atc.add_transaction(_make_budget_pad(algod_client, addr, user["signer"]))
        atc.add_method_call(
            app_id=app_id,
            method=METHODS["claim_preseed"],
            sender=addr,
            sp=sp,
            signer=user["signer"],
            method_args=[
                amounts["tfry"],
                amounts["fnode"],
                proof.to_bytes(),
                leaf_idx,
                TransactionWithSigner(mbr_pay, user["signer"]),
                tfry_id,
                fnode_id,
            ],
            foreign_assets=[tfry_id, fnode_id],
            boxes=[(app_id, b"w" + wallet_bytes)],
        )

        with pytest.raises(AlgodHTTPError, match="logic eval error"):
            atc.execute(algod_client, 4)

        print("\n  Double-claim correctly rejected")

    # ── Phase E: Invalid Proof Rejected ──────────────────────────────

    def test_e_invalid_proof_rejected(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)
        admin = accounts["admin"]
        tree = _state["tree"]
        wallet_index = _state["wallet_index"]
        preseed = _state["preseed"]

        # Generate user4 (not in tree)
        sk4, addr4 = account.generate_account()
        signer4 = AccountTransactionSigner(sk4)
        _state["user4"] = {"addr": addr4, "sk": sk4, "signer": signer4}

        # Fund + opt in
        sp = algod_client.suggested_params()
        fund_txn = transaction.PaymentTxn(
            sender=admin["addr"], sp=sp, receiver=addr4, amt=10_000_000
        )
        stxn = fund_txn.sign(admin["sk"])
        wait_for_confirmation(algod_client, algod_client.send_transaction(stxn))

        for asa_id in (tfry_id, fnode_id):
            txn = transaction.AssetTransferTxn(
                sender=addr4, sp=algod_client.suggested_params(),
                receiver=addr4, amt=0, index=asa_id,
            )
            stxn = txn.sign(sk4)
            wait_for_confirmation(algod_client, algod_client.send_transaction(stxn))

        # Try claiming with user1's proof from user4's address
        user1_addr = accounts["user1"]["addr"]
        user1_amounts = preseed[user1_addr]
        leaf_idx = wallet_index[user1_addr]
        proof = tree.get_proof(leaf_idx)

        sp = get_sp(algod_client, fee=3000)
        mbr_pay = transaction.PaymentTxn(
            sender=addr4,
            sp=algod_client.suggested_params(),
            receiver=app_addr,
            amt=BOX_MBR,
        )
        wallet_bytes4 = encoding.decode_address(addr4)
        atc = AtomicTransactionComposer()
        atc.add_transaction(_make_budget_pad(algod_client, addr4, signer4))
        atc.add_method_call(
            app_id=app_id,
            method=METHODS["claim_preseed"],
            sender=addr4,
            sp=sp,
            signer=signer4,
            method_args=[
                user1_amounts["tfry"],
                user1_amounts["fnode"],
                proof.to_bytes(),
                leaf_idx,
                TransactionWithSigner(mbr_pay, signer4),
                tfry_id,
                fnode_id,
            ],
            foreign_assets=[tfry_id, fnode_id],
            boxes=[(app_id, b"w" + wallet_bytes4)],
        )

        with pytest.raises(AlgodHTTPError, match="logic eval error"):
            atc.execute(algod_client, 4)

        # Also test corrupted proof bytes
        bad_proof = bytearray(proof.to_bytes())
        bad_proof[0] ^= 0xFF  # Flip first byte
        bad_proof = bytes(bad_proof)

        sp = get_sp(algod_client, fee=3000)
        mbr_pay2 = transaction.PaymentTxn(
            sender=addr4,
            sp=algod_client.suggested_params(),
            receiver=app_addr,
            amt=BOX_MBR,
        )
        atc2 = AtomicTransactionComposer()
        atc2.add_transaction(_make_budget_pad(algod_client, addr4, signer4))
        atc2.add_method_call(
            app_id=app_id,
            method=METHODS["claim_preseed"],
            sender=addr4,
            sp=sp,
            signer=signer4,
            method_args=[
                user1_amounts["tfry"],
                user1_amounts["fnode"],
                bad_proof,
                leaf_idx,
                TransactionWithSigner(mbr_pay2, signer4),
                tfry_id,
                fnode_id,
            ],
            foreign_assets=[tfry_id, fnode_id],
            boxes=[(app_id, b"w" + wallet_bytes4)],
        )

        with pytest.raises(AlgodHTTPError, match="logic eval error"):
            atc2.execute(algod_client, 4)

        print("\n  Invalid proof correctly rejected (wrong sender + corrupted proof)")

    # ── Phase F: Wrong Amounts Rejected ──────────────────────────────

    def test_f_wrong_amounts_rejected(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)

        user4 = _state["user4"]
        addr4 = user4["addr"]
        tree = _state["tree"]
        wallet_index = _state["wallet_index"]
        preseed = _state["preseed"]

        # Use user1's proof/index but with wrong amounts
        user1_addr = accounts["user1"]["addr"]
        leaf_idx = wallet_index[user1_addr]
        proof = tree.get_proof(leaf_idx)

        sp = get_sp(algod_client, fee=3000)
        mbr_pay = transaction.PaymentTxn(
            sender=addr4,
            sp=algod_client.suggested_params(),
            receiver=app_addr,
            amt=BOX_MBR,
        )
        wallet_bytes4 = encoding.decode_address(addr4)
        atc = AtomicTransactionComposer()
        atc.add_transaction(_make_budget_pad(algod_client, addr4, user4["signer"]))
        atc.add_method_call(
            app_id=app_id,
            method=METHODS["claim_preseed"],
            sender=addr4,
            sp=sp,
            signer=user4["signer"],
            method_args=[
                999_999_999,  # Wrong amount
                999_999_999,  # Wrong amount
                proof.to_bytes(),
                leaf_idx,
                TransactionWithSigner(mbr_pay, user4["signer"]),
                tfry_id,
                fnode_id,
            ],
            foreign_assets=[tfry_id, fnode_id],
            boxes=[(app_id, b"w" + wallet_bytes4)],
        )

        with pytest.raises(AlgodHTTPError, match="logic eval error"):
            atc.execute(algod_client, 4)

        print("\n  Wrong amounts correctly rejected")

    # ── Phase G: Non-Preseeded Wallet Rejected ───────────────────────

    def test_g_non_preseeded_wallet_rejected(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)

        user4 = _state["user4"]
        addr4 = user4["addr"]

        # Bogus proof (valid length, all zeros)
        bogus_proof = b"\x00" * 384

        sp = get_sp(algod_client, fee=3000)
        mbr_pay = transaction.PaymentTxn(
            sender=addr4,
            sp=algod_client.suggested_params(),
            receiver=app_addr,
            amt=BOX_MBR,
        )
        wallet_bytes4 = encoding.decode_address(addr4)
        atc = AtomicTransactionComposer()
        atc.add_transaction(_make_budget_pad(algod_client, addr4, user4["signer"]))
        atc.add_method_call(
            app_id=app_id,
            method=METHODS["claim_preseed"],
            sender=addr4,
            sp=sp,
            signer=user4["signer"],
            method_args=[
                1_000_000,
                500_000,
                bogus_proof,
                0,
                TransactionWithSigner(mbr_pay, user4["signer"]),
                tfry_id,
                fnode_id,
            ],
            foreign_assets=[tfry_id, fnode_id],
            boxes=[(app_id, b"w" + wallet_bytes4)],
        )

        with pytest.raises(AlgodHTTPError, match="logic eval error"):
            atc.execute(algod_client, 4)

        print("\n  Non-preseeded wallet correctly rejected")

    # ── Phase H: Weekly Publish Rewards ──────────────────────────────

    def test_h_weekly_publish_rewards(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        admin = accounts["admin"]
        app_addr = app_address(app_id)

        # Publish updated cumulative amounts for each preseed wallet
        # Preseed had: user1 entitled=100M/50M, user2 entitled=200M/100M, user3 entitled=50M/25M
        # Weekly adds new rewards: entitled grows, matured grows partially
        weekly_updates = {
            accounts["user1"]["addr"]: {
                "entitled_tfry": 200_000_000,
                "entitled_fnode": 100_000_000,
                "matured_tfry": 150_000_000,
                "matured_fnode": 75_000_000,
            },
            accounts["user2"]["addr"]: {
                "entitled_tfry": 400_000_000,
                "entitled_fnode": 200_000_000,
                "matured_tfry": 300_000_000,
                "matured_fnode": 150_000_000,
            },
            accounts["user3"]["addr"]: {
                "entitled_tfry": 100_000_000,
                "entitled_fnode": 50_000_000,
                "matured_tfry": 75_000_000,
                "matured_fnode": 37_500_000,
            },
        }
        _state["weekly_updates"] = weekly_updates

        for addr, amounts in weekly_updates.items():
            sp = get_sp(algod_client, fee=1000)
            mbr_pay = transaction.PaymentTxn(
                sender=admin["addr"],
                sp=algod_client.suggested_params(),
                receiver=app_addr,
                amt=BOX_MBR,
            )
            wallet_bytes = encoding.decode_address(addr)
            atc = AtomicTransactionComposer()
            atc.add_method_call(
                app_id=app_id,
                method=METHODS["publish_rewards"],
                sender=admin["addr"],
                sp=sp,
                signer=admin["signer"],
                method_args=[
                    addr,
                    amounts["entitled_tfry"],
                    amounts["entitled_fnode"],
                    amounts["matured_tfry"],
                    amounts["matured_fnode"],
                    1,  # epoch
                    TransactionWithSigner(mbr_pay, admin["signer"]),
                ],
                boxes=[(app_id, b"w" + wallet_bytes)],
            )
            atc.execute(algod_client, 4)

        # Advance epoch
        sp = get_sp(algod_client, fee=1000)
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=app_id,
            method=METHODS["advance_epoch"],
            sender=admin["addr"],
            sp=sp,
            signer=admin["signer"],
            method_args=[],
        )
        atc.execute(algod_client, 4)

        # Verify epoch advanced
        gs = read_global_state(algod_client, app_id)
        assert gs["current_epoch"] == 1

        # Verify boxes updated — entitled/matured changed, claimed preserved from preseed
        preseed = _state["preseed"]
        for name, addr in [
            ("user1", accounts["user1"]["addr"]),
            ("user2", accounts["user2"]["addr"]),
            ("user3", accounts["user3"]["addr"]),
        ]:
            box = read_box_state(algod_client, app_id, addr)
            wu = weekly_updates[addr]
            ps = preseed[addr]
            assert box["entitled_tfry"] == wu["entitled_tfry"], f"{name} entitled_tfry"
            assert box["entitled_fnode"] == wu["entitled_fnode"], f"{name} entitled_fnode"
            assert box["matured_tfry"] == wu["matured_tfry"], f"{name} matured_tfry"
            assert box["matured_fnode"] == wu["matured_fnode"], f"{name} matured_fnode"
            # claimed preserved from preseed
            assert box["claimed_tfry"] == ps["tfry"], f"{name} claimed_tfry preserved"
            assert box["claimed_fnode"] == ps["fnode"], f"{name} claimed_fnode preserved"
            assert box["last_update_epoch"] == 1, f"{name} epoch"

        print("\n  Weekly update published for 3 wallets, epoch advanced to 1")

    # ── Phase I: Claim Updated Rewards (FIFO Fee Logic) ──────────────

    def test_i_claim_updated_rewards_fifo(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)
        preseed = _state["preseed"]
        weekly_updates = _state["weekly_updates"]

        user1 = accounts["user1"]
        addr = user1["addr"]
        fee_addr = accounts["fee_addr"]["addr"]

        # Pre-claim balances
        user_tfry_before = get_asset_balance(algod_client, addr, tfry_id)
        user_fnode_before = get_asset_balance(algod_client, addr, fnode_id)
        fee_tfry_before = get_asset_balance(algod_client, fee_addr, tfry_id)
        fee_fnode_before = get_asset_balance(algod_client, fee_addr, fnode_id)

        # Calculate expected FIFO
        # tFRY: entitled=200M, matured=150M, claimed=100M (preseed)
        # total_claimable = 200M - 100M = 100M
        # matured_portion = min(150M - 100M, 100M) = 50M (fee-free)
        # recent_portion = 100M - 50M = 50M
        # recent_fee = 50M * 3000 / 10000 = 15M
        # user_receives = 50M + 35M = 85M
        expected_user_tfry = 85_000_000
        expected_fee_tfry = 15_000_000

        # fNODE: entitled=100M, matured=75M, claimed=50M (preseed)
        # total_claimable = 100M - 50M = 50M
        # matured_portion = min(75M - 50M, 50M) = 25M (fee-free)
        # recent_portion = 50M - 25M = 25M
        # recent_fee = 25M * 3000 / 10000 = 7.5M
        # user_receives = 25M + 17.5M = 42.5M
        expected_user_fnode = 42_500_000
        expected_fee_fnode = 7_500_000

        # Execute claim
        sp = get_sp(algod_client, fee=5000)
        wallet_bytes = encoding.decode_address(addr)
        fee_bytes = encoding.decode_address(fee_addr)
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=app_id,
            method=METHODS["claim"],
            sender=addr,
            sp=sp,
            signer=user1["signer"],
            method_args=[tfry_id, fnode_id],
            foreign_assets=[tfry_id, fnode_id],
            boxes=[(app_id, b"w" + wallet_bytes)],
            accounts=[fee_addr],
        )
        result = atc.execute(algod_client, 4)

        # Verify user balances
        user_tfry_after = get_asset_balance(algod_client, addr, tfry_id)
        user_fnode_after = get_asset_balance(algod_client, addr, fnode_id)
        actual_user_tfry = user_tfry_after - user_tfry_before
        actual_user_fnode = user_fnode_after - user_fnode_before
        assert actual_user_tfry == expected_user_tfry, (
            f"user tFRY: expected {expected_user_tfry}, got {actual_user_tfry}"
        )
        assert actual_user_fnode == expected_user_fnode, (
            f"user fNODE: expected {expected_user_fnode}, got {actual_user_fnode}"
        )

        # Verify fee address balances
        fee_tfry_after = get_asset_balance(algod_client, fee_addr, tfry_id)
        fee_fnode_after = get_asset_balance(algod_client, fee_addr, fnode_id)
        actual_fee_tfry = fee_tfry_after - fee_tfry_before
        actual_fee_fnode = fee_fnode_after - fee_fnode_before
        assert actual_fee_tfry == expected_fee_tfry, (
            f"fee tFRY: expected {expected_fee_tfry}, got {actual_fee_tfry}"
        )
        assert actual_fee_fnode == expected_fee_fnode, (
            f"fee fNODE: expected {expected_fee_fnode}, got {actual_fee_fnode}"
        )

        # Verify box: all claimed
        box = read_box_state(algod_client, app_id, addr)
        wu = weekly_updates[addr]
        assert box["claimed_tfry"] == wu["entitled_tfry"]
        assert box["claimed_fnode"] == wu["entitled_fnode"]

        # Verify global fee counters
        gs = read_global_state(algod_client, app_id)
        assert gs["total_fees_tfry"] == expected_fee_tfry
        assert gs["total_fees_fnode"] == expected_fee_fnode

        _state["claim1_user_tfry"] = expected_user_tfry
        _state["claim1_user_fnode"] = expected_user_fnode
        _state["claim1_fee_tfry"] = expected_fee_tfry
        _state["claim1_fee_fnode"] = expected_fee_fnode

        print(f"\n  user1 claimed: {actual_user_tfry / 1e6:.2f} tFRY, {actual_user_fnode / 1e6:.2f} fNODE")
        print(f"  fee received: {actual_fee_tfry / 1e6:.2f} tFRY, {actual_fee_fnode / 1e6:.2f} fNODE")
        print(f"  txn: {result.tx_ids[0]}")

    # ── Phase J: Final State Verification ────────────────────────────

    def test_j_final_state_verification(self, algod_client, accounts, test_asas, deployed_app):
        app_id = deployed_app
        tfry_id = test_asas["tFRY"]
        fnode_id = test_asas["fNODE"]
        app_addr = app_address(app_id)
        preseed = _state["preseed"]
        weekly_updates = _state["weekly_updates"]

        # Read all wallet boxes
        for name, addr in [
            ("user1", accounts["user1"]["addr"]),
            ("user2", accounts["user2"]["addr"]),
            ("user3", accounts["user3"]["addr"]),
        ]:
            box = read_box_state(algod_client, app_id, addr)
            wu = weekly_updates[addr]
            print(f"\n  {name} box: entitled={box['entitled_tfry']}/{box['entitled_fnode']}, "
                  f"matured={box['matured_tfry']}/{box['matured_fnode']}, "
                  f"claimed={box['claimed_tfry']}/{box['claimed_fnode']}")

            # Entitled/matured should match weekly update
            assert box["entitled_tfry"] == wu["entitled_tfry"]
            assert box["entitled_fnode"] == wu["entitled_fnode"]
            assert box["matured_tfry"] == wu["matured_tfry"]
            assert box["matured_fnode"] == wu["matured_fnode"]

        # user1: fully claimed (preseed + weekly)
        box1 = read_box_state(algod_client, app_id, accounts["user1"]["addr"])
        assert box1["claimed_tfry"] == weekly_updates[accounts["user1"]["addr"]]["entitled_tfry"]
        assert box1["claimed_fnode"] == weekly_updates[accounts["user1"]["addr"]]["entitled_fnode"]

        # user2, user3: only preseed claimed (didn't call claim() in Phase I)
        for name in ("user2", "user3"):
            addr = accounts[name]["addr"]
            box = read_box_state(algod_client, app_id, addr)
            assert box["claimed_tfry"] == preseed[addr]["tfry"], f"{name} claimed_tfry"
            assert box["claimed_fnode"] == preseed[addr]["fnode"], f"{name} claimed_fnode"

        # Contract ASA balances
        # Total outflows:
        #   Preseed: 350M tFRY (100+200+50), 175M fNODE (50+100+25)
        #   user1 weekly claim: 85M tFRY user + 15M tFRY fee = 100M total outflow
        #   user1 weekly claim: 42.5M fNODE user + 7.5M fNODE fee = 50M total outflow
        total_tfry_out = _state["total_preseed_tfry"] + _state["claim1_user_tfry"] + _state["claim1_fee_tfry"]
        total_fnode_out = _state["total_preseed_fnode"] + _state["claim1_user_fnode"] + _state["claim1_fee_fnode"]

        expected_pool_tfry = POOL_TFRY - total_tfry_out
        expected_pool_fnode = POOL_FNODE - total_fnode_out

        actual_pool_tfry = get_asset_balance(algod_client, app_addr, tfry_id)
        actual_pool_fnode = get_asset_balance(algod_client, app_addr, fnode_id)

        assert actual_pool_tfry == expected_pool_tfry, (
            f"pool tFRY: expected {expected_pool_tfry}, got {actual_pool_tfry}"
        )
        assert actual_pool_fnode == expected_pool_fnode, (
            f"pool fNODE: expected {expected_pool_fnode}, got {actual_pool_fnode}"
        )

        # Global counters cross-check
        gs = read_global_state(algod_client, app_id)
        # total_distributed includes user amounts + fees
        expected_total_dist_tfry = _state["total_preseed_tfry"] + _state["claim1_user_tfry"] + _state["claim1_fee_tfry"]
        expected_total_dist_fnode = _state["total_preseed_fnode"] + _state["claim1_user_fnode"] + _state["claim1_fee_fnode"]
        assert gs["total_distributed_tfry"] == expected_total_dist_tfry
        assert gs["total_distributed_fnode"] == expected_total_dist_fnode
        assert gs["total_fees_tfry"] == _state["claim1_fee_tfry"]
        assert gs["total_fees_fnode"] == _state["claim1_fee_fnode"]

        print(f"\n  Pool remaining: {actual_pool_tfry / 1e6:,.2f} tFRY, {actual_pool_fnode / 1e6:,.2f} fNODE")
        print(f"  Total distributed: {gs['total_distributed_tfry'] / 1e6:,.2f} tFRY, {gs['total_distributed_fnode'] / 1e6:,.2f} fNODE")
        print(f"  Total fees: {gs['total_fees_tfry'] / 1e6:,.2f} tFRY, {gs['total_fees_fnode'] / 1e6:,.2f} fNODE")
        print(f"\n  ALL BALANCES VERIFIED")

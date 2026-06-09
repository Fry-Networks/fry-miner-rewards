#!/usr/bin/env python3
"""Deploy and manage FryMinerRewardPool on Algorand mainnet.

Signs with rekey mnemonic (authority wallet is rekeyed).
Handles: compile TEAL, deploy, opt_in_assets, fund_pool, set_merkle_root,
         deploy-budget-helper, claim-preseed.

Usage:
    python deploy_mainnet.py --action deploy
    python deploy_mainnet.py --action opt-in --app-id APP
    python deploy_mainnet.py --action fund --app-id APP --asset tfry --amount 176000000000000
    python deploy_mainnet.py --action verify --app-id APP
    python deploy_mainnet.py --action set-root --app-id APP --merkle-json proofs.json
    python deploy_mainnet.py --action deploy-budget-helper
    python deploy_mainnet.py --action claim-preseed --app-id APP --merkle-json proofs.json --wallet ADDR
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from algosdk import account, encoding, mnemonic, transaction
from algosdk.abi import Method
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
    TransactionWithSigner,
)
from algosdk.v2client import algod

# ── Config ────────────────────────────────────────────────────────────

TEAL_DIR = Path("smart_contracts/fry_miner_reward_pool")
APPROVAL_TEAL = TEAL_DIR / "FryMinerRewardPool.approval.teal"
CLEAR_TEAL = TEAL_DIR / "FryMinerRewardPool.clear.teal"

TFRY_ID = 2681521901
FNODE_ID = 2485202024
FEE_BPS = 3000
MATURATION_EPOCHS = 4

AUTHORITY_ADDR = "HXWYLLZDPTM5OXS3DPARMTG52RSBMMCQNKT4L2LZRRXYPNAWJBT6VIW6WU"
FEE_ADDR = "AM53XSHRSSSZMNFAMKVAJFXHPMIYYUUBOVCODJ2LQY3D27CVXAHAPIXYXQ"

# Set after deploying via --action deploy-budget-helper.
# claim_preseed's 12-round SHA256 Merkle proof verification exceeds the 700
# per-txn opcode budget. A noop app call in the same group pools +700 budget.
BUDGET_APP_ID = 0

# ABI methods
METHODS = {
    "create": Method.from_signature("create(uint64,uint64,address,uint64,uint64)void"),
    "opt_in_assets": Method.from_signature("opt_in_assets(uint64,uint64,pay)void"),
    "fund_pool": Method.from_signature("fund_pool(axfer)void"),
    "set_merkle_root": Method.from_signature("set_merkle_root(byte[])void"),
    "claim_preseed": Method.from_signature(
        "claim_preseed(uint64,uint64,byte[],uint64,pay,uint64,uint64)void"
    ),
    "get_wallet_state": Method.from_signature(
        "get_wallet_state(address)(uint64,uint64,uint64,uint64,uint64,uint64,uint64,uint64)"
    ),
}


def app_address(app_id: int) -> str:
    return encoding.encode_address(
        encoding.checksum(b"appID" + app_id.to_bytes(8, "big"))
    )


def get_client(args) -> algod.AlgodClient:
    token = args.algod_token or os.environ.get("ALGOD_TOKEN", "")
    return algod.AlgodClient(token, args.algod_url)


def get_signer(args):
    mnemonic_val = os.environ.get(args.mnemonic_env, "")
    if not mnemonic_val:
        print(f"ERROR: {args.mnemonic_env} env var not set")
        sys.exit(1)
    sk = mnemonic.to_private_key(mnemonic_val)
    derived_addr = account.address_from_private_key(sk)
    print(f"Signer address (rekey): {derived_addr}")
    print(f"Authority address (sender): {AUTHORITY_ADDR}")
    return sk, AccountTransactionSigner(sk)


# ── Actions ───────────────────────────────────────────────────────────


def action_deploy(client, signer_sk, signer, args):
    """Deploy the contract to mainnet."""
    print("\n=== DEPLOYING CONTRACT ===")

    # Compile TEAL
    approval_src = APPROVAL_TEAL.read_text()
    clear_src = CLEAR_TEAL.read_text()
    approval_result = client.compile(approval_src)
    clear_result = client.compile(clear_src)
    approval_bytes = base64.b64decode(approval_result["result"])
    clear_bytes = base64.b64decode(clear_result["result"])
    print(f"Approval: {len(approval_bytes)} bytes, Clear: {len(clear_bytes)} bytes")

    extra_pages = max(0, (len(approval_bytes) - 2048 + 2047) // 2048)
    print(f"Extra pages: {extra_pages}")

    # Build ARC-4 create
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 3000

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=0,
        method=METHODS["create"],
        sender=AUTHORITY_ADDR,
        sp=sp,
        signer=signer,
        method_args=[TFRY_ID, FNODE_ID, FEE_ADDR, FEE_BPS, MATURATION_EPOCHS],
        approval_program=approval_bytes,
        clear_program=clear_bytes,
        global_schema=transaction.StateSchema(num_uints=10, num_byte_slices=3),
        local_schema=transaction.StateSchema(num_uints=0, num_byte_slices=0),
        extra_pages=extra_pages,
        on_complete=transaction.OnComplete.NoOpOC,
    )

    result = atc.execute(client, 4)
    txid = result.tx_ids[0]
    txinfo = client.pending_transaction_info(txid)
    new_app_id = txinfo["application-index"]
    new_app_addr = app_address(new_app_id)

    print(f"\nDEPLOYED:")
    print(f"  App ID:      {new_app_id}")
    print(f"  App address: {new_app_addr}")
    print(f"  Txn ID:      {txid}")
    return new_app_id


def action_opt_in(client, signer_sk, signer, args):
    """Opt contract into tFRY and fNODE ASAs."""
    print("\n=== OPT-IN ASSETS ===")
    app_addr = app_address(args.app_id)

    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 3000  # Cover 2 inner txns

    mbr_pay = transaction.PaymentTxn(
        sender=AUTHORITY_ADDR,
        sp=client.suggested_params(),
        receiver=app_addr,
        amt=300_000,  # MBR for 2 ASA opt-ins
    )

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=args.app_id,
        method=METHODS["opt_in_assets"],
        sender=AUTHORITY_ADDR,
        sp=sp,
        signer=signer,
        method_args=[TFRY_ID, FNODE_ID, TransactionWithSigner(mbr_pay, signer)],
        foreign_assets=[TFRY_ID, FNODE_ID],
    )

    result = atc.execute(client, 4)
    print(f"  ASA opt-ins complete. Txn: {result.tx_ids[0]}")


def action_fund(client, signer_sk, signer, args):
    """Fund the contract pool with tFRY or fNODE."""
    asset_id = TFRY_ID if args.asset == "tfry" else FNODE_ID
    asset_name = "tFRY" if args.asset == "tfry" else "fNODE"
    amount = args.amount

    print(f"\n=== FUND POOL: {amount / 1_000_000:,.2f} {asset_name} ===")
    app_addr = app_address(args.app_id)

    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 2000

    axfer = transaction.AssetTransferTxn(
        sender=AUTHORITY_ADDR,
        sp=client.suggested_params(),
        receiver=app_addr,
        amt=amount,
        index=asset_id,
    )

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=args.app_id,
        method=METHODS["fund_pool"],
        sender=AUTHORITY_ADDR,
        sp=sp,
        signer=signer,
        method_args=[TransactionWithSigner(axfer, signer)],
        foreign_assets=[TFRY_ID, FNODE_ID],
    )

    result = atc.execute(client, 4)
    print(f"  Funded. Txn: {result.tx_ids[0]}")


def action_verify(client, args):
    """Verify contract state and balances."""
    print(f"\n=== VERIFY CONTRACT {args.app_id} ===")
    app_addr = app_address(args.app_id)

    # Global state
    app_info = client.application_info(args.app_id)
    gs = {}
    for kv in app_info["params"]["global-state"]:
        key = base64.b64decode(kv["key"]).decode("utf-8", errors="replace")
        val = kv["value"]
        if val["type"] == 2:  # uint
            gs[key] = val["uint"]
        else:
            gs[key] = base64.b64decode(val.get("bytes", "")).hex()

    print("Global state:")
    for k, v in sorted(gs.items()):
        print(f"  {k}: {v}")

    # Pool balances
    acct = client.account_info(app_addr)
    print(f"\nApp ALGO: {acct['amount'] / 1_000_000:.2f}")
    for a in acct.get("assets", []):
        if a["asset-id"] == TFRY_ID:
            print(f"App tFRY: {a['amount'] / 1_000_000:,.2f}")
        elif a["asset-id"] == FNODE_ID:
            print(f"App fNODE: {a['amount'] / 1_000_000:,.2f}")


def action_set_root(client, signer_sk, signer, args):
    """Set the Merkle root for preseed claims."""
    print(f"\n=== SET MERKLE ROOT ===")
    if not args.merkle_json:
        print("ERROR: --merkle-json required")
        sys.exit(1)

    with open(args.merkle_json) as f:
        data = json.load(f)

    root_hex = data["root"]
    wallet_count = len(data.get("wallets", {}))
    root_bytes = bytes.fromhex(root_hex)
    assert len(root_bytes) == 32, f"root must be 32 bytes, got {len(root_bytes)}"

    print(f"  Root: {root_hex}")
    print(f"  Wallets: {wallet_count}")

    # Pass raw bytes — algosdk ATC auto-encodes byte[] with length prefix.
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 2000

    atc = AtomicTransactionComposer()
    atc.add_method_call(
        app_id=args.app_id,
        method=METHODS["set_merkle_root"],
        sender=AUTHORITY_ADDR,
        sp=sp,
        signer=signer,
        method_args=[root_bytes],
    )

    result = atc.execute(client, 4)
    print(f"  Merkle root set. Txn: {result.tx_ids[0]}")


def action_preflight(client, args):
    """Check authority wallet balances vs requirements."""
    print(f"\n=== BALANCE PRE-FLIGHT ===")
    info = client.account_info(AUTHORITY_ADDR)
    algo = info["amount"]
    tfry_bal = 0
    fnode_bal = 0
    for a in info.get("assets", []):
        if a["asset-id"] == TFRY_ID:
            tfry_bal = a["amount"]
        elif a["asset-id"] == FNODE_ID:
            fnode_bal = a["amount"]

    req_algo = 150_000_000  # 150 ALGO
    req_tfry = args.required_tfry
    req_fnode = args.required_fnode

    print(f"Authority: {AUTHORITY_ADDR}")
    print(f"  ALGO:  {algo / 1_000_000:,.2f}  (need {req_algo / 1_000_000:,.2f})")
    print(f"  tFRY:  {tfry_bal / 1_000_000:,.2f}  (need {req_tfry / 1_000_000:,.2f})")
    print(f"  fNODE: {fnode_bal / 1_000_000:,.2f}  (need {req_fnode / 1_000_000:,.2f})")

    ok = True
    if algo < req_algo:
        print(f"  SHORTFALL ALGO: {(req_algo - algo) / 1_000_000:,.2f}")
        ok = False
    if tfry_bal < req_tfry:
        print(f"  SHORTFALL tFRY: {(req_tfry - tfry_bal) / 1_000_000:,.2f}")
        ok = False
    if fnode_bal < req_fnode:
        print(f"  SHORTFALL fNODE: {(req_fnode - fnode_bal) / 1_000_000:,.2f}")
        ok = False

    if ok:
        print("  SUFFICIENT")
    else:
        print("  HARD STOP: wallet underfunded")
        sys.exit(1)


# ── Budget Helper & Claim ────────────────────────────────────────────


def action_deploy_budget_helper(client, signer_sk, signer, args):
    """Deploy a trivial noop app for opcode budget padding.

    claim_preseed's 12-round SHA256 Merkle proof verification costs ~750
    opcodes, exceeding the 700 per-txn budget. Including a noop app call
    in the same atomic group pools an extra 700 budget (total 1400).
    Deploy this once on mainnet, then set BUDGET_APP_ID in this file.
    """
    print("\n=== DEPLOY BUDGET HELPER ===")
    approval = client.compile("#pragma version 10\nint 1")
    clear = client.compile("#pragma version 10\nint 1")
    approval_bytes = base64.b64decode(approval["result"])
    clear_bytes = base64.b64decode(clear["result"])

    sp = client.suggested_params()
    txn = transaction.ApplicationCreateTxn(
        sender=AUTHORITY_ADDR,
        sp=sp,
        on_complete=transaction.OnComplete.NoOpOC,
        approval_program=approval_bytes,
        clear_program=clear_bytes,
        global_schema=transaction.StateSchema(0, 0),
        local_schema=transaction.StateSchema(0, 0),
    )
    stxn = txn.sign(signer_sk)
    txid = client.send_transaction(stxn)
    info = transaction.wait_for_confirmation(client, txid, 4)
    budget_app_id = info["application-index"]

    print(f"  App ID:  {budget_app_id}")
    print(f"  Txn ID:  {txid}")
    print(f"\n  Next: update BUDGET_APP_ID = {budget_app_id} in deploy_mainnet.py")
    return budget_app_id


def action_claim_preseed(client, signer_sk, signer, args):
    """Claim preseed rewards for a wallet using Merkle proof.

    Reads proof data from --merkle-json, claims for --wallet.
    Includes a budget-padding noop app call (claim_preseed's 12-round
    SHA256 loop exceeds 700 opcode budget without it).
    """
    print("\n=== CLAIM PRESEED ===")
    if not args.merkle_json:
        print("ERROR: --merkle-json required")
        sys.exit(1)
    if not args.wallet:
        print("ERROR: --wallet required")
        sys.exit(1)

    budget_app_id = args.budget_app_id or BUDGET_APP_ID
    if budget_app_id == 0:
        print("ERROR: --budget-app-id required (or set BUDGET_APP_ID constant)")
        sys.exit(1)

    with open(args.merkle_json) as f:
        data = json.load(f)

    wallet_addr = args.wallet
    wallet_data = data.get("wallets", {}).get(wallet_addr)
    if not wallet_data:
        print(f"ERROR: wallet {wallet_addr} not found in Merkle JSON")
        sys.exit(1)

    e_tfry = wallet_data["entitled_tfry"]
    e_fnode = wallet_data["entitled_fnode"]
    leaf_index = wallet_data["leaf_index"]
    proof_bytes = bytes.fromhex(wallet_data["proof"])
    assert len(proof_bytes) == 384, f"proof must be 384 bytes, got {len(proof_bytes)}"

    app_addr = app_address(args.app_id)
    wallet_bytes = encoding.decode_address(wallet_addr)

    print(f"  Wallet:     {wallet_addr}")
    print(f"  tFRY:       {e_tfry / 1_000_000:,.2f}")
    print(f"  fNODE:      {e_fnode / 1_000_000:,.2f}")
    print(f"  Leaf index: {leaf_index}")
    print(f"  Budget app: {budget_app_id}")

    # Budget-padding noop call (pools +700 opcode budget)
    sp_noop = client.suggested_params()
    sp_noop.flat_fee = True
    sp_noop.fee = 1000
    noop_txn = transaction.ApplicationCallTxn(
        sender=AUTHORITY_ADDR,
        sp=sp_noop,
        index=budget_app_id,
        on_complete=transaction.OnComplete.NoOpOC,
    )

    # MBR payment for box creation
    mbr_pay = transaction.PaymentTxn(
        sender=AUTHORITY_ADDR,
        sp=client.suggested_params(),
        receiver=app_addr,
        amt=100_000,
    )

    # claim_preseed app call (fee covers 2 inner asset transfers)
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 3000

    atc = AtomicTransactionComposer()
    atc.add_transaction(TransactionWithSigner(noop_txn, signer))
    atc.add_method_call(
        app_id=args.app_id,
        method=METHODS["claim_preseed"],
        sender=AUTHORITY_ADDR,
        sp=sp,
        signer=signer,
        method_args=[
            e_tfry,
            e_fnode,
            proof_bytes,
            leaf_index,
            TransactionWithSigner(mbr_pay, signer),
            TFRY_ID,
            FNODE_ID,
        ],
        foreign_assets=[TFRY_ID, FNODE_ID],
        boxes=[(args.app_id, b"w" + wallet_bytes)],
    )

    result = atc.execute(client, 4)
    print(f"  Claimed. Txn: {result.tx_ids[0]}")


# ── Main ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Deploy FryMinerRewardPool to mainnet")
    parser.add_argument("--action", required=True, choices=[
        "deploy", "opt-in", "fund", "verify", "preflight",
        "set-root", "deploy-budget-helper", "claim-preseed",
    ])
    parser.add_argument("--app-id", type=int, default=0)
    parser.add_argument("--algod-url", default="http://100.69.195.100:4190")
    parser.add_argument("--algod-token", default="")
    parser.add_argument("--mnemonic-env", default="AUTHORITY_MNEMONIC")
    parser.add_argument("--asset", choices=["tfry", "fnode"], help="Asset for fund action")
    parser.add_argument("--amount", type=int, default=0, help="Amount in microunits for fund action")
    parser.add_argument("--required-tfry", type=int, default=0, help="Required tFRY for preflight")
    parser.add_argument("--required-fnode", type=int, default=0, help="Required fNODE for preflight")
    parser.add_argument("--merkle-json", default="", help="Path to Merkle proofs JSON")
    parser.add_argument("--wallet", default="", help="Wallet address (for claim-preseed)")
    parser.add_argument("--budget-app-id", type=int, default=0, help="Budget helper app ID")
    args = parser.parse_args()

    client = get_client(args)
    status = client.status()
    print(f"Algod round: {status['last-round']}")

    if args.action == "verify":
        action_verify(client, args)
        return

    if args.action == "preflight":
        action_preflight(client, args)
        return

    signer_sk, signer = get_signer(args)

    if args.action == "deploy":
        app_id = action_deploy(client, signer_sk, signer, args)
        print(f"\nNext: python deploy_mainnet.py --action opt-in --app-id {app_id}")

    elif args.action == "opt-in":
        if args.app_id == 0:
            print("ERROR: --app-id required")
            sys.exit(1)
        action_opt_in(client, signer_sk, signer, args)

    elif args.action == "set-root":
        if args.app_id == 0:
            print("ERROR: --app-id required")
            sys.exit(1)
        action_set_root(client, signer_sk, signer, args)

    elif args.action == "fund":
        if args.app_id == 0 or not args.asset or args.amount <= 0:
            print("ERROR: --app-id, --asset, --amount required")
            sys.exit(1)
        action_fund(client, signer_sk, signer, args)

    elif args.action == "deploy-budget-helper":
        action_deploy_budget_helper(client, signer_sk, signer, args)

    elif args.action == "claim-preseed":
        if args.app_id == 0:
            print("ERROR: --app-id required")
            sys.exit(1)
        action_claim_preseed(client, signer_sk, signer, args)


if __name__ == "__main__":
    main()

"""Pre-seed old claimable rewards into the new contract as epoch 0.

Reads old device-rewards collection, sums claimable amounts per wallet,
publishes as initial entitled with matured = entitled (all immediately matured
since old rewards already passed the 30-day maturation period).

Two-phase workflow (ARES00 can't reach ATLAS00 algod):
  Phase A (ARES00):  python -m calculator.preseed_old_rewards --export preseed.json --mongo-uri ...
  Phase B (FryStation): python -m calculator.preseed_old_rewards --import-publish preseed.json --app-id X ...

Single-machine:
  python -m calculator.preseed_old_rewards --app-id X --mongo-uri ... --mnemonic-env AUTHORITY_MNEMONIC
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

from pymongo import MongoClient

from .config import FNODE_ASA_ID, MICROUNITS, TFRY_ASA_ID

log = logging.getLogger(__name__)

PUBLISH_METHOD_SIG = "publish_rewards(address,uint64,uint64,uint64,uint64,uint64,pay)void"


# ── Data loading ────────────────────────────────────────────────────────


def load_claimable_rewards(db_main) -> list[dict[str, Any]]:
    """Load device-rewards docs that have any claimable amounts."""
    query = {
        "$or": [
            {"daily_rewards.status": "claimable"},
            {"weekly_rewards.status": "claimable"},
            {"total_claimable": {"$gt": 0}},
        ]
    }
    docs = list(db_main["device-rewards"].find(query))
    log.info("Found %d device-rewards docs with claimable amounts", len(docs))
    return docs


def sum_claimable_by_asset(doc: dict) -> dict[str, float]:
    """Sum claimable amounts from daily_rewards and weekly_rewards, grouped by asset_id."""
    totals: dict[str, float] = defaultdict(float)

    for entry in doc.get("daily_rewards", []):
        if entry.get("status") == "claimable":
            asset_id = entry.get("asset_id", "")
            amount = entry.get("amount", 0)
            if asset_id and amount > 0:
                totals[asset_id] += amount

    for entry in doc.get("weekly_rewards", []):
        if entry.get("status") == "claimable":
            asset_id = entry.get("asset_id", "")
            amount = entry.get("amount", 0)
            if asset_id and amount > 0:
                totals[asset_id] += amount

    return dict(totals)


def resolve_wallet(miner_key: str, devices_by_key: dict) -> str:
    """Resolve reward wallet for a device. Falls back to owner address."""
    device = devices_by_key.get(miner_key, {})
    wallet = device.get("reward_wallet") or ""
    if not wallet.strip():
        wallet = device.get("address") or ""
    return wallet.strip()


def miner_code_from_key(miner_key: str) -> str:
    return miner_key.split("-", 1)[0] if "-" in miner_key else ""


# ── fNODE policy ────────────────────────────────────────────────────────


def should_zero_fnode(
    miner_key: str,
    policy: str,
    policy_args: str,
    poc_hw: dict,
    device: dict,
    now: datetime,
) -> bool:
    """Return True if this device's fNODE should be zeroed per policy."""
    if policy == "all":
        return False

    code = miner_code_from_key(miner_key)

    if policy == "poc-only":
        hw = poc_hw.get(miner_key)
        return not (hw and hw.get("reward_eligible", False))

    if policy == "poc-active-90d":
        hw = poc_hw.get(miner_key)
        if not (hw and hw.get("reward_eligible", False)):
            return True
        last_active = device.get("last_seen") or device.get("updated_at")
        if last_active and isinstance(last_active, datetime):
            if last_active.tzinfo is None:
                last_active = last_active.replace(tzinfo=timezone.utc)
            if last_active < now - timedelta(days=90):
                return True
        return False

    if policy == "zero-codes":
        blocked = {c.strip() for c in (policy_args or "").split(",")}
        return code in blocked

    return False


# ── Computation ─────────────────────────────────────────────────────────


def compute_preseed(
    db_main,
    db_poc=None,
    fnode_policy: str = "all",
    fnode_policy_args: str = "",
    fnode_cap: float = 0,
) -> dict[str, dict[str, int]]:
    """Compute pre-seed amounts per wallet.

    Returns {wallet: {"tfry": microunits, "fnode": microunits}}.
    """
    claimable_docs = load_claimable_rewards(db_main)

    # Load device lookup for wallet resolution
    devices_by_key = {}
    for d in db_main.devices.find({}):
        mk = d.get("miner_key", "")
        if mk:
            devices_by_key[mk] = d
    log.info("Loaded %d devices for wallet resolution", len(devices_by_key))

    # Load PoC hardware if needed for policy
    poc_hw = {}
    if fnode_policy in ("poc-only", "poc-active-90d") and db_poc is not None:
        for p in db_poc.hardware.find({}):
            mk = p.get("miner_key", "")
            if mk:
                poc_hw[mk] = p
        log.info("Loaded %d PoC hardware docs for fNODE policy", len(poc_hw))

    now = datetime.now(timezone.utc)
    wallet_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"tfry": 0.0, "fnode": 0.0})
    skipped = {"no_wallet": 0, "no_claimable": 0, "fnode_zeroed": 0}

    for doc in claimable_docs:
        miner_key = doc.get("miner_key", "")
        wallet = resolve_wallet(miner_key, devices_by_key)
        if not wallet:
            skipped["no_wallet"] += 1
            continue

        by_asset = sum_claimable_by_asset(doc)
        if not by_asset:
            skipped["no_claimable"] += 1
            continue

        # Apply fNODE policy at device level
        if FNODE_ASA_ID in by_asset and fnode_policy != "all":
            device = devices_by_key.get(miner_key, {})
            if should_zero_fnode(miner_key, fnode_policy, fnode_policy_args, poc_hw, device, now):
                skipped["fnode_zeroed"] += 1
                del by_asset[FNODE_ASA_ID]

        for asset_id, amount in by_asset.items():
            if asset_id == TFRY_ASA_ID:
                wallet_totals[wallet]["tfry"] += amount
            elif asset_id == FNODE_ASA_ID:
                wallet_totals[wallet]["fnode"] += amount

    # Apply fNODE cap if specified
    if fnode_cap > 0:
        total_fnode_dec = sum(t["fnode"] for t in wallet_totals.values())
        if total_fnode_dec > fnode_cap:
            ratio = fnode_cap / total_fnode_dec
            log.info("Applying fNODE cap: %.2f → %.2f (ratio %.4f)", total_fnode_dec, fnode_cap, ratio)
            for wt in wallet_totals.values():
                wt["fnode"] *= ratio

    # Convert to microunits
    result = {}
    for wallet, totals in wallet_totals.items():
        tfry_micro = math.floor(totals["tfry"] * MICROUNITS)
        fnode_micro = math.floor(totals["fnode"] * MICROUNITS)
        if tfry_micro > 0 or fnode_micro > 0:
            result[wallet] = {"tfry": tfry_micro, "fnode": fnode_micro}

    log.info(
        "Pre-seed: %d wallets (skipped: %d no_wallet, %d no_claimable, %d fnode_zeroed)",
        len(result), skipped["no_wallet"], skipped["no_claimable"], skipped["fnode_zeroed"],
    )
    return result


# ── JSON export/import ──────────────────────────────────────────────────


def export_json(wallet_amounts: dict, path: str) -> None:
    with open(path, "w") as f:
        json.dump(wallet_amounts, f, indent=2)
    log.info("Exported %d wallets to %s", len(wallet_amounts), path)


def import_json(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    log.info("Imported %d wallets from %s", len(data), path)
    return data


# ── Contract publishing ─────────────────────────────────────────────────


def publish_to_contract(
    algod_url: str,
    algod_token: str,
    app_id: int,
    authority_addr: str,
    signer_sk: str,
    wallet_amounts: dict[str, dict[str, int]],
) -> dict:
    """Publish preseed to mainnet contract. Returns stats."""
    from algosdk import encoding, transaction
    from algosdk.abi import Method
    from algosdk.atomic_transaction_composer import (
        AccountTransactionSigner,
        AtomicTransactionComposer,
        TransactionWithSigner,
    )
    from algosdk.v2client import algod as algod_module

    client = algod_module.AlgodClient(algod_token, algod_url)
    status = client.status()
    log.info("Connected to algod. Round: %d", status["last-round"])

    method = Method.from_signature(PUBLISH_METHOD_SIG)
    signer = AccountTransactionSigner(signer_sk)
    app_addr = encoding.encode_address(
        encoding.checksum(b"appID" + app_id.to_bytes(8, "big"))
    )

    total = len(wallet_amounts)
    published = 0
    failed = []
    total_tfry = 0
    total_fnode = 0

    sorted_wallets = sorted(wallet_amounts.items())

    for i, (wallet, amounts) in enumerate(sorted_wallets):
        tfry = amounts["tfry"]
        fnode = amounts["fnode"]

        try:
            sp = client.suggested_params()
            sp.flat_fee = True
            sp.fee = 2000  # Cover outer + potential inner

            # MBR payment for box creation
            mbr_pay = transaction.PaymentTxn(
                sender=authority_addr,
                sp=client.suggested_params(),
                receiver=app_addr,
                amt=100_000,  # Generous MBR for box creation
            )

            box_key = b"w" + encoding.decode_address(wallet)

            atc = AtomicTransactionComposer()
            atc.add_method_call(
                app_id=app_id,
                method=method,
                sender=authority_addr,
                sp=sp,
                signer=signer,
                method_args=[
                    wallet,       # wallet address
                    tfry,         # entitled_tfry
                    fnode,        # entitled_fnode
                    tfry,         # matured_tfry = entitled (all matured for preseed)
                    fnode,        # matured_fnode = entitled
                    0,            # epoch = 0 (preseed)
                    TransactionWithSigner(mbr_pay, signer),
                ],
                accounts=[wallet],
                boxes=[(app_id, box_key)],
            )

            atc.execute(client, 4)
            published += 1
            total_tfry += tfry
            total_fnode += fnode

        except Exception as e:
            failed.append({"wallet": wallet, "error": str(e)})
            log.error("[%d/%d] FAILED %s: %s", i + 1, total, wallet[:12], e)

        if (i + 1) % 50 == 0 or i + 1 == total:
            log.info(
                "[%d/%d] published=%d failed=%d tFRY=%s fNODE=%s",
                i + 1, total, published, len(failed),
                f"{total_tfry / MICROUNITS:,.2f}",
                f"{total_fnode / MICROUNITS:,.2f}",
            )

    return {
        "total": total,
        "published": published,
        "failed": failed,
        "total_tfry": total_tfry,
        "total_fnode": total_fnode,
    }


# ── MongoDB logging ─────────────────────────────────────────────────────


def save_preseed_log(
    db_main,
    wallet_amounts: dict[str, dict[str, int]],
    dry_run: bool,
    now: datetime,
) -> None:
    """Write pre-seed migration log to MongoDB."""
    total_tfry = sum(w["tfry"] for w in wallet_amounts.values())
    total_fnode = sum(w["fnode"] for w in wallet_amounts.values())

    db_main.reward_preseed_log.insert_one({
        "wallet_count": len(wallet_amounts),
        "total_tfry_micro": total_tfry,
        "total_fnode_micro": total_fnode,
        "total_tfry_decimal": total_tfry / MICROUNITS,
        "total_fnode_decimal": total_fnode / MICROUNITS,
        "dry_run": dry_run,
        "computed_at": now,
    })


# ── CLI ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-seed old claimable rewards into new contract")
    parser.add_argument("--dry-run", action="store_true", help="Compute + display only")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:27017/?tls=true&tlsAllowInvalidCertificates=true")
    parser.add_argument("--app-id", type=int, default=0, help="FryMinerRewardPool app ID")
    parser.add_argument("--algod-url", default="http://100.69.195.100:4190")
    parser.add_argument("--algod-token", default="", help="Algod API token")
    parser.add_argument("--mnemonic-env", default="AUTHORITY_MNEMONIC", help="Env var containing signing mnemonic")
    parser.add_argument("--authority-address", default="", help="Authority sender address (for rekeyed wallets)")

    # Two-phase workflow
    parser.add_argument("--export", metavar="PATH", help="Export computed wallet amounts to JSON (no algod needed)")
    parser.add_argument("--import-publish", metavar="PATH", help="Import JSON and publish to contract (no MongoDB needed)")

    # fNODE policy
    parser.add_argument("--fnode-policy", default="all",
                        choices=["all", "poc-only", "poc-active-90d", "zero-codes", "cap"],
                        help="fNODE filter policy")
    parser.add_argument("--fnode-policy-args", default="", help="Policy args (codes for zero-codes, amount for cap)")

    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    now = datetime.now(timezone.utc)

    # ── Mode: import-publish (no MongoDB needed) ──
    if args.import_publish:
        log.info("Import-publish mode: reading %s", args.import_publish)
        wallet_amounts = import_json(args.import_publish)

        if args.dry_run:
            total_tfry = sum(w["tfry"] for w in wallet_amounts.values())
            total_fnode = sum(w["fnode"] for w in wallet_amounts.values())
            print(f"\n=== IMPORT DRY RUN ===")
            print(f"Wallets: {len(wallet_amounts)}")
            print(f"Total tFRY: {total_tfry / MICROUNITS:,.2f}")
            print(f"Total fNODE: {total_fnode / MICROUNITS:,.2f}")
            return

        if args.app_id == 0:
            log.error("--app-id required for publish")
            sys.exit(1)

        mnemonic_val = os.environ.get(args.mnemonic_env, "")
        if not mnemonic_val:
            log.error("Mnemonic env var %s not set", args.mnemonic_env)
            sys.exit(1)

        from algosdk import mnemonic
        signer_sk = mnemonic.to_private_key(mnemonic_val)
        authority_addr = args.authority_address
        if not authority_addr:
            from algosdk import account
            authority_addr = account.address_from_private_key(signer_sk)
            log.info("Derived authority address: %s", authority_addr)

        algod_token = args.algod_token or os.environ.get("ALGOD_TOKEN", "")

        stats = publish_to_contract(
            args.algod_url, algod_token, args.app_id,
            authority_addr, signer_sk, wallet_amounts,
        )

        print(f"\n=== PRESEED PUBLISH COMPLETE ===")
        print(f"Published: {stats['published']} / {stats['total']}")
        print(f"Failed: {len(stats['failed'])}")
        print(f"Total tFRY: {stats['total_tfry'] / MICROUNITS:,.2f}")
        print(f"Total fNODE: {stats['total_fnode'] / MICROUNITS:,.2f}")
        if stats["failed"]:
            print("\nFailed wallets:")
            for f in stats["failed"]:
                print(f"  {f['wallet']}: {f['error'][:80]}")
        return

    # ── Mode: compute from MongoDB ──
    log.info("Starting pre-seed migration (dry_run=%s)", args.dry_run)

    client = MongoClient(args.mongo_uri)
    db_main = client["main"]
    db_poc = client["PoC"] if args.fnode_policy in ("poc-only", "poc-active-90d") else None

    fnode_cap = 0.0
    if args.fnode_policy == "cap" and args.fnode_policy_args:
        fnode_cap = float(args.fnode_policy_args)

    wallet_amounts = compute_preseed(
        db_main,
        db_poc=db_poc,
        fnode_policy=args.fnode_policy,
        fnode_policy_args=args.fnode_policy_args,
        fnode_cap=fnode_cap,
    )

    total_tfry = sum(w["tfry"] for w in wallet_amounts.values())
    total_fnode = sum(w["fnode"] for w in wallet_amounts.values())

    print(f"\n=== PRE-SEED {'DRY RUN ' if args.dry_run else ''}SUMMARY ===")
    print(f"fNODE policy: {args.fnode_policy}" + (f" ({args.fnode_policy_args})" if args.fnode_policy_args else ""))
    print(f"Wallets: {len(wallet_amounts)}")
    print(f"Total tFRY: {total_tfry / MICROUNITS:,.2f} ({total_tfry} microunits)")
    print(f"Total fNODE: {total_fnode / MICROUNITS:,.2f} ({total_fnode} microunits)")

    # Top 10 wallets
    sorted_wallets = sorted(
        wallet_amounts.items(),
        key=lambda x: x[1]["tfry"] + x[1]["fnode"],
        reverse=True,
    )
    if sorted_wallets:
        print("\nTop 10 wallets:")
        for wallet, amounts in sorted_wallets[:10]:
            tfry_dec = amounts["tfry"] / MICROUNITS
            fnode_dec = amounts["fnode"] / MICROUNITS
            print(f"  {wallet[:8]}...{wallet[-4:]}: tFRY={tfry_dec:,.2f}  fNODE={fnode_dec:,.2f}")

    # Export mode
    if args.export:
        export_json(wallet_amounts, args.export)
        print(f"\nExported to {args.export}")
        client.close()
        return

    # Live publish mode
    if not args.dry_run:
        if args.app_id == 0:
            log.error("--app-id required for live publish")
            sys.exit(1)

        save_preseed_log(db_main, wallet_amounts, False, now)

        mnemonic_val = os.environ.get(args.mnemonic_env, "")
        if not mnemonic_val:
            log.error("Mnemonic env var %s not set", args.mnemonic_env)
            sys.exit(1)

        from algosdk import mnemonic
        signer_sk = mnemonic.to_private_key(mnemonic_val)
        authority_addr = args.authority_address
        if not authority_addr:
            from algosdk import account
            authority_addr = account.address_from_private_key(signer_sk)

        algod_token = args.algod_token or os.environ.get("ALGOD_TOKEN", "")

        stats = publish_to_contract(
            args.algod_url, algod_token, args.app_id,
            authority_addr, signer_sk, wallet_amounts,
        )

        print(f"\n=== PRESEED PUBLISH COMPLETE ===")
        print(f"Published: {stats['published']} / {stats['total']}")
        print(f"Failed: {len(stats['failed'])}")
        if stats["failed"]:
            print("\nFailed wallets:")
            for f in stats["failed"]:
                print(f"  {f['wallet']}: {f['error'][:80]}")

    client.close()


if __name__ == "__main__":
    main()

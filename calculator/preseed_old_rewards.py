"""Pre-seed old claimable rewards into the new contract as epoch 0.

Reads old device-rewards collection, sums claimable amounts per wallet,
publishes as initial entitled with matured = entitled (all immediately matured
since old rewards already passed the 30-day maturation period).

Usage:
    python -m calculator.preseed_old_rewards --dry-run --mongo-uri "mongodb://..."
"""

from __future__ import annotations

import argparse
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

from .config import FNODE_ASA_ID, MICROUNITS, TFRY_ASA_ID

log = logging.getLogger(__name__)


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


def compute_preseed(
    db_main,
) -> dict[str, dict[str, int]]:
    """Compute pre-seed amounts per wallet.

    Returns {wallet: {"tfry": microunits, "fnode": microunits}}.
    """
    claimable_docs = load_claimable_rewards(db_main)

    # Load device lookup for wallet resolution
    devices_by_key = {}
    for d in db_main.devices.find({}, {"miner_key": 1, "reward_wallet": 1, "address": 1}):
        mk = d.get("miner_key", "")
        if mk:
            devices_by_key[mk] = d
    log.info("Loaded %d devices for wallet resolution", len(devices_by_key))

    wallet_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"tfry": 0.0, "fnode": 0.0})
    skipped = {"no_wallet": 0, "no_claimable": 0}

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

        for asset_id, amount in by_asset.items():
            if asset_id == TFRY_ASA_ID:
                wallet_totals[wallet]["tfry"] += amount
            elif asset_id == FNODE_ASA_ID:
                wallet_totals[wallet]["fnode"] += amount
            # Other asset IDs (FRY 1.0 legacy, etc.) — skip for V2 contract

    # Convert to microunits
    result = {}
    for wallet, totals in wallet_totals.items():
        tfry_micro = math.floor(totals["tfry"] * MICROUNITS)
        fnode_micro = math.floor(totals["fnode"] * MICROUNITS)
        if tfry_micro > 0 or fnode_micro > 0:
            result[wallet] = {"tfry": tfry_micro, "fnode": fnode_micro}

    log.info(
        "Pre-seed: %d wallets (skipped: %d no_wallet, %d no_claimable)",
        len(result), skipped["no_wallet"], skipped["no_claimable"],
    )
    return result


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-seed old claimable rewards into new contract")
    parser.add_argument("--dry-run", action="store_true", help="Compute + log only, do NOT publish to contract")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:27017/?tls=true&tlsAllowInvalidCertificates=true", help="MongoDB connection URI")
    parser.add_argument("--app-id", type=int, default=0, help="FryMinerRewardPool app ID")
    parser.add_argument("--algod-url", default="http://100.69.195.100:4190", help="Algod URL")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    now = datetime.now(timezone.utc)
    log.info("Starting pre-seed migration (dry_run=%s)", args.dry_run)

    client = MongoClient(args.mongo_uri)
    db_main = client["main"]

    wallet_amounts = compute_preseed(db_main)
    save_preseed_log(db_main, wallet_amounts, args.dry_run, now)

    total_tfry = sum(w["tfry"] for w in wallet_amounts.values())
    total_fnode = sum(w["fnode"] for w in wallet_amounts.values())

    print(f"\n=== PRE-SEED {'DRY RUN ' if args.dry_run else ''}SUMMARY ===")
    print(f"Wallets: {len(wallet_amounts)}")
    print(f"Total tFRY: {total_tfry / MICROUNITS:,.2f} ({total_tfry} microunits)")
    print(f"Total fNODE: {total_fnode / MICROUNITS:,.2f} ({total_fnode} microunits)")

    if not args.dry_run:
        if args.app_id == 0:
            log.error("--app-id required for live publish")
            return
        log.info("Publishing pre-seed as epoch 0 (matured = entitled) ...")
        # TODO: Phase 3 — implement algod batch publishing
        # For pre-seed: matured = entitled (all immediately matured)
        log.warning("Contract publishing not yet implemented. Use --dry-run for now.")

    # Top 10 wallets by combined amount
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

    client.close()


if __name__ == "__main__":
    main()

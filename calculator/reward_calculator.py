"""Weekly reward calculator — replaces dbrewards.

Reads MongoDB (devices, products, PoC), computes per-device rewards,
aggregates per wallet, optionally publishes to FryMinerRewardPool contract.

Usage:
    python -m calculator.reward_calculator --epoch 1 --dry-run --mongo-uri "mongodb://..."
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

from .config import (
    AEM_CODES,
    ALL_REWARD_CODES,
    BYOD_FACTOR,
    FNODE_ASA_ID,
    INSTALLER_CODES,
    MICROUNITS,
    NODE_CODES,
    STAKING_MULTIPLIERS,
    TFRY_ASA_ID,
    VIRTUAL_NODE_CODES,
)

log = logging.getLogger(__name__)


def round_two(value: float) -> float:
    """Round to 2 decimal places using round-half-up (matches JS Math.round)."""
    return math.floor(value * 100 + 0.5) / 100


# ── Reward computation ───────────────────────────────────────────────


def miner_code_from_key(miner_key: str) -> str:
    """Extract miner code prefix from miner_key (e.g. 'BM-XXXX' → 'BM')."""
    return miner_key.split("-", 1)[0] if "-" in miner_key else ""


def is_device_eligible(device: dict, miner_code: str, now: datetime) -> bool:
    """Check device eligibility. No staking gates — staking only affects multiplier."""
    if not device.get("is_registered"):
        return False

    # Rewards exception overrides
    exc = device.get("rewards_exception")
    if exc and exc.get("enabled"):
        expires = exc.get("expires_at")
        if expires and isinstance(expires, datetime) and expires > now:
            return False
        elif not expires:
            return False

    # Virtual nodes: require activated
    if miner_code in VIRTUAL_NODE_CODES:
        return bool(device.get("activated"))

    # Nodes and AEM: is_registered sufficient (no created_at gate)
    if miner_code in NODE_CODES or miner_code in AEM_CODES:
        return True

    # Regular miners: require created_at <= now
    created_at = device.get("created_at")
    if created_at and isinstance(created_at, datetime):
        return created_at <= now
    return bool(created_at)  # trust if present but not datetime


def compute_base_reward(device: dict, product: dict, now: datetime) -> float:
    """Compute base reward amount with staking multiplier and BYOD reduction."""
    reward_cfg = product.get("reward", {})
    staked = device.get("staked")

    # Staking multiplier check
    has_staking = (
        staked
        and staked.get("type") in STAKING_MULTIPLIERS
        and staked.get("amount", 0) > 0
        and isinstance(staked.get("time"), datetime)
        and staked["time"] <= now
    )

    if has_staking:
        base = reward_cfg.get("verified", 0)
        multiplier = STAKING_MULTIPLIERS[staked["type"]]
        amount = round_two(base * multiplier)
    else:
        amount = reward_cfg.get("unverified", 0)

    # BYOD reduction (50%)
    if device.get("byod"):
        amount = round_two(amount * BYOD_FACTOR)

    return amount


def apply_poc_factor(
    amount: float, miner_code: str, poc_hardware: dict | None
) -> float:
    """Apply PoC slot-based pro-rating for INSTALLER devices."""
    if miner_code not in INSTALLER_CODES:
        return amount  # NON_INSTALLER and virtual: no PoC gating

    if poc_hardware is None:
        return 0.0  # fail-closed: no PoC doc → zero reward

    if not poc_hardware.get("reward_eligible"):
        return 0.0  # fail-closed: not eligible → zero reward

    factor = poc_hardware.get("rewards_multiplier_day", 0.0)
    factor = max(0.0, min(1.0, factor))
    return round_two(amount * factor)


def get_reward_token(product: dict) -> str | None:
    """Get reward token ASA ID from product config. Returns None if no reward."""
    tokens = product.get("reward", {}).get("tokens", {})
    token_id = tokens.get("reward", "")
    if token_id in (TFRY_ASA_ID, FNODE_ASA_ID):
        return token_id
    return None


def compute_device_reward(
    device: dict,
    product: dict,
    poc_hardware: dict | None,
    now: datetime,
) -> tuple[float, str | None]:
    """Compute daily reward for a single device.

    Returns (amount, token_id) or (0.0, None) if ineligible.
    """
    miner_code = miner_code_from_key(device.get("miner_key", ""))

    if miner_code not in ALL_REWARD_CODES:
        return 0.0, None

    if not is_device_eligible(device, miner_code, now):
        return 0.0, None

    token_id = get_reward_token(product)
    if token_id is None:
        return 0.0, None

    amount = compute_base_reward(device, product, now)
    if amount <= 0:
        return 0.0, None

    amount = apply_poc_factor(amount, miner_code, poc_hardware)
    return amount, token_id


# ── Wallet aggregation ───────────────────────────────────────────────


def get_wallet(device: dict) -> str:
    """Get reward destination wallet. Falls back to owner address."""
    wallet = device.get("reward_wallet") or ""
    if not wallet or not wallet.strip():
        wallet = device.get("address") or ""
    return wallet.strip()


def to_microunits(amount: float) -> int:
    """Convert decimal amount to integer microunits (floor)."""
    return math.floor(amount * MICROUNITS)


# ── Main calculator ──────────────────────────────────────────────────


def load_data(db_main, db_poc) -> tuple[dict, list, dict, dict]:
    """Load all data from MongoDB. Returns (products, devices, poc_hardware, poc_versions)."""
    products = {}
    for p in db_main.products.find():
        key = p.get("key", "")
        if key:
            products[key] = p
    log.info("Loaded %d products", len(products))

    devices = list(db_main.devices.find({"is_registered": True}))
    log.info("Loaded %d registered devices", len(devices))

    poc_hardware = {}
    for h in db_poc.hardware.find({}, {"miner_key": 1, "reward_eligible": 1, "rewards_multiplier_day": 1}):
        mk = h.get("miner_key", "")
        if mk:
            poc_hardware[mk] = h
    log.info("Loaded %d PoC hardware docs", len(poc_hardware))

    poc_versions = {}
    for v in db_poc.versions.find():
        mc = v.get("miner_code", "")
        if mc:
            poc_versions[mc] = v
    log.info("Loaded %d PoC version docs", len(poc_versions))

    return products, devices, poc_hardware, poc_versions


def compute_weekly_rewards(
    products: dict,
    devices: list,
    poc_hardware: dict,
    now: datetime,
    days: int = 7,
) -> dict[str, dict[str, int]]:
    """Compute weekly rewards per wallet.

    Returns {wallet: {"tfry": microunits, "fnode": microunits}}.
    """
    wallet_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"tfry": 0.0, "fnode": 0.0})
    stats = {"eligible": 0, "ineligible": 0, "no_product": 0, "zero_reward": 0}

    for device in devices:
        miner_key = device.get("miner_key", "")
        miner_code = miner_code_from_key(miner_key)
        product = products.get(miner_code)

        if product is None:
            stats["no_product"] += 1
            continue

        poc_hw = poc_hardware.get(miner_key)
        daily_amount, token_id = compute_device_reward(device, product, poc_hw, now)

        if daily_amount <= 0 or token_id is None:
            if not is_device_eligible(device, miner_code, now):
                stats["ineligible"] += 1
            else:
                stats["zero_reward"] += 1
            continue

        stats["eligible"] += 1
        weekly_amount = round(daily_amount * days, 2)
        wallet = get_wallet(device)
        if not wallet:
            continue

        token_key = "tfry" if token_id == TFRY_ASA_ID else "fnode"
        wallet_totals[wallet][token_key] += weekly_amount

    # Convert to microunits
    result = {}
    for wallet, totals in wallet_totals.items():
        tfry_micro = to_microunits(totals["tfry"])
        fnode_micro = to_microunits(totals["fnode"])
        if tfry_micro > 0 or fnode_micro > 0:
            result[wallet] = {"tfry": tfry_micro, "fnode": fnode_micro}

    log.info(
        "Computed rewards: %d eligible, %d ineligible, %d no_product, %d zero_reward → %d wallets",
        stats["eligible"], stats["ineligible"], stats["no_product"], stats["zero_reward"], len(result),
    )
    return result


def compute_epoch_publish(
    db_main,
    weekly_rewards: dict[str, dict[str, int]],
    epoch: int,
    maturation_epochs: int = 4,
) -> list[dict[str, Any]]:
    """Compute cumulative entitled + matured values for contract publishing.

    Returns list of {wallet, entitled_tfry, entitled_fnode, matured_tfry, matured_fnode, epoch}.
    """
    history_col = db_main.reward_epoch_history
    publish_records = []

    for wallet, weekly in weekly_rewards.items():
        # Load most recent epoch history for this wallet
        prev = history_col.find_one(
            {"wallet": wallet, "epoch": {"$lt": epoch}},
            sort=[("epoch", -1)],
        )
        prev_tfry = prev["cumulative_tfry"] if prev else 0
        prev_fnode = prev["cumulative_fnode"] if prev else 0

        new_entitled_tfry = prev_tfry + weekly["tfry"]
        new_entitled_fnode = prev_fnode + weekly["fnode"]

        # Matured = what cumulative was N epochs ago
        lookback_epoch = epoch - maturation_epochs
        if lookback_epoch >= 0:
            matured_doc = history_col.find_one({"wallet": wallet, "epoch": lookback_epoch})
            matured_tfry = matured_doc["cumulative_tfry"] if matured_doc else 0
            matured_fnode = matured_doc["cumulative_fnode"] if matured_doc else 0
        else:
            matured_tfry = 0
            matured_fnode = 0

        publish_records.append({
            "wallet": wallet,
            "entitled_tfry": new_entitled_tfry,
            "entitled_fnode": new_entitled_fnode,
            "matured_tfry": matured_tfry,
            "matured_fnode": matured_fnode,
            "epoch": epoch,
        })

    return publish_records


def save_epoch_history(db_main, publish_records: list[dict], now: datetime) -> None:
    """Write epoch history to MongoDB for maturation lookback."""
    history_col = db_main.reward_epoch_history
    docs = []
    for rec in publish_records:
        docs.append({
            "wallet": rec["wallet"],
            "epoch": rec["epoch"],
            "cumulative_tfry": rec["entitled_tfry"],
            "cumulative_fnode": rec["entitled_fnode"],
            "computed_at": now,
        })
    if docs:
        history_col.insert_many(docs)
    log.info("Saved %d epoch history records", len(docs))


def save_publish_log(
    db_main,
    publish_records: list[dict],
    epoch: int,
    dry_run: bool,
    now: datetime,
) -> None:
    """Write publish log to MongoDB."""
    total_tfry = sum(r["entitled_tfry"] - (r.get("prev_tfry", 0)) for r in publish_records)
    total_fnode = sum(r["entitled_fnode"] - (r.get("prev_fnode", 0)) for r in publish_records)
    # Approximate: use weekly amounts from entitled - previous
    db_main.reward_publish_log.insert_one({
        "epoch": epoch,
        "wallet_count": len(publish_records),
        "total_new_tfry": total_tfry,
        "total_new_fnode": total_fnode,
        "dry_run": dry_run,
        "computed_at": now,
    })


# ── CLI ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly reward calculator")
    parser.add_argument("--epoch", type=int, required=True, help="Epoch number for this publish cycle")
    parser.add_argument("--dry-run", action="store_true", help="Compute + log only, do NOT publish to contract")
    parser.add_argument("--mongo-uri", default="mongodb://localhost:27017/?tls=true&tlsAllowInvalidCertificates=true", help="MongoDB connection URI")
    parser.add_argument("--days", type=int, default=7, help="Days in reward period (default: 7)")
    parser.add_argument("--maturation-epochs", type=int, default=4, help="Epochs before fee-free (default: 4)")
    parser.add_argument("--app-id", type=int, default=0, help="FryMinerRewardPool app ID (for publishing)")
    parser.add_argument("--algod-url", default="http://100.69.195.100:4190", help="Algod URL")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    now = datetime.now(timezone.utc)
    log.info("Starting reward calculation for epoch %d (dry_run=%s)", args.epoch, args.dry_run)

    client = MongoClient(args.mongo_uri)
    db_main = client["main"]
    db_poc = client["PoC"]

    products, devices, poc_hardware, poc_versions = load_data(db_main, db_poc)
    weekly_rewards = compute_weekly_rewards(products, devices, poc_hardware, now, args.days)
    publish_records = compute_epoch_publish(db_main, weekly_rewards, args.epoch, args.maturation_epochs)

    save_epoch_history(db_main, publish_records, now)
    save_publish_log(db_main, publish_records, args.epoch, args.dry_run, now)

    log.info("Epoch %d: %d wallets, dry_run=%s", args.epoch, len(publish_records), args.dry_run)

    if not args.dry_run:
        if args.app_id == 0:
            log.error("--app-id required for live publish")
            sys.exit(1)
        log.info("Publishing to contract app_id=%d ...", args.app_id)
        # TODO: Phase 3 — implement algod batch publishing
        log.warning("Contract publishing not yet implemented. Use --dry-run for now.")
    else:
        # Print summary
        total_tfry_new = sum(r["entitled_tfry"] for r in publish_records) - sum(
            (db_main.reward_epoch_history.find_one(
                {"wallet": r["wallet"], "epoch": {"$lt": args.epoch}},
                sort=[("epoch", -1)],
            ) or {}).get("cumulative_tfry", 0)
            for r in publish_records
        )
        total_fnode_new = sum(r["entitled_fnode"] for r in publish_records) - sum(
            (db_main.reward_epoch_history.find_one(
                {"wallet": r["wallet"], "epoch": {"$lt": args.epoch}},
                sort=[("epoch", -1)],
            ) or {}).get("cumulative_fnode", 0)
            for r in publish_records
        )
        print(f"\n=== DRY RUN SUMMARY (Epoch {args.epoch}) ===")
        print(f"Wallets: {len(publish_records)}")
        print(f"New tFRY: {total_tfry_new / MICROUNITS:,.2f}")
        print(f"New fNODE: {total_fnode_new / MICROUNITS:,.2f}")

    client.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Weekly epoch publisher for FryMinerRewardPool.

Self-contained. Runnable standalone or imported by calculator.reward_calculator.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import struct
import sys
try:
    import fcntl
except ImportError:
    fcntl = None
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

from pymongo import MongoClient
from algosdk import account, encoding, logic, mnemonic, transaction
from algosdk.abi import Method
from algosdk.atomic_transaction_composer import (
    AccountTransactionSigner,
    AtomicTransactionComposer,
    TransactionWithSigner,
)
from algosdk.v2client import algod
from algosdk.v2client.models.simulate_request import (
    SimulateRequest,
    SimulateRequestTransactionGroup,
)

# ── Constants ──────────────────────────────────────────────────────────

APP_ID = 3592975326
ADMIN_ADDR = "HXWYLLZDPTM5OXS3DPARMTG52RSBMMCQNKT4L2LZRRXYPNAWJBT6VIW6WU"
TFRY_ID = 2681521901
FNODE_ID = 2485202024
MICROUNITS = 10**6

MBR_PER_NEW_WALLET = 41300
WALLETS_PER_GROUP = 8
MAX_TXNS_PER_GROUP = 16
MAX_GROUPS = 500

PUBLISH_METHOD_SIG = "publish_rewards(address,uint64,uint64,uint64,uint64,uint64,pay)void"
ADVANCE_METHOD_SIG = "advance_epoch()void"

# Caps (as decimal token amounts)
CAP_POOL_TFRY_DEC = 175_802_904.22
CAP_POOL_FNODE_DEC = 213_762_677.82

ALGOD_URL = os.environ.get("ALGOD_URL", "http://100.69.195.100:4190")

log = logging.getLogger(__name__)

_LOCK_FD = None


def acquire_single_launch_lock(lock_path: str) -> None:
    """Exclusive non-blocking lock so two --live runs can't overlap.

    Kernel auto-releases on process exit/death.
    """
    global _LOCK_FD
    if fcntl is None:
        log.warning("fcntl unavailable (non-Unix) — single-launch guard SKIPPED")
        return
    _LOCK_FD = open(lock_path, "a")
    try:
        fcntl.flock(_LOCK_FD.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.error(
            "Another --live publish is already running (lock held: %s). "
            "Aborting to prevent duplicate submission.",
            lock_path,
        )
        sys.exit(1)
    log.info("Single-launch lock acquired: %s", lock_path)


# ── Helpers ────────────────────────────────────────────────────────────


def to_micro(value: float) -> int:
    """Convert decimal token amount to integer microunits (floor)."""
    return math.floor(value * MICROUNITS)


def app_address(app_id: int) -> str:
    return logic.get_application_address(app_id)


def get_algod_client(algod_url: str, algod_token: str) -> algod.AlgodClient | None:
    try:
        client = algod.AlgodClient(algod_token, algod_url)
        status = client.status()
        log.info("Algod connected: round=%d", status["last-round"])
        return client
    except Exception as e:
        log.warning("Algod connection failed: %s", e)
        return None


def query_weekly_rewards(db_main, days: int = 7) -> tuple[dict[str, dict[str, int]], int]:
    """Query device-rewards for weekly_rewards in the past N days.

    Returns ({wallet: {"tfry": int, "fnode": int}}, devices_without_wallet_count).
    """
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    pipeline = [
        {"$unwind": "$weekly_rewards"},
        {
            "$match": {
                "weekly_rewards.unlock_at": {
                    "$gte": start_dt,
                    "$lte": end_dt,
                }
            }
        },
        {
            "$lookup": {
                "from": "devices",
                "localField": "miner_key",
                "foreignField": "miner_key",
                "as": "device",
            }
        },
        {
            "$project": {
                "miner_key": 1,
                "wallet": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$device.reward_wallet", 0]},
                        {"$arrayElemAt": ["$device.address", 0]},
                    ]
                },
                "amount": {"$toDouble": "$weekly_rewards.amount"},
                "asset_id": {"$toString": "$weekly_rewards.asset_id"},
            }
        },
        {
            "$group": {
                "_id": "$wallet",
                "tfry": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$asset_id", str(TFRY_ID)]},
                            "$amount",
                            0,
                        ]
                    }
                },
                "fnode": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$asset_id", str(FNODE_ID)]},
                            "$amount",
                            0,
                        ]
                    }
                },
                "device_count": {"$sum": 1},
            }
        },
    ]

    try:
        results = list(db_main["device-rewards"].aggregate(pipeline, allowDiskUse=True))
    except Exception as e:
        log.error("Mongo aggregation failed: %s", e)
        return {}, 0

    wallet_totals = {}
    devices_without_wallet = 0

    for r in results:
        wallet = (r["_id"] or "").strip()
        if not wallet:
            devices_without_wallet += r.get("device_count", 0)
            continue
        try:
            encoding.decode_address(wallet)
        except Exception:
            devices_without_wallet += r.get("device_count", 0)
            continue
        tfry = to_micro(r.get("tfry", 0))
        fnode = to_micro(r.get("fnode", 0))
        if tfry > 0 or fnode > 0:
            wallet_totals[wallet] = {
                "tfry": tfry,
                "fnode": fnode,
                "device_count": r.get("device_count", 0),
            }

    return wallet_totals, devices_without_wallet


def get_distinct_unlock_at_in_range(db_main, start_dt, end_dt) -> list[str]:
    """Return distinct unlock_at dates (YYYY-MM-DD) within a range, newest first."""
    pipeline = [
        {"$unwind": "$weekly_rewards"},
        {
            "$match": {
                "$expr": {
                    "$and": [
                        {"$gte": [{"$toDate": "$weekly_rewards.unlock_at"}, start_dt]},
                        {"$lte": [{"$toDate": "$weekly_rewards.unlock_at"}, end_dt]},
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": {"$toDate": "$weekly_rewards.unlock_at"},
                    }
                }
            }
        },
        {"$sort": {"_id": -1}},
    ]
    try:
        results = list(db_main["device-rewards"].aggregate(pipeline, allowDiskUse=True))
        return [r["_id"] for r in results]
    except Exception as e:
        log.warning("get_distinct_unlock_at_in_range failed: %s", e)
        return []


def query_last_windows(db_main, n: int = 3) -> list[dict]:
    """Query last N distinct unlock_at windows with device counts and totals."""
    pipeline = [
        {"$unwind": "$weekly_rewards"},
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$toDate": "$weekly_rewards.unlock_at"}}},
                "devices": {"$sum": 1},
                "tfry": {
                    "$sum": {
                        "$cond": [
                            {"$eq": [{"$toString": "$weekly_rewards.asset_id"}, str(TFRY_ID)]},
                            {"$toDouble": "$weekly_rewards.amount"},
                            0,
                        ]
                    }
                },
                "fnode": {
                    "$sum": {
                        "$cond": [
                            {"$eq": [{"$toString": "$weekly_rewards.asset_id"}, str(FNODE_ID)]},
                            {"$toDouble": "$weekly_rewards.amount"},
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": -1}},
        {"$limit": n},
    ]
    try:
        results = list(db_main["device-rewards"].aggregate(pipeline, allowDiskUse=True))
        return [
            {
                "window": r["_id"],
                "devices": r.get("devices", 0),
                "tfry": r.get("tfry", 0.0),
                "fnode": r.get("fnode", 0.0),
            }
            for r in results
        ]
    except Exception as e:
        log.warning("query_last_windows failed: %s", e)
        return []


def query_weekly_rewards_for_window(db_main, window_date_str: str) -> tuple[dict[str, dict[str, int]], int]:
    """Query device-rewards for a single unlock_at date (YYYY-MM-DD)."""
    from datetime import datetime
    year, month, day = map(int, window_date_str.split("-"))
    start_dt = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)

    pipeline = [
        {"$unwind": "$weekly_rewards"},
        {
            "$match": {
                "weekly_rewards.unlock_at": {
                    "$gte": start_dt,
                    "$lte": end_dt,
                }
            }
        },
        {
            "$lookup": {
                "from": "devices",
                "localField": "miner_key",
                "foreignField": "miner_key",
                "as": "device",
            }
        },
        {
            "$project": {
                "miner_key": 1,
                "wallet": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$device.reward_wallet", 0]},
                        {"$arrayElemAt": ["$device.address", 0]},
                    ]
                },
                "amount": {"$toDouble": "$weekly_rewards.amount"},
                "asset_id": {"$toString": "$weekly_rewards.asset_id"},
            }
        },
        {
            "$group": {
                "_id": "$wallet",
                "tfry": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$asset_id", str(TFRY_ID)]},
                            "$amount",
                            0,
                        ]
                    }
                },
                "fnode": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$asset_id", str(FNODE_ID)]},
                            "$amount",
                            0,
                        ]
                    }
                },
                "device_count": {"$sum": 1},
            }
        },
    ]

    try:
        results = list(db_main["device-rewards"].aggregate(pipeline, allowDiskUse=True))
    except Exception as e:
        log.error("Mongo single-window aggregation failed: %s", e)
        return {}, 0

    wallet_totals = {}
    devices_without_wallet = 0
    for r in results:
        wallet = (r["_id"] or "").strip()
        if not wallet:
            devices_without_wallet += r.get("device_count", 0)
            continue
        try:
            encoding.decode_address(wallet)
        except Exception:
            devices_without_wallet += r.get("device_count", 0)
            continue
        tfry = to_micro(r.get("tfry", 0))
        fnode = to_micro(r.get("fnode", 0))
        if tfry > 0 or fnode > 0:
            wallet_totals[wallet] = {
                "tfry": tfry,
                "fnode": fnode,
                "device_count": r.get("device_count", 0),
            }

    return wallet_totals, devices_without_wallet


def load_preseed(path: str) -> dict[str, dict[str, int]]:
    """Load preseed Merkle JSON and return wallet amounts."""
    with open(path) as f:
        data = json.load(f)

    result = {}
    for wallet, info in data.get("wallets", {}).items():
        result[wallet] = {
            "tfry": info.get("entitled_tfry", 0),
            "fnode": info.get("entitled_fnode", 0),
        }
    return result


def get_existing_wallets(client: algod.AlgodClient, app_id: int) -> set[str]:
    """Query app boxes to find wallets that already have a state box."""
    existing = set()
    if client is None:
        return existing
    try:
        boxes_resp = client.application_boxes(app_id)
        for box in boxes_resp.get("boxes", []):
            name_b64 = box.get("name", "")
            if not name_b64:
                continue
            name_bytes = base64.b64decode(name_b64)
            if len(name_bytes) == 33 and name_bytes[:1] == b"w":
                wallet = encoding.encode_address(name_bytes[1:])
                existing.add(wallet)
    except Exception as e:
        log.warning("Could not query app boxes: %s", e)
    return existing


def decode_box_state(value_b64: str) -> dict[str, int]:
    """Decode a 64-byte WalletState box into 8 uint64 fields."""
    value_bytes = base64.b64decode(value_b64)
    if len(value_bytes) != 64:
        raise ValueError(f"Expected 64 bytes, got {len(value_bytes)}")
    vals = struct.unpack(">8Q", value_bytes)
    return {
        "entitled_tfry": vals[0],
        "entitled_fnode": vals[1],
        "matured_tfry": vals[2],
        "matured_fnode": vals[3],
        "claimed_tfry": vals[4],
        "claimed_fnode": vals[5],
        "last_update_epoch": vals[6],
        "last_claim_time": vals[7],
    }


def get_wallet_claimed(client: algod.AlgodClient, app_id: int, wallet: str) -> tuple[int, int]:
    """Read claimed amounts from on-chain box for a wallet."""
    if client is None:
        return 0, 0
    try:
        box_key = b"w" + encoding.decode_address(wallet)
        box_data = client.application_box_by_name(app_id, box_key)
        state = decode_box_state(box_data["value"])
        return state["claimed_tfry"], state["claimed_fnode"]
    except Exception as e:
        log.warning("Could not read box for %s: %s", wallet[:12], e)
        return 0, 0


def get_app_balances(
    client: algod.AlgodClient, app_id: int
) -> tuple[int, int, int]:
    """Return (algo_micro, tfry_micro, fnode_micro) for app account."""
    if client is None:
        return 0, 0, 0
    try:
        addr = app_address(app_id)
        info = client.account_info(addr)
        algo = info.get("amount", 0)
        tfry = 0
        fnode = 0
        for asset in info.get("assets", []):
            if asset["asset-id"] == TFRY_ID:
                tfry = asset.get("amount", 0)
            elif asset["asset-id"] == FNODE_ID:
                fnode = asset.get("amount", 0)
        return algo, tfry, fnode
    except Exception as e:
        log.warning("Could not query app balances: %s", e)
        return 0, 0, 0


def get_account_balance(client: algod.AlgodClient, addr: str) -> int:
    """Return ALGO microbalance for an account."""
    if client is None:
        return 0
    try:
        info = client.account_info(addr)
        return info.get("amount", 0)
    except Exception as e:
        log.warning("Could not query account %s: %s", addr[:12], e)
        return 0


# ── Record building ────────────────────────────────────────────────────


def compute_publish_records(
    weekly: dict[str, dict[str, int]],
    preseed: dict[str, dict[str, int]],
    epoch: int,
    db_main,
    maturation_epochs: int = 4,
) -> list[dict[str, Any]]:
    """Compute full publish records: weekly + preseed fold + history lookback."""
    history_col = db_main.reward_epoch_history

    records_by_wallet: dict[str, dict[str, Any]] = {}

    # Start with weekly wallets
    for wallet, amounts in weekly.items():
        prev = history_col.find_one(
            {"wallet": wallet, "epoch": {"$lt": epoch}},
            sort=[("epoch", -1)],
        )
        prev_tfry = prev["cumulative_tfry"] if prev else 0
        prev_fnode = prev["cumulative_fnode"] if prev else 0

        new_entitled_tfry = prev_tfry + amounts["tfry"]
        new_entitled_fnode = prev_fnode + amounts["fnode"]

        lookback_epoch = epoch - maturation_epochs
        if lookback_epoch >= 0:
            matured_doc = history_col.find_one(
                {"wallet": wallet, "epoch": lookback_epoch}
            )
            matured_tfry = matured_doc["cumulative_tfry"] if matured_doc else 0
            matured_fnode = matured_doc["cumulative_fnode"] if matured_doc else 0
        else:
            matured_tfry = 0
            matured_fnode = 0

        # Add preseed if this wallet has never been published
        if prev is None:
            pre_tfry = preseed.get(wallet, {}).get("tfry", 0)
            pre_fnode = preseed.get(wallet, {}).get("fnode", 0)
            new_entitled_tfry += pre_tfry
            new_entitled_fnode += pre_fnode
            matured_tfry += pre_tfry
            matured_fnode += pre_fnode

        records_by_wallet[wallet] = {
            "wallet": wallet,
            "entitled_tfry": new_entitled_tfry,
            "entitled_fnode": new_entitled_fnode,
            "matured_tfry": matured_tfry,
            "matured_fnode": matured_fnode,
            "device_count": weekly[wallet].get("device_count", 0),
            "epoch": epoch,
        }

    # Add preseed wallets not in weekly
    for wallet, amounts in preseed.items():
        if wallet in records_by_wallet:
            continue
        prev = history_col.find_one(
            {"wallet": wallet, "epoch": {"$lt": epoch}},
            sort=[("epoch", -1)],
        )
        if prev is None:
            records_by_wallet[wallet] = {
                "wallet": wallet,
                "entitled_tfry": amounts["tfry"],
                "entitled_fnode": amounts["fnode"],
                "matured_tfry": amounts["tfry"],
                "matured_fnode": amounts["fnode"],
                "epoch": epoch,
            }
        else:
            lookback_epoch = epoch - maturation_epochs
            if lookback_epoch >= 0:
                matured_doc = history_col.find_one(
                    {"wallet": wallet, "epoch": lookback_epoch}
                )
                matured_tfry = matured_doc["cumulative_tfry"] if matured_doc else 0
                matured_fnode = matured_doc["cumulative_fnode"] if matured_doc else 0
            else:
                matured_tfry = 0
                matured_fnode = 0
            records_by_wallet[wallet] = {
                "wallet": wallet,
                "entitled_tfry": prev["cumulative_tfry"],
                "entitled_fnode": prev["cumulative_fnode"],
                "matured_tfry": matured_tfry,
                "matured_fnode": matured_fnode,
                "epoch": epoch,
            }

    return list(records_by_wallet.values())


def compute_weekly_from_records(
    records: list[dict],
    epoch: int,
    db_main,
) -> dict[str, dict[str, int]]:
    """Derive weekly deltas from publish records + history."""
    history_col = db_main.reward_epoch_history
    weekly = {}
    for rec in records:
        wallet = rec["wallet"]
        prev = history_col.find_one(
            {"wallet": wallet, "epoch": {"$lt": epoch}},
            sort=[("epoch", -1)],
        )
        prev_tfry = prev["cumulative_tfry"] if prev else 0
        prev_fnode = prev["cumulative_fnode"] if prev else 0
        delta_tfry = rec["entitled_tfry"] - prev_tfry
        delta_fnode = rec["entitled_fnode"] - prev_fnode
        if delta_tfry > 0 or delta_fnode > 0:
            weekly[wallet] = {
                "tfry": delta_tfry,
                "fnode": delta_fnode,
                "device_count": rec.get("device_count", 0),
            }
    return weekly


# ── Caps ───────────────────────────────────────────────────────────────


def check_caps(
    records: list[dict],
    weekly: dict[str, dict[str, int]],
    preseed: dict[str, dict[str, int]],
    client: algod.AlgodClient,
    app_id: int,
    db_main=None,
) -> dict[str, Any]:
    """Apply safety caps. Returns report dict. Exits on breach."""
    report: dict[str, Any] = {"pass": True, "details": {}}

    P = len(preseed)
    W = len(weekly)
    published_count = len(records)

    # Cap (a) weekly totals — dynamic trailing-3-window baseline
    weekly_tfry = sum(a["tfry"] for a in weekly.values())
    weekly_fnode = sum(a["fnode"] for a in weekly.values())

    windows = query_last_windows(db_main, n=3) if db_main is not None else []
    if len(windows) >= 3:
        mean_tfry_dec = sum(w["tfry"] for w in windows) / len(windows)
        mean_fnode_dec = sum(w["fnode"] for w in windows) / len(windows)
        threshold_tfry_dec = mean_tfry_dec * 3
        threshold_fnode_dec = mean_fnode_dec * 3
        pass_tfry = weekly_tfry / MICROUNITS <= threshold_tfry_dec
        pass_fnode = weekly_fnode / MICROUNITS <= threshold_fnode_dec
        report["details"]["cap_a"] = {
            "weekly_tfry": weekly_tfry,
            "weekly_fnode": weekly_fnode,
            "windows": [w["window"] for w in windows],
            "mean_tfry_dec": mean_tfry_dec,
            "mean_fnode_dec": mean_fnode_dec,
            "threshold_tfry_dec": threshold_tfry_dec,
            "threshold_fnode_dec": threshold_fnode_dec,
            "pass_tfry": pass_tfry,
            "pass_fnode": pass_fnode,
            "pass": pass_tfry and pass_fnode,
        }
        if not pass_tfry or not pass_fnode:
            report["pass"] = False
    else:
        report["details"]["cap_a"] = {
            "weekly_tfry": weekly_tfry,
            "weekly_fnode": weekly_fnode,
            "windows": [w["window"] for w in windows],
            "note": f"insufficient history ({len(windows)} windows)",
            "pass_tfry": True,
            "pass_fnode": True,
            "pass": True,
        }

    # Cap (b) total entitled - already_claimed <= pool balances
    total_entitled_tfry = sum(r["entitled_tfry"] for r in records)
    total_entitled_fnode = sum(r["entitled_fnode"] for r in records)

    already_claimed_tfry = 0
    already_claimed_fnode = 0
    existing = get_existing_wallets(client, app_id)
    for wallet in existing:
        ct, cf = get_wallet_claimed(client, app_id, wallet)
        already_claimed_tfry += ct
        already_claimed_fnode += cf

    _, pool_tfry, pool_fnode = get_app_balances(client, app_id)

    unclaimed_tfry = total_entitled_tfry - already_claimed_tfry
    unclaimed_fnode = total_entitled_fnode - already_claimed_fnode

    pass_tfry_b = unclaimed_tfry <= pool_tfry
    pass_fnode_b = unclaimed_fnode <= pool_fnode
    report["details"]["cap_b"] = {
        "total_entitled_tfry": total_entitled_tfry,
        "total_entitled_fnode": total_entitled_fnode,
        "already_claimed_tfry": already_claimed_tfry,
        "already_claimed_fnode": already_claimed_fnode,
        "unclaimed_tfry": unclaimed_tfry,
        "unclaimed_fnode": unclaimed_fnode,
        "pool_tfry": pool_tfry,
        "pool_fnode": pool_fnode,
        "cap_tfry": pool_tfry,
        "cap_fnode": pool_fnode,
        "pass_tfry": pass_tfry_b,
        "pass_fnode": pass_fnode_b,
        "pass": pass_tfry_b and pass_fnode_b,
    }
    if not pass_tfry_b or not pass_fnode_b:
        report["pass"] = False

    # Cap (c) wallet count invariant
    count_min = max(P, W)
    count_max = P + W
    report["details"]["cap_c"] = {
        "published_count": published_count,
        "P": P,
        "W": W,
        "min": count_min,
        "max": count_max,
        "pass": count_min <= published_count <= count_max,
    }
    if not report["details"]["cap_c"]["pass"]:
        report["pass"] = False

    # Cap (d) per-device threshold: 10x mean_per_device x device_count
    if weekly:
        amounts = [a["tfry"] + a["fnode"] for a in weekly.values()]
        total_devices = sum(a["device_count"] for a in weekly.values())
        mean_per_device = sum(amounts) / total_devices if total_devices else 0
        max_weekly = max(amounts)

        flagged = []
        old_flagged = []
        old_mean = sum(amounts) / len(amounts)
        old_threshold = old_mean * 10
        for wallet, data in weekly.items():
            wallet_total = data["tfry"] + data["fnode"]
            threshold = 10 * mean_per_device * data["device_count"]
            if wallet_total > threshold:
                flagged.append({
                    "wallet": wallet,
                    "amount": wallet_total,
                    "threshold": threshold,
                    "device_count": data["device_count"],
                })
            if wallet_total > old_threshold:
                old_flagged.append({
                    "wallet": wallet,
                    "amount": wallet_total,
                    "threshold": old_threshold,
                    "device_count": data["device_count"],
                })

        report["details"]["cap_d"] = {
            "mean_per_device": mean_per_device,
            "mean_per_device_dec": mean_per_device / MICROUNITS,
            "total_devices": total_devices,
            "W": len(weekly),
            "max_weekly": max_weekly,
            "max_weekly_dec": max_weekly / MICROUNITS,
            "flagged": flagged,
            "old_flagged": old_flagged,
            "pass": len(flagged) == 0,
        }
        if not report["details"]["cap_d"]["pass"]:
            report["pass"] = False
    else:
        report["details"]["cap_d"] = {"pass": True, "note": "no weekly wallets"}

    # Group count cap
    num_groups = math.ceil(published_count / WALLETS_PER_GROUP)
    report["details"]["group_cap"] = {
        "num_groups": num_groups,
        "max_groups": MAX_GROUPS,
        "pass": num_groups <= MAX_GROUPS,
    }
    if not report["details"]["group_cap"]["pass"]:
        report["pass"] = False

    if not report["pass"]:
        print("\n============ CAP BREACH REPORT ============")
        for cap_name, cap_data in report["details"].items():
            print(f"\n{cap_name}:")
            for k, v in cap_data.items():
                if isinstance(v, int) and v > 1000 and any(x in k for x in ("tfry", "fnode", "algo")):
                    print(f"  {k}: {v / MICROUNITS:,.2f}")
                else:
                    print(f"  {k}: {v}")
        print(f"\nInvariant: max({P}, {W}) = {count_min} <= {published_count} <= {P + W} = {count_max}")
        print("===========================================")

    return report


# ── Batch builder ────────────────────────────────────────────────────────


def build_atomic_groups(
    records: list[dict],
    existing_wallets: set[str],
    app_id: int,
    admin_addr: str,
    client: algod.AlgodClient,
) -> list[list[transaction.Transaction]]:
    """Build atomic groups of up to 8 wallets (16 txns)."""
    if client is None:
        log.warning("No algod client; cannot build groups with valid suggested params")
        return []

    app_addr = app_address(app_id)
    method = Method.from_signature(PUBLISH_METHOD_SIG)
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 1000

    groups = []
    current_group: list[transaction.Transaction] = []

    import secrets

    # MBR optimization: use app's existing ALGO excess before drawing from admin
    acct = client.account_info(app_addr)
    app_excess = acct["amount"] - acct["min-balance"]
    log.info("App ALGO excess: %d (%.3f ALGO)", app_excess, app_excess / 1e6)

    mbr_from_admin = 0
    mbr_from_excess = 0

    for rec in records:
        wallet = rec["wallet"]
        is_existing = wallet in existing_wallets

        if is_existing:
            mbr_amount = 0
        elif app_excess >= MBR_PER_NEW_WALLET:
            mbr_amount = 0
            app_excess -= MBR_PER_NEW_WALLET
            mbr_from_excess += 1
        else:
            mbr_amount = MBR_PER_NEW_WALLET
            mbr_from_admin += 1

        mbr_pay = transaction.PaymentTxn(
            sender=admin_addr,
            sp=sp,
            receiver=app_addr,
            amt=mbr_amount,
            lease=secrets.token_bytes(32),
        )

        box_key = b"w" + encoding.decode_address(wallet)
        app_call = transaction.ApplicationCallTxn(
            sender=admin_addr,
            sp=sp,
            index=app_id,
            on_complete=transaction.OnComplete.NoOpOC,
            app_args=[
                method.get_selector(),
                encoding.decode_address(wallet),
                rec["entitled_tfry"].to_bytes(8, "big"),
                rec["entitled_fnode"].to_bytes(8, "big"),
                rec["matured_tfry"].to_bytes(8, "big"),
                rec["matured_fnode"].to_bytes(8, "big"),
                rec["epoch"].to_bytes(8, "big"),
            ],
            accounts=[wallet],
            boxes=[(app_id, box_key)],
            lease=secrets.token_bytes(32),
        )

        current_group.extend([mbr_pay, app_call])

        if len(current_group) == MAX_TXNS_PER_GROUP:
            groups.append(current_group)
            current_group = []

    if current_group:
        groups.append(current_group)

    log.info(
        "MBR: %d from excess, %d from admin (%.3f ALGO)",
        mbr_from_excess, mbr_from_admin, mbr_from_admin * MBR_PER_NEW_WALLET / 1e6,
    )

    # assignGroupID and set fees AFTER per §15
    for group in groups:
        # assign_group_id deferred to simulate_groups / submit_groups
        for txn in group:
            txn.fee = 1000

    return groups


# ── Simulate ─────────────────────────────────────────────────────────────


def simulate_groups(
    client: algod.AlgodClient,
    groups: list[list[transaction.Transaction]],
    algod_url: str = "",
    algod_token: str = "",
) -> tuple[bool, int, int]:
    """Simulate each group. Return (all_pass, passed_count, failed_count)."""
    if not groups:
        log.info("No groups to simulate")
        return True, 0, 0

    # Get auth address once for rekeyed admin
    auth_addr = ADMIN_ADDR
    try:
        acct_info = client.account_info(ADMIN_ADDR)
        auth_addr = acct_info.get("auth-addr", ADMIN_ADDR)
    except Exception as e:
        log.warning("Could not query auth-addr for admin: %s", e)

    # Base suggested params for per-group first_valid offset
    base_sp = client.suggested_params()

    passed = 0
    failed = 0
    for i, group in enumerate(groups):
        try:
            # Unique txids per txn: offset first_valid per group AND per txn
            # (avoids node cache collision on same sender/app_id/first_valid)
            for j, txn in enumerate(group):
                txn.first_valid_round = base_sp.first - i - j
                txn.last_valid_round = base_sp.last - i - j
                txn.note = f"sim-{i}-{j}".encode()
            transaction.assign_group_id(group)

            stxns = []
            for txn in group:
                stxn = transaction.SignedTransaction(txn, b"", authorizing_address=auth_addr)
                stxns.append(stxn)

            # Fresh client per group to avoid any connection-level txn caching
            sim_client = (
                algod.AlgodClient(algod_token, algod_url)
                if algod_url
                else client
            )
            tg = SimulateRequestTransactionGroup(txns=stxns)
            req = SimulateRequest(txn_groups=[tg], allow_empty_signatures=True)
            result = sim_client.simulate_transactions(req)

            group_result = result.get("txn-groups", [{}])[0]
            failure_message = group_result.get("failure-message", "")
            if failure_message:
                log.error("Group %d simulate FAILED: %s", i, failure_message)
                print(f"\nSimulate group {i} FAILED: {failure_message}")
                failed += 1
            else:
                log.info("Group %d simulate OK", i)
                passed += 1
        except Exception as e:
            log.error("Group %d simulate exception: %s", i, e)
            print(f"\nSimulate group {i} exception: {e}")
            failed += 1

    return failed == 0, passed, failed


# ── Live submit ──────────────────────────────────────────────────────────


def submit_groups(
    client: algod.AlgodClient,
    groups: list[list[transaction.Transaction]],
    signer_sk: bytes,
    admin_addr: str,
    records: list[dict] | None = None,
    app_id: int = APP_ID,
) -> dict[str, Any]:
    """Submit atomic groups live with ground-truth box verification.

    Each group is verified by checking that its wallet boxes exist on-chain
    after submission. Retries up to 3 times with fresh params on failure.
    """
    import time

    stats = {"submitted": 0, "failed": 0, "wallets": 0}

    # Pre-compute wallet list per group from records (reliable source)
    wallets_per_group: list[list[str]] = []
    if records:
        idx = 0
        for group in groups:
            n_wallets = len(group) // 2
            wallets_per_group.append(
                [records[idx + j]["wallet"] for j in range(n_wallets)]
            )
            idx += n_wallets

    import secrets

    for i, group in enumerate(groups):
        log.info("Group %d/%d — %d wallets published so far", i + 1, len(groups), stats["wallets"])
        group_wallets = wallets_per_group[i] if wallets_per_group else []
        expected = len(group) // 2
        if len(group_wallets) != expected:
            raise Exception(
                f"Group {i}: extracted {len(group_wallets)} wallets, "
                f"expected {expected} — wallet list mismatch; "
                f"aborting before any submit"
            )

        success = False
        for attempt in range(3):
            try:
                if attempt > 0:
                    # Fresh params + fresh lease for retry (genuinely new TXIDs)
                    sp = client.suggested_params()
                    sp.flat_fee = True
                    sp.fee = 1000
                    for txn in group:
                        txn.first_valid_round = sp.first
                        txn.last_valid_round = sp.last
                        txn.genesis_hash = sp.gh
                        txn.lease = secrets.token_bytes(32)
                        txn.group = None
                    log.info("Group %d retry %d with fresh params+lease", i, attempt + 1)

                transaction.assign_group_id(group)
                signed_txns = [txn.sign(signer_sk) for txn in group]

                txid = None
                try:
                    txid = client.send_transactions(signed_txns)
                except Exception as send_err:
                    if "already in ledger" not in str(send_err):
                        raise
                    log.info("Group %d send returned 'already in ledger', checking boxes", i)

                # Poll for confirmation if we have txid
                if txid:
                    for _ in range(20):
                        try:
                            info = client.pending_transaction_info(txid)
                            cr = info.get("confirmed-round", 0)
                            if cr and cr > 0:
                                log.info("Group %d confirmed round %d", i, cr)
                                break
                            pe = info.get("pool-error", "")
                            if pe:
                                raise Exception(f"pool error: {pe}")
                        except Exception as poll_err:
                            if "not found" in str(poll_err).lower():
                                break
                            raise
                        time.sleep(3)

                # GROUND TRUTH: poll box existence (absorb Nodely LB lag)
                boxes_verified = False
                for poll in range(5):
                    time.sleep(2)
                    boxes_found = 0
                    for wallet in group_wallets:
                        box_name = b"w" + encoding.decode_address(wallet)
                        try:
                            client.application_box_by_name(app_id, box_name)
                            boxes_found += 1
                        except Exception:
                            pass
                    if boxes_found == len(group_wallets):
                        boxes_verified = True
                        break
                    log.debug(
                        "Group %d box poll %d: %d/%d",
                        i, poll + 1, boxes_found, len(group_wallets),
                    )

                if boxes_verified:
                    success = True
                    break
                else:
                    log.warning(
                        "Group %d attempt %d: %d/%d boxes after polling, will retry",
                        i, attempt + 1, boxes_found, len(group_wallets),
                    )

            except Exception as e:
                log.warning("Group %d attempt %d error: %s", i, attempt + 1, e)

        if success:
            stats["submitted"] += 1
            stats["wallets"] += len(group) // 2
            log.info("Group %d verified (%d boxes on-chain)", i, len(group_wallets))
        else:
            stats["failed"] += 1
            log.error("Group %d FAILED after 3 attempts (%d/%d boxes)", i, boxes_found, len(group_wallets))
            print(f"\nGroup {i} FAILED after 3 attempts ({boxes_found}/{len(group_wallets)} boxes)")

    return stats


def submit_advance_epoch(
    client: algod.AlgodClient,
    app_id: int,
    signer_sk: bytes,
    admin_addr: str,
) -> str | None:
    """Submit advance_epoch app call. Returns txid or None.

    Uses direct sign + send_raw_transaction (ATC has rekeyed account issues).
    """
    method = Method.from_signature(ADVANCE_METHOD_SIG)
    sp = client.suggested_params()
    sp.flat_fee = True
    sp.fee = 2000

    txn = transaction.ApplicationCallTxn(
        sender=admin_addr,
        sp=sp,
        index=app_id,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=[method.get_selector()],
    )

    try:
        stxn = txn.sign(signer_sk)
        txid = client.send_transaction(stxn)
        transaction.wait_for_confirmation(client, txid, 4)
        log.info("advance_epoch submitted: %s", txid)
        return txid
    except Exception as e:
        log.error("advance_epoch FAILED: %s", e)
        return None


# ── Plan export for call-batch.html ──────────────────────────────────────


AUTH_ADDR = "7CTUPB7WJQYZBLPYIRQMRVOS2QOAB3R7RJBYTX5VGHV6QL4D7GCVCNCC7U"


def export_plan(
    records: list[dict],
    existing_wallets: set[str],
    epoch: int,
    app_id: int,
    output_path: str,
    client: algod.AlgodClient | None = None,
) -> None:
    """Export publish plan as call-batch.html-compatible JSON.

    Generates a plan that can be loaded into wc-signer's call-batch.html
    for batch signing via Pera Wallet, eliminating the need for
    AUTHORITY_MNEMONIC on the execution host.

    MBR optimization: queries app's current ALGO excess and only includes
    non-zero MBR payments for wallets where the app account needs more ALGO
    to cover the new box min-balance. Prevents admin ALGO exhaustion.
    """
    app_addr = app_address(app_id)

    # Calculate app's ALGO excess to optimize MBR payments
    app_excess = 0
    if client:
        try:
            acct = client.account_info(app_addr)
            app_balance = acct["amount"]
            app_min_bal = acct["min-balance"]
            app_excess = app_balance - app_min_bal
            log.info(
                "App ALGO: balance=%d min_bal=%d excess=%d (%.3f ALGO)",
                app_balance, app_min_bal, app_excess, app_excess / 1e6,
            )
        except Exception as e:
            log.warning("Could not query app balance for MBR optimization: %s", e)

    plan: dict[str, Any] = {
        "appId": app_id,
        "sender": ADMIN_ADDR,
        "authAddr": AUTH_ADDR,
        "network": "mainnet",
        "batchSize": 10,
        "groups": [],
    }

    new_count = 0
    mbr_from_admin = 0
    mbr_from_excess = 0
    current_txns: list[dict[str, Any]] = []
    wallet_start = 0
    for i, rec in enumerate(records):
        wallet = rec["wallet"]
        is_existing = wallet in existing_wallets

        if is_existing:
            mbr_amount = 0
        elif app_excess >= MBR_PER_NEW_WALLET:
            # App has enough excess ALGO — no payment needed from admin
            mbr_amount = 0
            app_excess -= MBR_PER_NEW_WALLET
            mbr_from_excess += 1
            new_count += 1
        else:
            # Admin must pay full MBR
            mbr_amount = MBR_PER_NEW_WALLET
            mbr_from_admin += 1
            new_count += 1

        box_key_hex = "77" + encoding.decode_address(wallet).hex()

        current_txns.append({"type": "pay", "to": app_addr, "amount": mbr_amount})
        current_txns.append({
            "type": "appl",
            "method": PUBLISH_METHOD_SIG,
            "args": [
                wallet,
                rec["entitled_tfry"],
                rec["entitled_fnode"],
                rec["matured_tfry"],
                rec["matured_fnode"],
                rec["epoch"],
            ],
            "boxes": [{"appIndex": app_id, "name": "hex:" + box_key_hex}],
            "accounts": [wallet],
        })

        if len(current_txns) == MAX_TXNS_PER_GROUP:
            plan["groups"].append({
                "description": f"Wallets {wallet_start + 1}-{i + 1}",
                "txns": current_txns,
            })
            current_txns = []
            wallet_start = i + 1

    if current_txns:
        plan["groups"].append({
            "description": f"Wallets {wallet_start + 1}-{len(records)}",
            "txns": current_txns,
        })

    plan["groups"].append({
        "description": "advance_epoch",
        "txns": [{
            "type": "appl",
            "method": ADVANCE_METHOD_SIG,
            "args": [],
            "fee": 2000,
        }],
    })

    with open(output_path, "w") as f:
        json.dump(plan, f, indent=2)

    admin_algo_needed = mbr_from_admin * MBR_PER_NEW_WALLET
    print(
        f"Plan exported: {output_path} "
        f"({len(plan['groups'])} groups, {len(records)} wallets, "
        f"batchSize={plan['batchSize']})"
    )
    print(
        f"MBR: {new_count} new wallets — "
        f"{mbr_from_excess} covered by app excess, "
        f"{mbr_from_admin} need admin payment "
        f"({admin_algo_needed / 1e6:.3f} ALGO from admin)"
    )


# ── Mongo logging ──────────────────────────────────────────────────────


def save_epoch_history(
    db_main, records: list[dict], now: datetime, reduction_factor: float = 1.0
) -> None:
    history_col = db_main.reward_epoch_history
    docs = []
    for rec in records:
        docs.append(
            {
                "wallet": rec["wallet"],
                "epoch": rec["epoch"],
                "cumulative_tfry": rec["entitled_tfry"],
                "cumulative_fnode": rec["entitled_fnode"],
                "reduction_factor": reduction_factor,
                "computed_at": now,
            }
        )
    if docs:
        history_col.insert_many(docs)
    log.info("Saved %d epoch history records", len(docs))


def save_publish_log(
    db_main,
    records: list[dict],
    epoch: int,
    dry_run: bool,
    now: datetime,
) -> None:
    total_tfry = sum(r["entitled_tfry"] - r.get("prev_tfry", 0) for r in records)
    total_fnode = sum(r["entitled_fnode"] - r.get("prev_fnode", 0) for r in records)
    db_main.reward_publish_log.insert_one(
        {
            "epoch": epoch,
            "wallet_count": len(records),
            "total_new_tfry": total_tfry,
            "total_new_fnode": total_fnode,
            "dry_run": dry_run,
            "computed_at": now,
        }
    )


# ── Dry-run summary ──────────────────────────────────────────────────────


def print_dry_run_summary(
    records: list[dict],
    weekly: dict[str, dict[str, int]],
    preseed: dict[str, dict[str, int]],
    existing: set[str],
    client: algod.AlgodClient,
    cap_report: dict,
    epoch: int,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    db_main=None,
    sim_all_pass: bool = True,
    sim_passed: int = 0,
    sim_failed: int = 0,
    side_by_side: dict | None = None,
    only_missing: bool = False,
    skip_advance: bool = False,
) -> None:
    print(f"\n============ DRY RUN SUMMARY (Epoch {epoch}) ============")
    print(f"Total wallets to publish: {len(records)}")
    print(f"  Preseed wallets: {len(preseed)}")
    print(f"  Weekly wallets: {len(weekly)}")
    print(f"  Existing boxes (no MBR): {len(existing)}")
    print(f"  New boxes (MBR required): {len(records) - len(existing)}")

    total_tfry = sum(r["entitled_tfry"] for r in records)
    total_fnode = sum(r["entitled_fnode"] for r in records)
    print(f"\nTotal entitled tFRY: {total_tfry / MICROUNITS:,.2f}")
    print(f"Total entitled fNODE: {total_fnode / MICROUNITS:,.2f}")

    weekly_tfry = sum(a["tfry"] for a in weekly.values())
    weekly_fnode = sum(a["fnode"] for a in weekly.values())
    print(f"Weekly new tFRY: {weekly_tfry / MICROUNITS:,.2f}")
    print(f"Weekly new fNODE: {weekly_fnode / MICROUNITS:,.2f}")

    # Window info
    if start_dt and end_dt:
        print(f"\n--- WINDOW ---")
        print(f"Range: {start_dt.isoformat()} to {end_dt.isoformat()}")
        print(f"Note: mean over weekly-earning wallets W = {len(weekly)}, not all published wallets")

    # Recent windows table
    if db_main is not None:
        recent = query_last_windows(db_main, n=3)
        if recent:
            print(f"\n--- RECENT WINDOWS ---")
            print(f"{'Window':<12} {'Devices':>10} {'tFRY':>14} {'fNODE':>14}")
            print("-" * 54)
            for w in recent:
                print(f"{w['window']:<12} {w['devices']:>10,} {w['tfry']:>14,.2f} {w['fnode']:>14,.2f}")

    # ALGO required (MBR + transaction fees, mode-aware)
    new_boxes = len(records) - len(existing)
    _submit_n = new_boxes if only_missing else len(records)
    _num_groups = (_submit_n + WALLETS_PER_GROUP - 1) // WALLETS_PER_GROUP
    _mbr_budget = new_boxes * MBR_PER_NEW_WALLET
    _fee_budget = _num_groups * MAX_TXNS_PER_GROUP * 1000          # 1000 µA/txn
    if not skip_advance:
        _fee_budget += 2000                                       # advance_epoch app call
    algo_needed = _mbr_budget + _fee_budget
    admin_algo = get_account_balance(client, ADMIN_ADDR)
    app_algo, _, _ = get_app_balances(client, APP_ID)

    print(f"\n--- ALGO REQUIRED ---")
    print(f"New boxes: {new_boxes}")
    print(f"MBR per new box: {MBR_PER_NEW_WALLET} microAlgos")
    print(f"MBR total: {_mbr_budget:,} µA = {_mbr_budget / 1e6:.3f} ALGO")
    print(f"Fee budget: {_fee_budget:,} µA = {_fee_budget / 1e6:.3f} ALGO "
          f"({_num_groups} groups × {MAX_TXNS_PER_GROUP} txns × 0.001"
          f"{'' if skip_advance else ' + advance 0.002'})")
    print(f"Total ALGO needed: {algo_needed:,} µA = {algo_needed / 1e6:.3f} ALGO")
    print(f"App ALGO balance: {app_algo:,} µA = {app_algo / 1e6:.3f} ALGO")
    print(f"Admin ALGO balance: {admin_algo:,} µA = {admin_algo / 1e6:.3f} ALGO")

    available = admin_algo + app_algo
    if available < algo_needed:
        shortfall = algo_needed - available
        print(f"SHORTFALL: {shortfall:,} µA = {shortfall / 1e6:.3f} ALGO")
        print("LIVE SUBMIT BLOCKED: fund admin/app account before --live")
    else:
        print("Sufficient ALGO for MBR + fees.")

    # Cap a computation trace
    cap_a = cap_report.get("details", {}).get("cap_a", {})
    if cap_a and "windows" in cap_a:
        print(f"\n--- CAP A COMPUTATION ---")
        print(f"Trailing windows: {', '.join(cap_a['windows'])}")
        print(f"Mean tFRY per window:  {cap_a.get('mean_tfry_dec', 0):,.2f}")
        print(f"Mean fNODE per window: {cap_a.get('mean_fnode_dec', 0):,.2f}")
        print(f"Threshold (3x mean) tFRY:  {cap_a.get('threshold_tfry_dec', 0):,.2f}")
        print(f"Threshold (3x mean) fNODE: {cap_a.get('threshold_fnode_dec', 0):,.2f}")
        print(f"Actual weekly tFRY:  {cap_a.get('weekly_tfry', 0) / MICROUNITS:,.2f}")
        print(f"Actual weekly fNODE: {cap_a.get('weekly_fnode', 0) / MICROUNITS:,.2f}")
        print(f"Pass tFRY: {cap_a.get('pass_tfry', False)} | Pass fNODE: {cap_a.get('pass_fnode', False)}")

    # Cap d details
    cap_d = cap_report.get("details", {}).get("cap_d", {})
    if "mean_per_device_dec" in cap_d:
        print(f"\n--- CAP D DETAILS ---")
        print(f"Mean per device:        {cap_d['mean_per_device_dec']:,.2f}")
        print(f"Total devices:          {cap_d.get('total_devices', 0)}")
        print(f"Wallets in weekly:      {cap_d.get('W', 0)}")
        print(f"Max weekly per wallet:  {cap_d['max_weekly_dec']:,.2f}")
        flagged = cap_d.get("flagged", [])
        if flagged:
            print(f"\nFlagged wallets (new per-device check):")
            print(f"{'Wallet':>44} {'Total':>14} {'Threshold':>14} {'Devices':>8}")
            print("-" * 84)
            for f in flagged[:5]:
                print(f"{f['wallet']:>44} {f['amount']/MICROUNITS:>14,.2f} {f['threshold']/MICROUNITS:>14,.2f} {f['device_count']:>8}")
        old_flagged = cap_d.get("old_flagged", [])
        if old_flagged:
            print(f"\nWallets that would have FAILED old flat check:")
            print(f"{'Wallet':>44} {'Total':>14} {'Old Threshold':>14} {'Devices':>8}")
            print("-" * 84)
            for f in old_flagged[:5]:
                print(f"{f['wallet']:>44} {f['amount']/MICROUNITS:>14,.2f} {f['threshold']/MICROUNITS:>14,.2f} {f['device_count']:>8}")

    # Top-5 wallets
    if weekly:
        sorted_weekly = sorted(weekly.items(), key=lambda x: x[1]["tfry"] + x[1]["fnode"], reverse=True)[:5]
        print(f"\n--- TOP 5 WEEKLY WALLETS ---")
        print(f"{'Wallet':>44} {'tFRY':>14} {'fNODE':>14} {'Devices':>8}")
        print("-" * 84)
        for wallet, amounts in sorted_weekly:
            print(f"{wallet:>44} {amounts['tfry']/MICROUNITS:>14,.2f} {amounts['fnode']/MICROUNITS:>14,.2f} {amounts.get('device_count', 0):>8}")

    # Pool top-up
    cap_b = cap_report.get("details", {}).get("cap_b", {})
    if cap_b:
        print(f"\n--- POOL TOP-UP REQUIRED ---")
        print(f"{'Token':<10} {'Unclaimed':>18} {'Pool Balance':>18} {'Shortfall':>18}")
        print("-" * 66)
        unclaimed_tfry = cap_b.get("unclaimed_tfry", 0)
        unclaimed_fnode = cap_b.get("unclaimed_fnode", 0)
        pool_tfry = cap_b.get("pool_tfry", 0)
        pool_fnode = cap_b.get("pool_fnode", 0)
        short_tfry = max(0, unclaimed_tfry - pool_tfry)
        short_fnode = max(0, unclaimed_fnode - pool_fnode)
        print(f"{'tFRY':<10} {unclaimed_tfry/MICROUNITS:>18,.2f} {pool_tfry/MICROUNITS:>18,.2f} {short_tfry/MICROUNITS:>18,.2f}")
        print(f"{'fNODE':<10} {unclaimed_fnode/MICROUNITS:>18,.2f} {pool_fnode/MICROUNITS:>18,.2f} {short_fnode/MICROUNITS:>18,.2f}")
        print("Note: Pool currently holds exactly preseed obligation. Every weekly epoch requires a top-up or periodic pre-fund.")

    # Side-by-side comparison
    if side_by_side:
        print(f"\n--- SIDE-BY-SIDE: RANGE vs SINGLE WINDOW ({side_by_side['window']}) ---")
        sb_records = side_by_side["records"]
        sb_weekly = side_by_side["weekly"]
        sb_cap = side_by_side["cap_report"]

        print(f"{'Metric':<30} {'Range-based':>18} {'Single-window':>18}")
        print("-" * 68)
        print(f"{'Wallets':<30} {len(records):>18,} {len(sb_records):>18,}")

        range_tfry = sum(r["entitled_tfry"] for r in records)
        range_fnode = sum(r["entitled_fnode"] for r in records)
        sb_tfry = sum(r["entitled_tfry"] for r in sb_records)
        sb_fnode = sum(r["entitled_fnode"] for r in sb_records)
        print(f"{'Total entitled tFRY':<30} {range_tfry/MICROUNITS:>18,.2f} {sb_tfry/MICROUNITS:>18,.2f}")
        print(f"{'Total entitled fNODE':<30} {range_fnode/MICROUNITS:>18,.2f} {sb_fnode/MICROUNITS:>18,.2f}")

        range_w_tfry = sum(a["tfry"] for a in weekly.values())
        range_w_fnode = sum(a["fnode"] for a in weekly.values())
        sb_w_tfry = sum(a["tfry"] for a in sb_weekly.values())
        sb_w_fnode = sum(a["fnode"] for a in sb_weekly.values())
        print(f"{'Weekly new tFRY':<30} {range_w_tfry/MICROUNITS:>18,.2f} {sb_w_tfry/MICROUNITS:>18,.2f}")
        print(f"{'Weekly new fNODE':<30} {range_w_fnode/MICROUNITS:>18,.2f} {sb_w_fnode/MICROUNITS:>18,.2f}")

        range_cap_a = cap_report.get("details", {}).get("cap_a", {})
        sb_cap_a = sb_cap.get("details", {}).get("cap_a", {})
        range_cap_d = cap_report.get("details", {}).get("cap_d", {})
        sb_cap_d = sb_cap.get("details", {}).get("cap_d", {})

        a_pass_range = "PASS" if range_cap_a.get("pass_tfry") and range_cap_a.get("pass_fnode") else "FAIL"
        a_pass_sb = "PASS" if sb_cap_a.get("pass_tfry") and sb_cap_a.get("pass_fnode") else "FAIL"
        print(f"{'Cap a (weekly totals)':<30} {a_pass_range:>18} {a_pass_sb:>18}")

        if "flagged" in range_cap_d and "flagged" in sb_cap_d:
            d_pass_range = "PASS" if range_cap_d.get("pass") else "FAIL"
            d_pass_sb = "PASS" if sb_cap_d.get("pass") else "FAIL"
            print(f"{'Cap d (max vs mean)':<30} {d_pass_range:>18} {d_pass_sb:>18}")

    print(f"\n--- CAPS ---")
    for cap_name, cap_data in cap_report.get("details", {}).items():
        status = "PASS" if cap_data.get("pass") else "FAIL"
        print(f"  {cap_name}: {status}")

    # Simulate summary
    if sim_failed > 0 or sim_passed > 0:
        print(f"\n--- SIMULATE ---")
        print(f"Simulate: {sim_passed} groups passed / {sim_failed} groups failed")
        if not sim_all_pass:
            print("LIVE SUBMIT BLOCKED: simulate failures present")

    print(f"\n--- GROUPS ---")
    num_groups = math.ceil(len(records) / WALLETS_PER_GROUP)
    print(f"Groups needed: {num_groups} (max {MAX_GROUPS})")
    print("============================================================")


# ── Main publish entrypoint ──────────────────────────────────────────────


def publish_epoch(
    publish_records: list[dict] | None,
    epoch: int,
    dry_run: bool,
    mongo_uri: str,
    algod_url: str = ALGOD_URL,
    algod_token: str = "",
    app_id: int = APP_ID,
    preseed_path: str = "data/preseed_merkle.json",
    maturation_epochs: int = 4,
    reduction_factor: float = 1.0,
    days: int = 7,
    db_main=None,
    write_history: bool = True,
    export_plan_path: str | None = None,
    ack_cap_d: bool = False,
    skip_advance: bool = False,
    only_missing: bool = False,
) -> None:
    """Publish a batch of wallet records to the contract.

    When called from calculator.reward_calculator, pass publish_records
    from compute_epoch_publish and set write_history=False.
    """
    preseed = load_preseed(preseed_path)

    # Mongo connection
    mongo_client = None
    if db_main is None:
        try:
            mongo_client = MongoClient(mongo_uri)
            db_main = mongo_client["main"]
        except Exception as e:
            log.warning("Mongo connection failed: %s", e)
            db_main = None

    start_dt = datetime.now(timezone.utc) - timedelta(days=days)
    end_dt = datetime.now(timezone.utc)

    # Build/merge publish records
    if publish_records is None:
        if db_main is None:
            log.error("Mongo unavailable and no publish_records provided")
            sys.exit(1)
        weekly, no_wallet_count = query_weekly_rewards(db_main, days=days)
        log.info(
            "Weekly wallets: %d, devices without wallet: %d",
            len(weekly),
            no_wallet_count,
        )
        publish_records = compute_publish_records(
            weekly, preseed, epoch, db_main, maturation_epochs
        )
    else:
        if db_main is None:
            log.error("Mongo unavailable and publish_records provided (need db for preseed merge)")
            sys.exit(1)
        weekly = compute_weekly_from_records(publish_records, epoch, db_main)
        publish_records = compute_publish_records(
            weekly, preseed, epoch, db_main, maturation_epochs
        )

    # Algod connection
    client = get_algod_client(algod_url, algod_token)

    # Caps
    cap_report = check_caps(publish_records, weekly, preseed, client, app_id, db_main)

    if not dry_run:
        details = cap_report.get("details", {})
        # Critical caps — always hard-stop
        critical_fails = []
        for cap_name in ("cap_a", "cap_b", "cap_c", "group_cap"):
            if not details.get(cap_name, {}).get("pass", True):
                critical_fails.append(cap_name)
        if critical_fails:
            print(f"\nCritical cap breach ({', '.join(critical_fails)}) in live mode. Aborting.")
            sys.exit(1)

        # Cap D — requires explicit acknowledgement
        cap_d = details.get("cap_d", {})
        cap_d_flagged = cap_d.get("flagged", [])
        if not cap_d.get("pass", True):
            flagged_addrs = [w["wallet"][:12] + "..." for w in cap_d_flagged[:5]]
            print(f"\nCap D FAIL: {len(cap_d_flagged)} wallets flagged (per-device anomaly)")
            print(f"  Flagged: {', '.join(flagged_addrs)}" + (f" (+{len(cap_d_flagged)-5} more)" if len(cap_d_flagged) > 5 else ""))
            if not ack_cap_d:
                print("  Review flagged wallets, then re-run with --ack-cap-d. Aborting.")
                sys.exit(1)
            else:
                print("  Proceeding per --ack-cap-d.")

    # Query existing boxes
    existing = get_existing_wallets(client, app_id)

    # --only-missing: filter to wallets without an on-chain box
    if only_missing:
        before = len(publish_records)
        publish_records = [
            r for r in publish_records if r["wallet"] not in existing
        ]
        after = len(publish_records)
        print(f"only-missing: {len(existing)} existing boxes, {after} wallets to publish (filtered from {before})")
        if after == 0:
            print("Nothing to publish — all wallets have boxes.")
            if mongo_client:
                mongo_client.close()
            return

    # Export plan for call-batch.html signing (early exit — no group build needed)
    if export_plan_path:
        export_plan(publish_records, existing, epoch, app_id, export_plan_path, client)
        if mongo_client:
            mongo_client.close()
        return

    # Build groups
    groups = build_atomic_groups(publish_records, existing, app_id, ADMIN_ADDR, client)

    sim_all_pass = True
    sim_passed = 0
    sim_failed = 0

    if dry_run:
        if client and groups:
            sim_all_pass, sim_passed, sim_failed = simulate_groups(
                client, groups, algod_url=algod_url, algod_token=algod_token
            )
            if not sim_all_pass:
                print(f"\nSimulate: {sim_passed} groups passed / {sim_failed} groups failed")
        else:
            log.info("Skipping simulate (no algod client or no groups)")

    # Window detection for side-by-side
    side_by_side = None
    if db_main is not None:
        distinct = get_distinct_unlock_at_in_range(db_main, start_dt, end_dt)
        if len(distinct) > 1:
            latest_window = distinct[0]
            weekly_single, _ = query_weekly_rewards_for_window(db_main, latest_window)
            records_single = compute_publish_records(
                weekly_single, preseed, epoch, db_main, maturation_epochs
            )
            weekly_single_delta = compute_weekly_from_records(records_single, epoch, db_main)
            cap_report_single = check_caps(records_single, weekly_single_delta, preseed, client, app_id, db_main)
            side_by_side = {
                "window": latest_window,
                "records": records_single,
                "weekly": weekly_single_delta,
                "cap_report": cap_report_single,
            }

    if dry_run:
        print_dry_run_summary(
            publish_records,
            weekly,
            preseed,
            existing,
            client,
            cap_report,
            epoch,
            start_dt=start_dt,
            end_dt=end_dt,
            db_main=db_main,
            sim_all_pass=sim_all_pass,
            sim_passed=sim_passed,
            sim_failed=sim_failed,
            side_by_side=side_by_side,
            only_missing=only_missing,
            skip_advance=skip_advance,
        )

        if mongo_client:
            mongo_client.close()
        return

    # --live mode --
    acquire_single_launch_lock(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".publish.lock")
    )

    new_boxes = len(publish_records) - len(existing)
    _submit_n = new_boxes if only_missing else len(publish_records)
    _num_groups = (_submit_n + WALLETS_PER_GROUP - 1) // WALLETS_PER_GROUP
    _mbr_budget = new_boxes * MBR_PER_NEW_WALLET
    _fee_budget = _num_groups * MAX_TXNS_PER_GROUP * 1000          # 1000 µA/txn
    if not skip_advance:
        _fee_budget += 2000                                       # advance_epoch app call
    algo_needed = _mbr_budget + _fee_budget
    admin_algo = get_account_balance(client, ADMIN_ADDR)
    app_algo, _, _ = get_app_balances(client, app_id)
    if admin_algo + app_algo < algo_needed:
        print(
            f"ALGO SHORTFALL: need {algo_needed / 1e6:.3f} "
            f"(MBR {_mbr_budget / 1e6:.3f} + fees {_fee_budget / 1e6:.3f}), "
            f"have {(admin_algo + app_algo) / 1e6:.3f}"
        )
        sys.exit(1)

    # Get signer
    mnemonic_val = os.environ.get("AUTHORITY_MNEMONIC", "")
    if not mnemonic_val:
        print("ERROR: AUTHORITY_MNEMONIC env var not set")
        sys.exit(1)
    signer_sk = mnemonic.to_private_key(mnemonic_val)
    derived_addr = account.address_from_private_key(signer_sk)
    log.info("Signer: %s", derived_addr)

    # Submit groups
    stats = submit_groups(
        client, groups, signer_sk, ADMIN_ADDR,
        records=publish_records, app_id=app_id,
    )
    print(f"\nPublished: {stats['wallets']} wallets in {stats['submitted']} groups")
    if stats["failed"] > 0:
        print(f"FAILED groups: {stats['failed']}")
        sys.exit(1)

    # advance_epoch
    if skip_advance:
        print("advance_epoch SKIPPED (--skip-advance)")
    else:
        txid = submit_advance_epoch(client, app_id, signer_sk, ADMIN_ADDR)
        if txid:
            print(f"advance_epoch: {txid}")
        else:
            print("advance_epoch FAILED")
            sys.exit(1)

    # Write history
    now = datetime.now(timezone.utc)
    if write_history and db_main is not None:
        save_epoch_history(db_main, publish_records, now, reduction_factor)
        save_publish_log(db_main, publish_records, epoch, dry_run, now)

    if mongo_client:
        mongo_client.close()

    log.info("Epoch %d published successfully.", epoch)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly epoch publisher for FryMinerRewardPool"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate only, zero submits"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Submit transactions and write to Mongo",
    )
    parser.add_argument(
        "--epoch",
        type=int,
        default=None,
        help="Target epoch (default: current + 1 from chain)",
    )
    parser.add_argument(
        "--mongo-uri",
        default="mongodb://localhost:27017/?tls=true&tlsAllowInvalidCertificates=true",
        help="MongoDB URI",
    )
    parser.add_argument("--algod-url", default=ALGOD_URL, help="Algod URL")
    parser.add_argument("--algod-token", default="", help="Algod API token")
    parser.add_argument(
        "--preseed-path",
        default="data/preseed_merkle.json",
        help="Path to preseed Merkle JSON",
    )
    parser.add_argument(
        "--maturation-epochs", type=int, default=4, help="Maturation epoch count"
    )
    parser.add_argument(
        "--days", type=int, default=7, help="Days back for weekly_rewards unlock_at window"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--export-plan",
        type=str,
        default=None,
        help="Export signing plan JSON for call-batch.html (implies --dry-run, no signing)",
    )
    parser.add_argument(
        "--ack-cap-d",
        action="store_true",
        default=False,
        help="Acknowledge Cap D (per-device anomaly) failures and proceed with --live",
    )
    parser.add_argument(
        "--skip-advance",
        action="store_true",
        default=False,
        help="Skip advance_epoch after publishing (for recovery when epoch already advanced)",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        default=False,
        help="Publish only wallets without an existing on-chain box (recovery filter)",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.live and not args.export_plan:
        parser.print_usage()
        print("ERROR: specify --dry-run, --live, or --export-plan <path>")
        sys.exit(1)

    # --export-plan implies dry-run behavior (no signing/submit)
    if args.export_plan:
        args.dry_run = True

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    _logfile = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"publish-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log",
    )
    _fh = logging.FileHandler(_logfile)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(_fh)
    log.info("Progress log: %s", _logfile)

    # Determine epoch from chain if not provided
    epoch = args.epoch
    if epoch is None:
        client = get_algod_client(args.algod_url, args.algod_token)
        if client:
            try:
                app_info = client.application_info(APP_ID)
                gs = {}
                for kv in app_info["params"]["global-state"]:
                    key = base64.b64decode(kv["key"]).decode("utf-8", errors="replace")
                    if kv["value"]["type"] == 2:
                        gs[key] = kv["value"]["uint"]
                current_epoch = gs.get("current_epoch", 0)
                epoch = current_epoch + 1
                log.info(
                    "Current on-chain epoch: %d, publishing epoch: %d",
                    current_epoch,
                    epoch,
                )
            except Exception as e:
                log.warning("Could not read current epoch from chain: %s", e)
                epoch = 1
        else:
            epoch = 1

    publish_epoch(
        publish_records=None,
        epoch=epoch,
        dry_run=args.dry_run,
        mongo_uri=args.mongo_uri,
        algod_url=args.algod_url,
        algod_token=args.algod_token,
        preseed_path=args.preseed_path,
        maturation_epochs=args.maturation_epochs,
        days=args.days,
        write_history=True,
        export_plan_path=args.export_plan,
        ack_cap_d=args.ack_cap_d,
        skip_advance=args.skip_advance,
        only_missing=args.only_missing,
    )


if __name__ == "__main__":
    main()

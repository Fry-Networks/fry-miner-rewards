"""Unit tests for reward calculator logic."""

import math
from datetime import datetime, timezone

import pytest

from calculator.config import (
    FNODE_ASA_ID,
    MICROUNITS,
    TFRY_ASA_ID,
)
from calculator.reward_calculator import (
    apply_poc_factor,
    compute_base_reward,
    compute_device_reward,
    compute_weekly_rewards,
    get_wallet,
    is_device_eligible,
    miner_code_from_key,
    to_microunits,
)

NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _make_product(key: str, unverified: float, verified: float, reward_token: str) -> dict:
    return {
        "key": key,
        "reward": {
            "unverified": unverified,
            "verified": verified,
            "tokens": {"reward": reward_token},
        },
    }


def _make_device(
    miner_key: str,
    is_registered: bool = True,
    created_at: datetime | None = None,
    staked: dict | None = None,
    byod: str = "",
    reward_wallet: str = "",
    address: str = "OWNER_ADDR",
    activated: bool = False,
    rewards_exception: dict | None = None,
) -> dict:
    d: dict = {
        "miner_key": miner_key,
        "is_registered": is_registered,
        "address": address,
    }
    if created_at:
        d["created_at"] = created_at
    if staked:
        d["staked"] = staked
    if byod:
        d["byod"] = byod
    if reward_wallet:
        d["reward_wallet"] = reward_wallet
    if activated:
        d["activated"] = activated
    if rewards_exception:
        d["rewards_exception"] = rewards_exception
    return d


# ── miner_code_from_key ─────────────────────────────────────────────


class TestMinerCodeFromKey:
    def test_standard(self):
        assert miner_code_from_key("BM-ABC123") == "BM"

    def test_long_code(self):
        assert miner_code_from_key("VRDN-XYZ") == "VRDN"

    def test_no_dash(self):
        assert miner_code_from_key("INVALID") == ""


# ── is_device_eligible ───────────────────────────────────────────────


class TestDeviceEligibility:
    def test_not_registered(self):
        device = _make_device("BM-001", is_registered=False)
        assert is_device_eligible(device, "BM", NOW) is False

    def test_regular_miner_eligible(self):
        device = _make_device("BM-001", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert is_device_eligible(device, "BM", NOW) is True

    def test_regular_miner_future_created_at(self):
        device = _make_device("BM-001", created_at=datetime(2027, 1, 1, tzinfo=timezone.utc))
        assert is_device_eligible(device, "BM", NOW) is False

    def test_node_no_staking_required(self):
        """Nodes are eligible without any staking — staking was disabled."""
        device = _make_device("RDN-001")
        assert is_device_eligible(device, "RDN", NOW) is True

    def test_sdn_eligible_without_staking(self):
        device = _make_device("SDN-001")
        assert is_device_eligible(device, "SDN", NOW) is True

    def test_aem_no_staking_required(self):
        device = _make_device("AEM-001")
        assert is_device_eligible(device, "AEM", NOW) is True

    def test_virtual_node_requires_activated(self):
        device = _make_device("VRDN-001", activated=False)
        assert is_device_eligible(device, "VRDN", NOW) is False

    def test_virtual_node_activated(self):
        device = _make_device("VRDN-001", activated=True)
        assert is_device_eligible(device, "VRDN", NOW) is True

    def test_rewards_exception_blocks(self):
        device = _make_device(
            "BM-001",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            rewards_exception={"enabled": True},
        )
        assert is_device_eligible(device, "BM", NOW) is False

    def test_rewards_exception_expired(self):
        device = _make_device(
            "BM-001",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            rewards_exception={
                "enabled": True,
                "expires_at": datetime(2026, 1, 1, tzinfo=timezone.utc),  # expired
            },
        )
        assert is_device_eligible(device, "BM", NOW) is True


# ── compute_base_reward ──────────────────────────────────────────────


class TestBaseReward:
    def test_no_staking(self):
        product = _make_product("BM", unverified=22.89, verified=59.52, reward_token=TFRY_ASA_ID)
        device = _make_device("BM-001")
        assert compute_base_reward(device, product, NOW) == 22.89

    def test_staking_type_one(self):
        product = _make_product("BM", unverified=22.89, verified=59.52, reward_token=TFRY_ASA_ID)
        device = _make_device("BM-001", staked={
            "type": "one", "amount": 100, "time": datetime(2025, 6, 1, tzinfo=timezone.utc)
        })
        # 59.52 * 1.5 = 89.28
        assert compute_base_reward(device, product, NOW) == 89.28

    def test_staking_type_two(self):
        product = _make_product("BM", unverified=22.89, verified=59.52, reward_token=TFRY_ASA_ID)
        device = _make_device("BM-001", staked={
            "type": "two", "amount": 100, "time": datetime(2025, 6, 1, tzinfo=timezone.utc)
        })
        # 59.52 * 3.0 = 178.56
        assert compute_base_reward(device, product, NOW) == 178.56

    def test_staking_future_time(self):
        """Staking time in future → not yet active → use unverified rate."""
        product = _make_product("BM", unverified=22.89, verified=59.52, reward_token=TFRY_ASA_ID)
        device = _make_device("BM-001", staked={
            "type": "two", "amount": 100, "time": datetime(2027, 1, 1, tzinfo=timezone.utc)
        })
        assert compute_base_reward(device, product, NOW) == 22.89

    def test_staking_zero_amount(self):
        """Staking amount 0 → not active → use unverified rate."""
        product = _make_product("BM", unverified=22.89, verified=59.52, reward_token=TFRY_ASA_ID)
        device = _make_device("BM-001", staked={
            "type": "two", "amount": 0, "time": datetime(2025, 6, 1, tzinfo=timezone.utc)
        })
        assert compute_base_reward(device, product, NOW) == 22.89

    def test_byod_reduction(self):
        product = _make_product("BM", unverified=22.89, verified=59.52, reward_token=TFRY_ASA_ID)
        device = _make_device("BM-001", byod="1")
        # 22.89 * 0.5 = 11.445 → round_two (JS-style round half up) = 11.45
        assert compute_base_reward(device, product, NOW) == 11.45

    def test_byod_with_staking(self):
        product = _make_product("BM", unverified=22.89, verified=59.52, reward_token=TFRY_ASA_ID)
        device = _make_device("BM-001", byod="1", staked={
            "type": "two", "amount": 100, "time": datetime(2025, 6, 1, tzinfo=timezone.utc)
        })
        # 59.52 * 3.0 = 178.56, then * 0.5 = 89.28
        assert compute_base_reward(device, product, NOW) == 89.28

    def test_aem_reward_rate(self):
        product = _make_product("AEM", unverified=495.0, verified=990.0, reward_token=TFRY_ASA_ID)
        device = _make_device("AEM-001", staked={
            "type": "two", "amount": 100, "time": datetime(2025, 6, 1, tzinfo=timezone.utc)
        })
        # 990 * 3.0 = 2970.0
        assert compute_base_reward(device, product, NOW) == 2970.0


# ── apply_poc_factor ─────────────────────────────────────────────────


class TestPocFactor:
    def test_installer_eligible(self):
        poc = {"reward_eligible": True, "rewards_multiplier_day": 0.9}
        result = apply_poc_factor(100.0, "BM", poc)
        assert result == 90.0

    def test_installer_not_eligible(self):
        poc = {"reward_eligible": False, "rewards_multiplier_day": 0.9}
        assert apply_poc_factor(100.0, "BM", poc) == 0.0

    def test_installer_no_poc_doc(self):
        assert apply_poc_factor(100.0, "BM", None) == 0.0

    def test_non_installer_no_poc_gating(self):
        """SDN/SVN/CN get full reward regardless of PoC."""
        assert apply_poc_factor(119.04, "SDN", None) == 119.04

    def test_virtual_no_poc_gating(self):
        assert apply_poc_factor(119.04, "VRDN", None) == 119.04

    def test_installer_factor_clamped(self):
        poc = {"reward_eligible": True, "rewards_multiplier_day": 1.5}
        result = apply_poc_factor(100.0, "BM", poc)
        assert result == 100.0  # clamped to 1.0


# ── get_wallet ───────────────────────────────────────────────────────


class TestGetWallet:
    def test_reward_wallet_present(self):
        device = _make_device("BM-001", reward_wallet="REWARD_ADDR", address="OWNER_ADDR")
        assert get_wallet(device) == "REWARD_ADDR"

    def test_reward_wallet_empty(self):
        device = _make_device("BM-001", address="OWNER_ADDR")
        assert get_wallet(device) == "OWNER_ADDR"

    def test_reward_wallet_whitespace(self):
        device = {"miner_key": "BM-001", "reward_wallet": "  ", "address": "OWNER_ADDR"}
        assert get_wallet(device) == "OWNER_ADDR"


# ── to_microunits ────────────────────────────────────────────────────


class TestMicrounits:
    def test_whole_number(self):
        assert to_microunits(100.0) == 100_000_000

    def test_fraction(self):
        assert to_microunits(22.89) == 22_890_000

    def test_floor(self):
        # 0.0000001 = 0.1 microunit → floor to 0
        assert to_microunits(0.0000001) == 0

    def test_large_amount(self):
        assert to_microunits(990.0 * 7) == 6_930_000_000


# ── compute_device_reward ────────────────────────────────────────────


class TestComputeDeviceReward:
    def test_eligible_bm_with_poc(self):
        product = _make_product("BM", 22.89, 59.52, TFRY_ASA_ID)
        device = _make_device("BM-001", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        poc = {"reward_eligible": True, "rewards_multiplier_day": 0.95}
        amount, token = compute_device_reward(device, product, poc, NOW)
        assert token == TFRY_ASA_ID
        assert amount == round(22.89 * 0.95, 2)  # 21.75

    def test_ineligible_miner_code(self):
        product = _make_product("FNEB", 0, 0, "none")  # merchandise
        device = _make_device("FNEB-001", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        amount, token = compute_device_reward(device, product, None, NOW)
        assert amount == 0.0
        assert token is None

    def test_fnode_token_routing(self):
        product = _make_product("RDN", 59.52, 119.04, FNODE_ASA_ID)
        device = _make_device("RDN-001", staked={
            "type": "two", "amount": 500, "time": datetime(2025, 1, 1, tzinfo=timezone.utc)
        })
        poc = {"reward_eligible": True, "rewards_multiplier_day": 1.0}
        amount, token = compute_device_reward(device, product, poc, NOW)
        assert token == FNODE_ASA_ID
        assert amount == round(119.04 * 3.0, 2)  # 357.12


# ── compute_weekly_rewards ───────────────────────────────────────────


class TestComputeWeeklyRewards:
    def test_aggregation_by_wallet(self):
        products = {
            "BM": _make_product("BM", 22.89, 59.52, TFRY_ASA_ID),
            "SDN": _make_product("SDN", 59.52, 119.04, FNODE_ASA_ID),  # SDN = NON_INSTALLER
        }
        devices = [
            _make_device("BM-001", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                         reward_wallet="WALLET_A", address="ADDR_A"),
            _make_device("BM-002", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                         reward_wallet="WALLET_A", address="ADDR_A"),  # same wallet
            _make_device("SDN-001", reward_wallet="WALLET_B", address="ADDR_B"),  # NON_INSTALLER node
        ]
        poc = {
            "BM-001": {"reward_eligible": True, "rewards_multiplier_day": 1.0},
            "BM-002": {"reward_eligible": True, "rewards_multiplier_day": 0.5},
        }

        result = compute_weekly_rewards(products, devices, poc, NOW, days=7)

        assert "WALLET_A" in result
        # BM-001: 22.89 * 1.0 = 22.89/day, BM-002: round_two(22.89 * 0.5) = 11.45/day
        # Weekly: 22.89*7=160.23, 11.45*7=80.15, sum=240.38
        assert result["WALLET_A"]["tfry"] == to_microunits(round(22.89 * 7, 2) + round(11.45 * 7, 2))

        assert "WALLET_B" in result
        # SDN = NON_INSTALLER, no PoC gating: 59.52/day * 7 = 416.64
        expected_fnode = to_microunits(round(59.52 * 7, 2))
        assert result["WALLET_B"]["fnode"] == expected_fnode

    def test_wallet_fallback_to_address(self):
        products = {"BM": _make_product("BM", 22.89, 59.52, TFRY_ASA_ID)}
        devices = [
            _make_device("BM-001", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                         address="FALLBACK_ADDR"),
        ]
        poc = {"BM-001": {"reward_eligible": True, "rewards_multiplier_day": 1.0}}

        result = compute_weekly_rewards(products, devices, poc, NOW, days=7)
        assert "FALLBACK_ADDR" in result

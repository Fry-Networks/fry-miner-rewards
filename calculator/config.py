"""Shared constants for the reward calculator."""

# ASA IDs (Algorand Standard Assets)
TFRY_ASA_ID = "2681521901"
FNODE_ASA_ID = "2485202024"
FRY20_ASA_ID = "2485314946"

# Token decimals (all 6)
DECIMALS = 6
MICROUNITS = 10**DECIMALS

# PoC INSTALLER codes — from poc_eligibility.py on ZEUS00
# These devices get slot-based PoC pro-rating via reward_eligible flag
INSTALLER_CODES = frozenset({"BM", "IDM", "ODM", "ISM", "OSM", "RDN", "AEM"})

# NON_INSTALLER reward-earning types — earn rewards but NO PoC slot gating
# SDN, SVN, CN: node types not in poc_eligibility.py INSTALLER list
# IRM: excluded from V1 (IoT-adjacent, no active PoC pipeline)
NON_INSTALLER_REWARD_CODES = frozenset({"SDN", "SVN", "CN"})

# Virtual node codes — mirror of physical nodes, no PoC gating
VIRTUAL_NODE_CODES = frozenset({"VRDN", "VSDN", "VSVN"})

# All codes that earn rewards
ALL_REWARD_CODES = INSTALLER_CODES | NON_INSTALLER_REWARD_CODES | VIRTUAL_NODE_CODES

# Device type groupings for eligibility rules
NODE_CODES = frozenset({"RDN", "SDN", "SVN", "CN"})
AEM_CODES = frozenset({"AEM"})

# Verification staking multipliers (applied to product.reward.verified)
STAKING_MULTIPLIERS = {"one": 1.5, "two": 3.0}

# BYOD (Bring Your Own Device) reward reduction
BYOD_FACTOR = 0.5

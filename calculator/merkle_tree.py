"""Merkle tree construction and proof generation for preseed claims.

Builds a binary Merkle tree from wallet entitlement data.
Each leaf = SHA256(wallet_addr_32bytes || entitled_tfry_8bytes || entitled_fnode_8bytes
                   || matured_tfry_8bytes || matured_fnode_8bytes).
Tree is zero-padded to next power of 2. Depth = 12 (supports up to 4096 wallets).

Usage:
    tree = MerkleTree.from_preseed(wallet_amounts)
    root = tree.root
    proof = tree.get_proof(wallet_address)
    assert MerkleTree.verify_proof(root, leaf_hash, proof, leaf_index)
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

TREE_DEPTH = 12
MAX_LEAVES = 2**TREE_DEPTH  # 4096

ZERO_HASH = b"\x00" * 32


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def encode_leaf(
    wallet_bytes: bytes,
    entitled_tfry: int,
    entitled_fnode: int,
    matured_tfry: int,
    matured_fnode: int,
) -> bytes:
    """Encode leaf data matching on-chain format: addr(32) || 4 x uint64(8) big-endian."""
    assert len(wallet_bytes) == 32, f"wallet must be 32 bytes, got {len(wallet_bytes)}"
    return (
        wallet_bytes
        + struct.pack(">Q", entitled_tfry)
        + struct.pack(">Q", entitled_fnode)
        + struct.pack(">Q", matured_tfry)
        + struct.pack(">Q", matured_fnode)
    )


def hash_leaf(
    wallet_bytes: bytes,
    entitled_tfry: int,
    entitled_fnode: int,
    matured_tfry: int,
    matured_fnode: int,
) -> bytes:
    """Hash a leaf to 32 bytes."""
    return _sha256(encode_leaf(wallet_bytes, entitled_tfry, entitled_fnode, matured_tfry, matured_fnode))


@dataclass
class MerkleProof:
    """Proof for a single leaf in the tree."""

    leaf_index: int
    siblings: list[bytes]  # 12 x 32-byte hashes
    leaf_hash: bytes

    def to_bytes(self) -> bytes:
        """Concatenate siblings into 384-byte proof."""
        return b"".join(self.siblings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "leaf_index": self.leaf_index,
            "proof": self.to_bytes().hex(),
            "leaf_hash": self.leaf_hash.hex(),
        }


class MerkleTree:
    """Binary Merkle tree with SHA256 hashing."""

    def __init__(self, leaves: list[bytes]) -> None:
        if len(leaves) > MAX_LEAVES:
            raise ValueError(f"Too many leaves: {len(leaves)} > {MAX_LEAVES}")

        self._leaf_count = len(leaves)
        padded = list(leaves) + [ZERO_HASH] * (MAX_LEAVES - len(leaves))
        self._layers: list[list[bytes]] = [padded]
        self._build()

    def _build(self) -> None:
        """Build tree bottom-up."""
        current = self._layers[0]
        for _ in range(TREE_DEPTH):
            next_layer = []
            for i in range(0, len(current), 2):
                left = current[i]
                right = current[i + 1]
                next_layer.append(_sha256(left + right))
            self._layers.append(next_layer)
            current = next_layer
        assert len(self._layers[-1]) == 1

    @property
    def root(self) -> bytes:
        return self._layers[-1][0]

    @property
    def leaf_count(self) -> int:
        return self._leaf_count

    def get_proof(self, leaf_index: int) -> MerkleProof:
        """Generate Merkle proof for leaf at given index."""
        if leaf_index >= self._leaf_count:
            raise ValueError(f"Leaf index {leaf_index} >= leaf count {self._leaf_count}")

        siblings = []
        idx = leaf_index
        for level in range(TREE_DEPTH):
            sibling_idx = idx ^ 1
            siblings.append(self._layers[level][sibling_idx])
            idx >>= 1

        return MerkleProof(
            leaf_index=leaf_index,
            siblings=siblings,
            leaf_hash=self._layers[0][leaf_index],
        )

    @staticmethod
    def verify_proof(root: bytes, leaf_hash: bytes, proof: MerkleProof) -> bool:
        """Verify a Merkle proof against a root."""
        computed = leaf_hash
        idx = proof.leaf_index
        for level in range(TREE_DEPTH):
            sibling = proof.siblings[level]
            if idx & 1:
                computed = _sha256(sibling + computed)
            else:
                computed = _sha256(computed + sibling)
            idx >>= 1
        return computed == root

    @classmethod
    def from_preseed(cls, wallet_amounts: dict[str, dict[str, int]]) -> tuple["MerkleTree", dict[str, int]]:
        """Build tree from preseed data.

        Args:
            wallet_amounts: {wallet_addr_str: {"tfry": microunits, "fnode": microunits}}

        Returns:
            (tree, wallet_index_map) where wallet_index_map maps wallet_addr → leaf_index.
            Wallets sorted alphabetically for deterministic ordering.
        """
        from algosdk import encoding

        sorted_wallets = sorted(wallet_amounts.keys())
        leaves = []
        wallet_index = {}

        for i, wallet_addr in enumerate(sorted_wallets):
            amounts = wallet_amounts[wallet_addr]
            tfry = amounts["tfry"]
            fnode = amounts["fnode"]
            wallet_bytes = encoding.decode_address(wallet_addr)
            leaf = hash_leaf(wallet_bytes, tfry, fnode, tfry, fnode)  # matured = entitled for preseed
            leaves.append(leaf)
            wallet_index[wallet_addr] = i

        tree = cls(leaves)
        return tree, wallet_index

    def export_proofs(self, wallet_amounts: dict[str, dict[str, int]], path: str) -> None:
        """Export all proofs to JSON file."""
        from algosdk import encoding

        tree, wallet_index = MerkleTree.from_preseed(wallet_amounts)
        proofs = {}

        for wallet_addr, idx in wallet_index.items():
            proof = tree.get_proof(idx)
            amounts = wallet_amounts[wallet_addr]
            proofs[wallet_addr] = {
                "leaf_index": idx,
                "proof": proof.to_bytes().hex(),
                "entitled_tfry": amounts["tfry"],
                "entitled_fnode": amounts["fnode"],
            }

        with open(path, "w") as f:
            json.dump({"root": tree.root.hex(), "wallets": proofs}, f, indent=2)

"""Unit tests for Merkle tree construction and proof verification."""

import hashlib
import struct

import pytest

from calculator.merkle_tree import (
    TREE_DEPTH,
    ZERO_HASH,
    MerkleTree,
    encode_leaf,
    hash_leaf,
)


def _make_wallet_bytes(n: int) -> bytes:
    """Create a deterministic 32-byte 'wallet address' from an integer."""
    return hashlib.sha256(f"wallet-{n}".encode()).digest()


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


class TestEncodeLeaf:
    def test_length(self):
        wb = b"\x01" * 32
        data = encode_leaf(wb, 1000, 2000, 1000, 2000)
        assert len(data) == 64  # 32 + 4*8

    def test_encoding_matches_abi(self):
        wb = b"\xab" * 32
        tfry, fnode = 1_000_000, 2_000_000
        data = encode_leaf(wb, tfry, fnode, tfry, fnode)
        assert data[:32] == wb
        assert struct.unpack(">Q", data[32:40])[0] == tfry
        assert struct.unpack(">Q", data[40:48])[0] == fnode
        assert struct.unpack(">Q", data[48:56])[0] == tfry
        assert struct.unpack(">Q", data[56:64])[0] == fnode

    def test_bad_wallet_length(self):
        with pytest.raises(AssertionError):
            encode_leaf(b"\x01" * 31, 0, 0, 0, 0)


class TestHashLeaf:
    def test_deterministic(self):
        wb = _make_wallet_bytes(1)
        h1 = hash_leaf(wb, 100, 200, 100, 200)
        h2 = hash_leaf(wb, 100, 200, 100, 200)
        assert h1 == h2
        assert len(h1) == 32

    def test_different_amounts_differ(self):
        wb = _make_wallet_bytes(1)
        h1 = hash_leaf(wb, 100, 200, 100, 200)
        h2 = hash_leaf(wb, 101, 200, 101, 200)
        assert h1 != h2


class TestMerkleTree:
    def test_single_leaf(self):
        leaf = _sha256(b"leaf0")
        tree = MerkleTree([leaf])
        assert tree.leaf_count == 1
        assert len(tree.root) == 32

    def test_two_leaves(self):
        l0 = _sha256(b"leaf0")
        l1 = _sha256(b"leaf1")
        tree = MerkleTree([l0, l1])
        assert tree.leaf_count == 2

    def test_root_deterministic(self):
        leaves = [_sha256(f"leaf{i}".encode()) for i in range(10)]
        t1 = MerkleTree(leaves)
        t2 = MerkleTree(leaves)
        assert t1.root == t2.root

    def test_root_changes_with_data(self):
        leaves_a = [_sha256(f"leaf{i}".encode()) for i in range(10)]
        leaves_b = [_sha256(f"leaf{i}".encode()) for i in range(11)]
        assert MerkleTree(leaves_a).root != MerkleTree(leaves_b).root

    def test_max_leaves(self):
        leaves = [_sha256(f"leaf{i}".encode()) for i in range(4096)]
        tree = MerkleTree(leaves)
        assert tree.leaf_count == 4096

    def test_too_many_leaves(self):
        leaves = [ZERO_HASH] * 4097
        with pytest.raises(ValueError, match="Too many leaves"):
            MerkleTree(leaves)


class TestProofGeneration:
    @pytest.fixture
    def small_tree(self):
        leaves = [_sha256(f"leaf{i}".encode()) for i in range(100)]
        return MerkleTree(leaves), leaves

    def test_proof_length(self, small_tree):
        tree, _ = small_tree
        proof = tree.get_proof(0)
        assert len(proof.siblings) == TREE_DEPTH
        assert all(len(s) == 32 for s in proof.siblings)

    def test_proof_bytes_length(self, small_tree):
        tree, _ = small_tree
        proof = tree.get_proof(0)
        assert len(proof.to_bytes()) == TREE_DEPTH * 32  # 384

    def test_invalid_index(self, small_tree):
        tree, _ = small_tree
        with pytest.raises(ValueError, match="Leaf index"):
            tree.get_proof(100)  # only 100 leaves, index 100 is out


class TestProofVerification:
    @pytest.fixture
    def tree_with_leaves(self):
        leaves = [_sha256(f"leaf{i}".encode()) for i in range(3290)]
        tree = MerkleTree(leaves)
        return tree, leaves

    def test_valid_proof_first_leaf(self, tree_with_leaves):
        tree, leaves = tree_with_leaves
        proof = tree.get_proof(0)
        assert MerkleTree.verify_proof(tree.root, leaves[0], proof)

    def test_valid_proof_last_leaf(self, tree_with_leaves):
        tree, leaves = tree_with_leaves
        proof = tree.get_proof(3289)
        assert MerkleTree.verify_proof(tree.root, leaves[3289], proof)

    def test_valid_proof_middle_leaf(self, tree_with_leaves):
        tree, leaves = tree_with_leaves
        proof = tree.get_proof(1645)
        assert MerkleTree.verify_proof(tree.root, leaves[1645], proof)

    def test_valid_proof_all_positions(self, tree_with_leaves):
        """Spot-check 50 random positions."""
        tree, leaves = tree_with_leaves
        import random
        random.seed(42)
        for idx in random.sample(range(3290), 50):
            proof = tree.get_proof(idx)
            assert MerkleTree.verify_proof(tree.root, leaves[idx], proof), f"Failed at index {idx}"

    def test_wrong_leaf_fails(self, tree_with_leaves):
        tree, leaves = tree_with_leaves
        proof = tree.get_proof(0)
        wrong_leaf = _sha256(b"wrong")
        assert not MerkleTree.verify_proof(tree.root, wrong_leaf, proof)

    def test_wrong_root_fails(self, tree_with_leaves):
        tree, leaves = tree_with_leaves
        proof = tree.get_proof(0)
        wrong_root = _sha256(b"wrong_root")
        assert not MerkleTree.verify_proof(wrong_root, leaves[0], proof)

    def test_swapped_proof_fails(self, tree_with_leaves):
        """Proof for leaf 0 should not verify leaf 1."""
        tree, leaves = tree_with_leaves
        proof_0 = tree.get_proof(0)
        assert not MerkleTree.verify_proof(tree.root, leaves[1], proof_0)

    def test_proof_dict_roundtrip(self, tree_with_leaves):
        tree, leaves = tree_with_leaves
        proof = tree.get_proof(42)
        d = proof.to_dict()
        assert d["leaf_index"] == 42
        assert len(bytes.fromhex(d["proof"])) == 384
        assert len(bytes.fromhex(d["leaf_hash"])) == 32


class TestFromPreseed:
    def test_basic_construction(self):
        """Test tree construction from preseed-format wallet data."""
        from algosdk import account

        wallet_amounts = {}
        for i in range(10):
            sk = account.generate_account()[0]
            addr = account.address_from_private_key(sk)
            wallet_amounts[addr] = {"tfry": (i + 1) * 1_000_000, "fnode": (i + 1) * 500_000}

        tree, wallet_index = MerkleTree.from_preseed(wallet_amounts)
        assert tree.leaf_count == 10
        assert len(wallet_index) == 10
        assert len(tree.root) == 32

        # Verify sorting
        sorted_addrs = sorted(wallet_amounts.keys())
        for addr in sorted_addrs:
            assert addr in wallet_index

    def test_proof_verifies_for_preseed_wallet(self):
        """End-to-end: build tree from preseed, verify proof for specific wallet."""
        from algosdk import account, encoding

        wallet_amounts = {}
        for i in range(50):
            sk = account.generate_account()[0]
            addr = account.address_from_private_key(sk)
            wallet_amounts[addr] = {"tfry": (i + 1) * 1_000_000, "fnode": (i + 1) * 500_000}

        tree, wallet_index = MerkleTree.from_preseed(wallet_amounts)

        # Pick a random wallet and verify its proof
        target_addr = list(wallet_amounts.keys())[25]
        idx = wallet_index[target_addr]
        proof = tree.get_proof(idx)

        # Reconstruct the expected leaf
        amounts = wallet_amounts[target_addr]
        wallet_bytes = encoding.decode_address(target_addr)
        expected_leaf = hash_leaf(
            wallet_bytes, amounts["tfry"], amounts["fnode"],
            amounts["tfry"], amounts["fnode"],
        )

        assert MerkleTree.verify_proof(tree.root, expected_leaf, proof)

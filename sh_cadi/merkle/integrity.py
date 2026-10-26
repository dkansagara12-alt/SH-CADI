import hashlib
from sh_cadi.merkle.merkle_tree import MerkleTree

from sh_cadi.merkle.record_digest import record_digest as _leaf_hash


class IntegritySystem:
    def __init__(self, cas):
        self.cas = cas
        self.baseline_root = None
        self.baseline_leaves = {}    
        self.baseline_tree = None

    def _current_leaves(self) -> dict:
        entries = sorted(self.cas.hash_table.items(), key=lambda kv: kv[0])
        return {chunk_hash: _leaf_hash(chunk_hash, entry) for chunk_hash, entry in entries}

    def generate_root_hash(self) -> str:
        self.baseline_leaves = self._current_leaves()
        self.baseline_tree = MerkleTree(list(self.baseline_leaves.values()))
        self.baseline_root = self.baseline_tree.get_root()
        return self.baseline_root

    def verify_integrity(self) -> dict:
        if self.baseline_root is None:
            raise RuntimeError(
                "No baseline established yet — call generate_root_hash() first."
            )

        current_leaves = self._current_leaves()
        current_tree = MerkleTree(list(current_leaves.values()))
        current_root = current_tree.get_root()

        return {
            "valid": current_root == self.baseline_root,
            "baseline_root": self.baseline_root,
            "current_root": current_root,
        }

    def diagnose_changes(self) -> dict:
        current_leaves = self._current_leaves()
        baseline_keys = set(self.baseline_leaves.keys())
        current_keys = set(current_leaves.keys())

        changed = [
            ch for ch in (baseline_keys & current_keys)
            if self.baseline_leaves[ch] != current_leaves[ch]
        ]
        added = sorted(current_keys - baseline_keys)
        removed = sorted(baseline_keys - current_keys)

        return {"changed": sorted(changed), "added": added, "removed": removed}

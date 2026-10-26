import hashlib

def _pair_hash(left: str, right: str) -> str:
    return hashlib.sha256((left + right).encode()).hexdigest()

class MerkleTree:
    def __init__(self, leaf_hashes: list):
        if not leaf_hashes:
            raise ValueError("Cannot build a Merkle tree with zero leaves")

        self.levels = [list(leaf_hashes)]
        current = list(leaf_hashes)

        while len(current) > 1:
            next_level = []
            for i in range(0, len(current), 2):
                left = current[i]
                right = current[i + 1] if i + 1 < len(current) else current[i]
                next_level.append(_pair_hash(left, right))
            self.levels.append(next_level)
            current = next_level

        self.root = current[0]

    def get_root(self) -> str:
        return self.root

    def get_leaves(self) -> list:
        return list(self.levels[0])

    def height(self) -> int:
        return len(self.levels) - 1

    def get_proof(self, leaf_index: int) -> list:
        if not (0 <= leaf_index < len(self.levels[0])):
            raise IndexError("leaf_index out of range")

        proof = []
        idx = leaf_index
        for level in self.levels[:-1]:
            is_right_node = idx % 2 == 1
            sibling_idx = idx - 1 if is_right_node else idx + 1
            if sibling_idx >= len(level):
                sibling_idx = idx
            sibling_hash = level[sibling_idx]
            position = "L" if is_right_node else "R"
            proof.append((sibling_hash, position))
            idx //= 2
        return proof

    @staticmethod
    def verify_proof(leaf_hash: str, proof: list, expected_root: str) -> bool:
        current = leaf_hash
        for sibling_hash, position in proof:
            if position == "R":
                current = _pair_hash(current, sibling_hash)
            else:
                current = _pair_hash(sibling_hash, current)
        return current == expected_root

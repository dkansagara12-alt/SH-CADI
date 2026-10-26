"""Bucketed Merkle tree: a FIXED-SHAPE authenticated tree over hash-prefix buckets.

Why not the Phase-1 sorted-leaf tree?
  * Phase 1 leaves were sorted by hash, so one insert shifted every later leaf
    and forced a full rebuild.  Here a record's leaf position is a pure function
    of its hash (top `depth` bits of the SHA-256 prefix), so positions never move.
  * The tree is a perfect binary tree with 2**depth leaves, so there is no
    odd-node duplication (a known weakness of the Phase-1 construction).
  * Leaves and internal nodes are domain-separated ("bucket:" / "node:").
  * The same prefix partition can later shard the recovery journal (Phase 2b).

Trust model
  * `trusted_root` is the anchor: assumed to be stored safely (e.g. signed / kept
    elsewhere) and updated ONLY by legitimate upserts/removes.
  * `levels` (stored leaves + internal nodes) and `records` (per-record digests)
    are untrusted caches that fault injection may damage.
"""
import hashlib

EMPTY_BUCKET = hashlib.sha256(b"bucket:empty").hexdigest()


class CorruptionDetected(Exception):
    """Raised when a write would build on an inconsistent stored path."""


def node_hash(left: str, right: str) -> str:
    return hashlib.sha256(b"node:" + left.encode() + right.encode()).hexdigest()


def bucket_digest(records: dict) -> str:
    """Digest of one bucket = SHA-256 over its record digests in sorted-hash order."""
    if not records:
        return EMPTY_BUCKET
    h = hashlib.sha256(b"bucket:")
    for k in sorted(records):
        h.update(records[k].encode())
    return h.hexdigest()


def build_levels(leaves: list) -> list:
    """Build all levels bottom-up from a power-of-two list of leaves. O(B)."""
    levels = [list(leaves)]
    cur = levels[0]
    while len(cur) > 1:
        cur = [node_hash(cur[i], cur[i + 1]) for i in range(0, len(cur), 2)]
        levels.append(cur)
    return levels


def depth_for(n_records: int, target_per_bucket: int = 4) -> int:
    """Pick a depth so buckets hold ~target_per_bucket records on average."""
    depth, cap = 1, 2
    while cap * target_per_bucket < n_records:
        depth += 1
        cap <<= 1
    return depth


class BucketMerkleTree:
    def __init__(self, depth: int = 10):
        if not 1 <= depth <= 24:
            raise ValueError("depth must be in 1..24")
        self.depth = depth
        self.num_buckets = 1 << depth
        self.records = {}            # bucket -> {chunk_hash: record_digest}
        self.levels = []             # stored tree (level 0 = bucket leaves)
        e = EMPTY_BUCKET
        for lvl in range(depth + 1):
            self.levels.append([e] * (self.num_buckets >> lvl))
            e = node_hash(e, e)
        self.trusted_root = self.levels[-1][0]

    # ------------------------------------------------------------------ helpers
    def bucket_of(self, chunk_hash: str) -> int:
        return int(chunk_hash[:8], 16) >> (32 - self.depth)

    @property
    def root(self) -> str:
        """Root as currently *stored* (untrusted cache)."""
        return self.levels[-1][0]

    def total_records(self) -> int:
        return sum(len(r) for r in self.records.values())

    # ------------------------------------------------------------------ build
    def bulk_load(self, items) -> None:
        """Build from (chunk_hash, record_digest) pairs. Time O(n + B)."""
        self.records = {}
        for h, d in items:
            self.records.setdefault(self.bucket_of(h), {})[h] = d
        leaves = [EMPTY_BUCKET] * self.num_buckets
        for b, recs in self.records.items():
            leaves[b] = bucket_digest(recs)
        self.levels = build_levels(leaves)
        self.trusted_root = self.levels[-1][0]

    # ------------------------------------------------------------------ updates
    def _verify_path(self, b: int) -> bool:
        """Check bucket b's stored leaf + siblings hash up to the trusted root. O(log B)."""
        cur, idx = self.levels[0][b], b
        for lvl in range(self.depth):
            sib = self.levels[lvl][idx ^ 1]
            cur = node_hash(cur, sib) if idx % 2 == 0 else node_hash(sib, cur)
            idx >>= 1
        return cur == self.trusted_root

    def _refresh(self, b: int) -> None:
        self.levels[0][b] = bucket_digest(self.records.get(b, {}))
        idx = b
        for lvl in range(self.depth):
            p = idx >> 1
            self.levels[lvl + 1][p] = node_hash(self.levels[lvl][2 * p],
                                                self.levels[lvl][2 * p + 1])
            idx = p
        self.trusted_root = self.levels[-1][0]

    def upsert(self, chunk_hash: str, digest: str) -> None:
        """Insert/replace one record and refresh only its leaf-to-root path.

        Purpose : keep the authenticated root current without a rebuild.
        Input   : chunk_hash, record digest.
        Output  : None; raises CorruptionDetected if the stored path is inconsistent
                  (fail-stop: heal first, otherwise the anchor would be poisoned).
        Pseudocode:
            b <- bucket_of(chunk_hash)
            if not verify_path(b): raise
            records[b][chunk_hash] <- digest
            leaf[b] <- H(sorted digests in b)
            for each ancestor of b: node <- H(left, right)
            trusted_root <- root
        Time    : O(s log s + log B), s = records in the bucket (O(1) expected)
        Space   : O(1) extra
        """
        b = self.bucket_of(chunk_hash)
        if not self._verify_path(b):
            raise CorruptionDetected(f"stored Merkle path of bucket {b} is inconsistent")
        self.records.setdefault(b, {})[chunk_hash] = digest
        self._refresh(b)

    def remove(self, chunk_hash: str) -> bool:
        b = self.bucket_of(chunk_hash)
        if not self._verify_path(b):
            raise CorruptionDetected(f"stored Merkle path of bucket {b} is inconsistent")
        recs = self.records.get(b)
        if not recs or chunk_hash not in recs:
            return False
        del recs[chunk_hash]
        if not recs:
            del self.records[b]
        self._refresh(b)
        return True

    # ------------------------------------------------------------------ repair support
    def authenticate_bucket(self, b: int) -> bool:
        """Is bucket b's record store consistent with its leaf AND is the leaf chained
        to the trusted root?  O(s + log B)."""
        return bucket_digest(self.records.get(b, {})) == self.levels[0][b] and self._verify_path(b)

    def repair_leaves(self, buckets) -> str:
        """Recompute the given bucket leaves from `records` and refresh their ancestors.
        Does NOT touch `trusted_root` -- the caller compares the result with the anchor.
        Time O(k log B) for k buckets."""
        cur = set(buckets)
        for b in cur:
            self.levels[0][b] = bucket_digest(self.records.get(b, {}))
        for lvl in range(self.depth):
            cur = {i >> 1 for i in cur}
            for p in cur:
                self.levels[lvl + 1][p] = node_hash(self.levels[lvl][2 * p], self.levels[lvl][2 * p + 1])
        return self.levels[-1][0]

    # ------------------------------------------------------------------ audit
    def derived_levels(self) -> list:
        """Recompute every internal node from the STORED leaves. O(B)."""
        return build_levels(self.levels[0])

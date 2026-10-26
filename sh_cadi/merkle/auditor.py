"""Phase 2a: Detect and Localize.

Two trees are compared:
  D = tree derived from the STORED bucket leaves     (what the tree claims)
  L = tree computed from the LIVE hash-index records (what the index holds)
and both roots are compared with the trusted anchor root.

Honest cost model
  * Detection must read every live record once (O(n) hashing) -- the same as any
    full audit, including the conventional baseline.
  * Localization is what becomes proportional to the damage: O(k log B) node
    comparisons for k damaged buckets, versus O(n) leaf comparisons for a
    full scan (Phase-1 `diagnose_changes`).
"""
import time

from sh_cadi.merkle.bucket_merkle import EMPTY_BUCKET, bucket_digest, build_levels
from sh_cadi.merkle.record_digest import record_digest


class IndexAuditor:
    def __init__(self, hash_table, tree):
        self.ht = hash_table
        self.tree = tree
        self._state = None

    # ------------------------------------------------------------ integration
    def attach(self, cas):
        """Register as the CAS index listener so uploads update the tree incrementally."""
        cas.index_listener = self
        return self

    def on_upsert(self, chunk_hash: str, entry: dict) -> None:
        self.tree.upsert(chunk_hash, record_digest(chunk_hash, entry))

    def build_baseline(self) -> str:
        self.tree.bulk_load((h, record_digest(h, e)) for h, e in self.ht.items())
        return self.tree.trusted_root

    # ------------------------------------------------------------ live state
    def _compute_live(self):
        bucket_of = self.tree.bucket_of
        live, n = {}, 0
        for h, e in self.ht.items():
            live.setdefault(bucket_of(h), {})[h] = record_digest(h, e)
            n += 1
        leaves = [EMPTY_BUCKET] * self.tree.num_buckets
        for b, recs in live.items():
            leaves[b] = bucket_digest(recs)
        return live, build_levels(leaves), n

    def live_bucket(self, b: int) -> dict:
        """Live record digests of bucket b from the last detect() (chunk_hash -> digest)."""
        return dict(self._state["live"].get(b, {}))

    # ------------------------------------------------------------ DETECT
    def detect(self) -> dict:
        """Detect corruption by root comparison.

        Purpose : decide whether the index or the Merkle tree deviates from the anchor.
        Input   : (implicit) live hash table, stored tree, trusted root.
        Output  : report with intact flag, the three roots, data_fault / tree_fault.
        Pseudocode:
            L <- tree built from live records          # O(n + B)
            D <- tree derived from stored leaves       # O(B)
            data_fault <- root(L) != trusted_root
            tree_fault <- root(D) != trusted_root
        Time    : O(n + B)      Space: O(n + B)
        """
        t0 = time.perf_counter()
        live, live_levels, n = self._compute_live()
        derived = self.tree.derived_levels()
        trusted, live_root, derived_root = self.tree.trusted_root, live_levels[-1][0], derived[-1][0]
        data_fault, tree_fault = live_root != trusted, derived_root != trusted
        self._state = {"live": live, "live_levels": live_levels, "derived": derived}
        return {
            "intact": not (data_fault or tree_fault),
            "trusted_root": trusted, "live_root": live_root, "derived_root": derived_root,
            "data_fault": data_fault, "tree_fault": tree_fault,
            "live_records": n, "buckets": self.tree.num_buckets,
            "time_s": time.perf_counter() - t0,
        }

    # ------------------------------------------------------------ LOCALIZE
    def localize(self) -> dict:
        """Locate damaged buckets by top-down descent, then damaged records inside them.

        Purpose : find *where* the damage is without scanning all leaves.
        Input   : state cached by detect().
        Output  : flagged buckets, per-record diagnosis, tree-leaf faults, cost counters.
        Pseudocode:
            descend(level, i):
                if D[level][i] == L[level][i]: return          # prune whole subtree
                if level == 0: flag bucket i; return
                descend(level-1, 2i); descend(level-1, 2i+1)
            descend(top, 0)
            for each flagged bucket b:
                live_b, stored_b <- records of b
                if H(stored_b) == stored leaf:  diff(live_b, stored_b) -> changed/missing/extra
                elif H(live_b) == H(stored_b):  stored leaf itself is corrupt (tree fault)
                else:                           ambiguous (both damaged) -> needs journal (2b)
        Time    : O(k log B + sum of flagged bucket sizes)     Space: O(k log B)
        """
        if self._state is None:
            raise RuntimeError("call detect() before localize()")
        t0 = time.perf_counter()
        D, L, live = self._state["derived"], self._state["live_levels"], self._state["live"]
        trace, flagged = [], []

        def descend(level, idx):
            same = D[level][idx] == L[level][idx]
            trace.append((level, idx, same))
            if same:
                return
            if level == 0:
                flagged.append(idx)
                return
            descend(level - 1, 2 * idx)
            descend(level - 1, 2 * idx + 1)

        descend(self.tree.depth, 0)

        changed, missing, extra = [], [], []
        tree_leaf_faults, ambiguous, examined = [], [], 0
        for b in flagged:
            live_b = live.get(b, {})
            store_b = self.tree.records.get(b, {})
            stored_leaf = self.tree.levels[0][b]
            examined += len(set(live_b) | set(store_b))
            store_consistent = bucket_digest(store_b) == stored_leaf
            live_matches_store = bucket_digest(live_b) == bucket_digest(store_b)
            if not store_consistent and live_matches_store:
                tree_leaf_faults.append(b)          # only the stored leaf is wrong
                continue
            if not store_consistent:
                ambiguous.append(b)                 # leaf AND records disagree
            for h in set(live_b) | set(store_b):
                if h not in live_b:
                    missing.append(h)
                elif h not in store_b:
                    extra.append(h)
                elif live_b[h] != store_b[h]:
                    changed.append(h)

        total = self.tree.total_records()
        return {
            "flagged_buckets": sorted(flagged),
            "faulty_records": {"changed": sorted(changed), "missing": sorted(missing),
                               "extra": sorted(extra)},
            "affected_record_hashes": sorted(set(changed) | set(missing) | set(extra)),
            "tree_leaf_faults": sorted(tree_leaf_faults),
            "ambiguous_buckets": sorted(ambiguous),
            "nodes_compared": len(trace), "trace": trace,
            "records_examined": examined, "total_records": total,
            "full_scan_comparisons": total,
            "time_s": time.perf_counter() - t0,
        }

    def audit(self) -> dict:
        det = self.detect()
        loc = None if det["intact"] else self.localize()
        return {"detection": det, "localization": loc}

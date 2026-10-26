"""The two systems compared in the evaluation, behind one small interface.

ConventionalIndex : chained hash table + the SAME sharded journal (a WAL is standard) + the Phase-1
                    sorted-leaf Merkle tree, rebuilt on demand.  Recovery = replay the whole journal
                    and rebuild.  No incremental tree, no bucket partition.
SHCADIIndex       : same hash table + same journal + incremental bucketed Merkle tree + localized heal.
Both receive identical recovery information, so differences isolate the SH-CADI mechanisms.
"""
import hashlib
from types import SimpleNamespace

from sh_cadi.hash_index.hashtable import HashTable
from sh_cadi.merkle.bucket_merkle import BucketMerkleTree, depth_for
from sh_cadi.merkle.integrity import IntegritySystem
from sh_cadi.recovery.baseline import replay_journal
from sh_cadi.recovery.healer import SelfHealingIndex
from sh_cadi.recovery.journal import MetadataJournal, entry_from_journal


def make_record(i: int, prefix: str = "rec"):
    h = hashlib.sha256(f"{prefix}{i}".encode()).hexdigest()
    return h, {"chunk_id": f"c{i}", "storage_location": f"loc/{h}", "size": 4096,
               "ref_count": 1 + i % 3, "referenced_by": []}


class ConventionalIndex:
    name = "conventional"

    def __init__(self, n_hint: int):
        self.depth = depth_for(n_hint)
        self.ht, self.journal, self.integrity = HashTable(), MetadataJournal(self.depth), None

    def bulk_insert(self, h, e):
        self.journal.append_put(h, e)
        self.ht.insert(h, e)

    insert = bulk_insert                       # normal path == build path (no tree maintenance)

    def seal(self):
        self.integrity = IntegritySystem(SimpleNamespace(hash_table=self.ht))
        return self.integrity.generate_root_hash()

    def lookup(self, h):
        return self.ht.search(h)

    def detect(self) -> bool:                  # True = corruption found
        return not self.integrity.verify_integrity()["valid"]

    def localize(self) -> set:
        d = self.integrity.diagnose_changes()
        return set(d["changed"]) | set(d["added"]) | set(d["removed"])

    def recover(self) -> int:
        fresh = replay_journal(self.journal)
        self.ht.capacity, self.ht.size, self.ht.buckets = fresh.capacity, fresh.size, fresh.buckets
        return len(self.ht)

    def verify(self) -> bool:
        return self.integrity.verify_integrity()["valid"]


class TargetedConventionalIndex(ConventionalIndex):
    """Stronger baseline: full-scan diff finds the damaged records, then ONLY those are repaired from
    the journal (no whole-index replay).  Still O(n) to detect, O(n) to localize and O(n) to verify
    (Phase-1 Merkle rebuild), because the conventional tree cannot be descended or updated in place."""
    name = "conventional_targeted"

    def localize(self) -> set:
        self._affected = super().localize()
        return self._affected

    def recover(self) -> int:
        cache, n = {}, 0
        for h in self._affected:
            b = self.journal.shard_of(h)
            if b not in cache:
                cache[b] = self.journal.read_shard(b)
            if cache[b]["latest"].get(h) is None:
                self.ht.delete(h)                      # not in journal -> spurious entry
            else:
                self.ht.insert(h, entry_from_journal(cache[b]["history"][h][0]))
                n += 1
        return n


class SHCADIIndex:
    name = "shcadi"

    def __init__(self, n_hint: int):
        self.depth = depth_for(n_hint)
        self.ht, self.tree = HashTable(), BucketMerkleTree(self.depth)
        self.journal = MetadataJournal(self.depth)
        self.shi = SelfHealingIndex(self.ht, self.tree, self.journal)

    def bulk_insert(self, h, e):               # build path: tree is built once in seal()
        self.journal.append_put(h, e)
        self.ht.insert(h, e)

    def insert(self, h, e):                    # normal operation: journal + hash table + incremental tree
        self.ht.insert(h, e)
        self.shi.on_upsert(h, e)

    def seal(self):
        return self.shi.auditor.build_baseline()

    def lookup(self, h):
        return self.ht.search(h)

    def verified_lookup(self, h):
        return self.shi.verified_get(h)

    def detect(self) -> bool:
        return not self.shi.auditor.detect()["intact"]


def build(cls, n: int):
    s = cls(n)
    for i in range(n):
        s.bulk_insert(*make_record(i))
    s.seal()
    return s

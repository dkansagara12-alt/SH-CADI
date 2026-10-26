"""Fault injection: controlled, reproducible damage with a ground-truth log."""
import hashlib
import os
import random


class FaultInjector:
    METADATA_KINDS = ("modify_location", "change_chunk_ref", "modify_ref_count",
                      "delete_entry", "insert_spurious")

    def __init__(self, hash_table, tree=None, storage=None, seed=None):
        self.ht, self.tree, self.storage = hash_table, tree, storage
        self.rng = random.Random(seed)
        self.log = []

    # ------------------------------------------------------------ internals
    def _log(self, kind, chunk_hash=None, bucket=None, **detail):
        if bucket is None and chunk_hash and self.tree:
            bucket = self.tree.bucket_of(chunk_hash)
        rec = {"kind": kind, "chunk_hash": chunk_hash, "bucket": bucket, **detail}
        self.log.append(rec)
        return rec

    def _copy(self, h):
        e = self.ht.search(h)
        if e is None:
            raise KeyError(h)
        return dict(e)

    # ------------------------------------------------------------ metadata faults
    def modify_location(self, h, new_location=None):
        e = self._copy(h)
        old, e["storage_location"] = e["storage_location"], new_location or e["storage_location"] + "_CORRUPTED"
        self.ht.update(h, e)
        return self._log("modify_location", h, old=old, new=e["storage_location"])

    def change_chunk_ref(self, h, new_chunk_id="Chunk_X"):
        e = self._copy(h)
        old, e["chunk_id"] = e["chunk_id"], new_chunk_id
        self.ht.update(h, e)
        return self._log("change_chunk_ref", h, old=old, new=new_chunk_id)

    def modify_ref_count(self, h, delta=1):
        e = self._copy(h)
        old = e["ref_count"]
        e["ref_count"] = old + delta
        self.ht.update(h, e)
        return self._log("modify_ref_count", h, old=old, new=e["ref_count"])

    def delete_entry(self, h):
        old = self._copy(h)
        self.ht.delete(h)
        return self._log("delete_entry", h, old=old)

    def insert_spurious(self, h=None):
        h = h or hashlib.sha256(f"ghost{self.rng.random()}".encode()).hexdigest()
        self.ht.insert(h, {"chunk_id": "GHOST", "storage_location": "nowhere",
                           "size": 0, "ref_count": 1, "referenced_by": []})
        return self._log("insert_spurious", h)

    # ------------------------------------------------------------ Merkle / chunk faults
    def corrupt_merkle_leaf(self, bucket):
        """Damage a stored bucket leaf (ancestors left stale, like bit rot)."""
        self.tree.levels[0][bucket] = hashlib.sha256(f"rot{bucket}{self.rng.random()}".encode()).hexdigest()
        return self._log("merkle_leaf", None, bucket=bucket)

    def delete_chunk(self, h):
        """Remove ALL stored data of a chunk.  Metadata is untouched -> NOT visible to the
        metadata Merkle check; chunk-level scrub (Phase 3a) detects it."""
        if hasattr(self.storage, "remove_chunk"):
            self.storage.remove_chunk(h)
        else:
            os.remove(self.storage._path_for(h))
        return self._log("delete_chunk", h)

    # ------------------------------------------------------------ chunk-fragment faults (protected store)
    def delete_fragment(self, h, i):
        self.storage.remove_fragment(h, i)
        return self._log("delete_fragment", h, index=i)

    def corrupt_fragment(self, h, i, silent=False):
        self.storage.corrupt_fragment(h, i, silent=silent)
        return self._log("corrupt_fragment", h, index=i, silent=silent)

    def fail_node(self, i):
        self.storage.fail_node(i)
        return self._log("fail_node", None, node=i)

    def inject_chunk_faults(self, count, per_chunk=1, kinds=("delete", "corrupt")):
        """Damage `per_chunk` fragments in each of `count` distinct chunks."""
        keys = [h for h, _ in self.ht.items()]
        out = []
        for h in self.rng.sample(keys, count):
            for i in self.rng.sample(range(self.storage.scheme.n), per_chunk):
                out.append(self.delete_fragment(h, i) if self.rng.choice(kinds) == "delete"
                           else self.corrupt_fragment(h, i))
        return out

    def affected_chunks(self):
        return {f["chunk_hash"] for f in self.log
                if f["chunk_hash"] and f["kind"] in ("delete_chunk", "delete_fragment", "corrupt_fragment")}

    # ------------------------------------------------------------ recovery-information faults
    def corrupt_journal_record(self, journal, h):
        """Damage the newest journal line of h (checksum then fails -> line is skipped)."""
        journal.tamper_last_record(h)
        return self._log("journal_record", h)

    def wipe_journal_shard(self, journal, bucket):
        journal.wipe_shard(bucket)
        return self._log("journal_shard", None, bucket=bucket)

    # ------------------------------------------------------------ multiple faults
    def inject_random(self, k, kinds=None, merkle_leaves=0):
        """Inject k metadata faults on k DISTINCT existing records (+ optional leaf faults)."""
        kinds = kinds or [x for x in self.METADATA_KINDS if x != "insert_spurious"]
        keys = [h for h, _ in self.ht.items()]
        out = []
        for h in self.rng.sample(keys, k):
            kind = self.rng.choice(kinds)
            out.append(self.insert_spurious() if kind == "insert_spurious" else getattr(self, kind)(h))
        if merkle_leaves:
            for b in self.rng.sample(range(self.tree.num_buckets), merkle_leaves):
                out.append(self.corrupt_merkle_leaf(b))
        return out

    # ------------------------------------------------------------ ground truth
    def affected_hashes(self):
        return {f["chunk_hash"] for f in self.log
                if f["chunk_hash"] and f["kind"] in self.METADATA_KINDS}

    def affected_leaf_buckets(self):
        return {f["bucket"] for f in self.log if f["kind"] == "merkle_leaf"}

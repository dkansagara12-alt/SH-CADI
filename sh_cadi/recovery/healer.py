"""Phase 2b: Recover -> Repair -> Verify, and the Detect->Localize->Recover->Repair->Verify engine.

Sources of recovery information
  1. Sharded, checksummed metadata journal (newest valid line for a record).
  2. Upload-log manifests: exact ref_count = number of times a hash appears across uploads.
Every candidate record is ACCEPTED ONLY IF its digest equals the digest the authenticated
Merkle bucket expects.  A stale or forged journal line therefore cannot be installed.
A Merkle hash alone never reconstructs data -- it only authenticates a reconstruction.
"""
import time
from collections import Counter, defaultdict

from sh_cadi.merkle.auditor import IndexAuditor
from sh_cadi.merkle.record_digest import record_digest
from sh_cadi.recovery.chunk_healer import ChunkHealer
from sh_cadi.recovery.journal import entry_from_journal


class UnrecoverableRecord(Exception):
    pass


class SelfHealingIndex:
    def __init__(self, hash_table, tree, journal, upload_log=None, chunk_store=None):
        if journal.depth != tree.depth:
            raise ValueError("journal and tree must share the same bucket depth")
        self.ht, self.tree, self.journal = hash_table, tree, journal
        self.auditor = IndexAuditor(hash_table, tree)
        self._upload_log = upload_log          # callable -> list of upload reports, or None
        self._counts = None
        self.chunks = None
        if chunk_store is not None:                       # chunk DATA protection (Phase 3a)
            self.chunks = ChunkHealer(chunk_store, lambda: [h for h, _ in self.ht.items()])
            chunk_store.degraded_listener = lambda h: self.chunks.heal([h])   # read-triggered repair

    # ------------------------------------------------------------ wiring
    def attach(self, cas):
        cas.index_listener = self
        self._upload_log = lambda: cas.upload_log
        return self

    def build_baseline(self) -> str:
        for h, e in self.ht.items():
            self.journal.append_put(h, e)
        return self.auditor.build_baseline()

    def before_write(self, h: str) -> None:
        self.verified_get(h)                   # never mutate on top of corrupted state

    def on_upsert(self, h: str, entry: dict) -> None:
        self.journal.append_put(h, entry)      # write-ahead
        self.tree.upsert(h, record_digest(h, entry))

    def _refcounts(self):
        if self._upload_log is None:
            return None
        if self._counts is None:
            self._counts = Counter(x for r in self._upload_log() for x in r["manifest"])
        return self._counts

    # ------------------------------------------------------------ record resolution
    def _resolve(self, h, expected, hist, counts):
        """Return (entry, source) for the first journal-derived candidate whose digest
        equals `expected`; (None, None) if no source can reproduce it."""
        for i, je in enumerate(hist):                      # newest -> oldest
            cand = entry_from_journal(je)
            if record_digest(h, cand) == expected:
                return cand, "journal" if i == 0 else "journal(older line)"
            if counts is not None:
                cand["ref_count"] = counts.get(h, cand["ref_count"])
                if record_digest(h, cand) == expected:
                    return cand, "journal+upload-log"
        return None, None

    def _put(self, h, entry):
        if self.ht.search(h) is None:
            self.ht.insert(h, entry)
        else:
            self.ht.update(h, entry)

    # ------------------------------------------------------------ authenticated read
    def verified_get(self, h: str):
        """Read with O(s + log B) verification; heals just this record if it is damaged.
        Returns (entry_or_None, status)."""
        b = self.tree.bucket_of(h)
        if not self.tree.authenticate_bucket(b):
            self.heal()
            return self.ht.search(h), "healed-full"
        exp, live = self.tree.records.get(b, {}).get(h), self.ht.search(h)
        if exp is None:
            if live is not None:
                self.ht.delete(h)
                return None, "removed-spurious"
            return None, "absent"
        if live is not None and record_digest(h, live) == exp:
            return live, "ok"
        hist = self.journal.read_shard(b)["history"].get(h, [])
        cand, src = self._resolve(h, exp, hist, self._refcounts())
        if cand is None:
            raise UnrecoverableRecord(h)
        self._put(h, cand)
        return cand, f"healed-record({src})"

    def heal_all(self, verify_full: bool = False) -> dict:
        """Metadata heal first (it defines which chunks must exist), then chunk-data heal."""
        meta = self.heal(verify_full)
        chunk = self.chunks.heal() if self.chunks else None
        return {"metadata": meta, "chunks": chunk,
                "success": bool(meta["success"] and (chunk is None or chunk["success"]))}

    # ------------------------------------------------------------ the engine
    def heal(self, verify_full: bool = False) -> dict:
        """Detect -> Localize -> Recover -> Repair -> Verify.

        Purpose : restore integrity, touching only the damaged buckets/records.
        Input   : (implicit) live index, Merkle tree + anchor, journal, upload log.
        Output  : report (timings, records reconstructed/removed, locality, success).
        Pseudocode:
            detect(); if intact: return
            localize() -> flagged buckets, changed/missing/extra records
            RECOVER (nothing modified yet):
              leaf-only bucket    -> nothing to fetch, leaf recomputed from record store
              ambiguous bucket    -> rebuild the whole bucket from its journal shard
              data-fault bucket   -> for each changed/missing record: pick the first
                                     journal-derived candidate whose digest == expected;
                                     for each extra record: schedule removal
            REPAIR : apply puts/removals to the hash table, recompute leaves of flagged
                     buckets and their ancestors (anchor untouched)
            VERIFY : per flagged bucket: live digests == record store, leaf chained to
                     the anchor;  stored root == anchor;  optionally full re-audit
        Time    : detect O(n+B) + O(k log B + journal lines of the k shards read)
        Space   : O(k log B + affected records)
        """
        self._counts = None
        T = {}
        det = self.auditor.detect()
        T["detect_s"] = det["time_s"]
        if det["intact"]:
            return {"healed": False, "intact": True, "success": True, "timings": T}
        loc = self.auditor.localize()
        T["localize_s"] = loc["time_s"]
        flagged, tree = loc["flagged_buckets"], self.tree

        # ------------------------------------------------ RECOVER
        t = time.perf_counter()
        counts = self._refcounts()
        per_bucket = defaultdict(lambda: defaultdict(list))
        for kind, hs in loc["faulty_records"].items():
            for h in hs:
                per_bucket[tree.bucket_of(h)][kind].append(h)
        puts, dels, overrides, unrecoverable, sources = [], [], {}, [], Counter()
        shard_cache, entries_read = {}, 0

        def shard(b):
            nonlocal entries_read
            if b not in shard_cache:
                shard_cache[b] = self.journal.read_shard(b)
                entries_read += shard_cache[b]["lines"]
            return shard_cache[b]

        for b in flagged:
            if b in loc["tree_leaf_faults"]:
                continue                                    # leaf recomputed in REPAIR
            if b in loc["ambiguous_buckets"]:               # leaf AND records untrusted
                sh, live_b, new = shard(b), self.auditor.live_bucket(b), {}
                for h, latest in sh["latest"].items():
                    if latest is None:
                        continue
                    e = entry_from_journal(sh["history"][h][0])
                    if counts is not None:
                        e["ref_count"] = counts.get(h, e["ref_count"])
                    new[h] = e
                for h, e in new.items():
                    d = record_digest(h, e)
                    if live_b.get(h) != d:
                        puts.append((h, e))
                        sources["journal-rebuild"] += 1
                dels += [h for h in live_b if h not in new]
                overrides[b] = {h: record_digest(h, e) for h, e in new.items()}
                continue
            recs = tree.records.get(b, {})
            for h in per_bucket[b]["changed"] + per_bucket[b]["missing"]:
                cand, src = self._resolve(h, recs[h], shard(b)["history"].get(h, []), counts)
                if cand is None:
                    unrecoverable.append(h)
                else:
                    puts.append((h, cand))
                    sources[src] += 1
            dels += per_bucket[b]["extra"]
        T["recover_s"] = time.perf_counter() - t

        # ------------------------------------------------ REPAIR
        t = time.perf_counter()
        for h, e in puts:
            self._put(h, e)
        for h in dels:
            self.ht.delete(h)
        for b, recs in overrides.items():
            if recs:
                tree.records[b] = recs
            else:
                tree.records.pop(b, None)
        repaired_root = tree.repair_leaves(flagged)
        T["repair_s"] = time.perf_counter() - t

        # ------------------------------------------------ VERIFY
        t = time.perf_counter()
        buckets_ok = True
        for b in flagged:
            recs = tree.records.get(b, {})
            live = {}
            for h in recs:
                e = self.ht.search(h)
                if e is not None:
                    live[h] = record_digest(h, e)
            buckets_ok &= (live == recs) and tree.authenticate_bucket(b)
        root_ok = tree.root == tree.trusted_root
        verified_local = bool(buckets_ok and root_ok and not unrecoverable)
        verified_full = self.auditor.detect()["intact"] if verify_full else None
        T["verify_s"] = time.perf_counter() - t
        T["total_s"] = sum(T.values())

        total = tree.total_records()
        return {
            "healed": True, "intact": False,
            "success": verified_local and (verified_full is not False),
            "verified_local": verified_local, "verified_full": verified_full,
            "timings": T,
            "expected_root": tree.trusted_root, "live_root_before": det["live_root"],
            "repaired_root": repaired_root, "root_restored": root_ok,
            "flagged_buckets": flagged, "buckets_repaired": len(flagged),
            "leaf_only_repairs": len(loc["tree_leaf_faults"]),
            "records_reconstructed": len(puts), "records_removed": len(dels),
            "unrecoverable": sorted(unrecoverable), "sources": dict(sources),
            "journal_shards_read": len(shard_cache), "journal_entries_read": entries_read,
            "total_records": total,
            "recovery_locality": len(puts) / total if total else 0.0,
            "bucket_locality": len(flagged) / tree.num_buckets,
            "localization": loc,
        }

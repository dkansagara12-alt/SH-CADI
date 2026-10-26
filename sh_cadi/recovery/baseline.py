"""Conventional recovery baseline: on ANY detected corruption, discard the whole hash
index and Merkle tree and rebuild both from the full journal.  It gets the same
journal (and upload-log ref_counts) as SH-CADI, so the comparison isolates locality."""
import time
from collections import Counter

from sh_cadi.hash_index.hashtable import HashTable
from sh_cadi.merkle.record_digest import record_digest
from sh_cadi.recovery.journal import entry_from_journal


def replay_journal(journal, counts=None):
    """Replay EVERY journal shard into a fresh hash table (what a conventional rebuild does)."""
    fresh = HashTable()
    for b in journal.shards():
        sh = journal.read_shard(b)
        for h, latest in sh["latest"].items():
            if latest is None:
                continue
            e = entry_from_journal(sh["history"][h][0])
            if counts is not None:
                e["ref_count"] = counts.get(h, e["ref_count"])
            fresh.insert(h, e)
    return fresh


def conventional_rebuild(hash_table, tree, journal, upload_log=None) -> dict:
    anchor = tree.trusted_root
    counts = Counter(x for r in upload_log() for x in r["manifest"]) if upload_log else None
    t0 = time.perf_counter()
    fresh, entries_read = HashTable(), 0
    shards = journal.shards()
    for b in shards:
        sh = journal.read_shard(b)
        entries_read += sh["lines"]
        for h, latest in sh["latest"].items():
            if latest is None:
                continue
            e = entry_from_journal(sh["history"][h][0])
            if counts is not None:
                e["ref_count"] = counts.get(h, e["ref_count"])
            fresh.insert(h, e)
    hash_table.capacity, hash_table.size, hash_table.buckets = fresh.capacity, fresh.size, fresh.buckets
    tree.bulk_load((h, record_digest(h, e)) for h, e in hash_table.items())
    rebuilt_root = tree.trusted_root
    tree.trusted_root = anchor                     # the anchor is never replaced by a rebuild
    n = len(hash_table)
    return {"records_reconstructed": n, "total_records": n, "recovery_locality": 1.0,
            "journal_shards_read": len(shards), "journal_entries_read": entries_read,
            "success": rebuilt_root == anchor, "time_s": time.perf_counter() - t0}

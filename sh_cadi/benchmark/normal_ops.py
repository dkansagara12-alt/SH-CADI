"""Normal-operation throughput: the whole dedup upload pipeline (chunk -> SHA-256 -> lookup ->
store/ref) with an in-memory chunk store, so only the INDEX layer differs between configurations.

  plain        : hash table only                        (lower bound, no recovery information)
  conventional : + write-ahead metadata journal
  shcadi       : + journal + incremental Merkle tree + authenticated read-before-write
"""
import os
import random
import statistics
import tempfile
import time

from sh_cadi.dedup_system import ContentAddressableStorage
from sh_cadi.merkle.bucket_merkle import BucketMerkleTree
from sh_cadi.recovery.healer import SelfHealingIndex
from sh_cadi.recovery.journal import MetadataJournal


class MemStorage:
    def __init__(self):
        self.d = {}

    def store_chunk(self, h, data):
        self.d[h] = data
        return "mem/" + h

    def read_chunk(self, h):
        return self.d[h]

    def exists(self, h):
        return h in self.d

    def chunk_count_on_disk(self):
        return len(self.d)


class JournalOnly:
    def __init__(self, j):
        self.j = j

    def on_upsert(self, h, e):
        self.j.append_put(h, e)


def make_file(path, unique=4000, total=8000, chunk=4096, seed=5):
    rng = random.Random(seed)
    blocks = [rng.randbytes(chunk) for _ in range(unique)]
    order = list(range(unique)) + [rng.randrange(unique) for _ in range(total - unique)]
    with open(path, "wb") as f:
        for i in order:
            f.write(blocks[i])
    return total, unique


def run(reps=5, unique=4000, total=8000):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "workload.bin")
    total, unique = make_file(path, unique, total)
    out = {"chunks": total, "unique": unique, "dup_ratio": 1 - unique / total, "chunk_bytes": 4096, "configs": {}}
    for name in ("plain", "conventional", "shcadi"):
        times = []
        for _ in range(reps):
            cas = ContentAddressableStorage(chunk_size=4096, storage=MemStorage())
            if name == "conventional":
                cas.index_listener = JournalOnly(MetadataJournal(12))
            elif name == "shcadi":
                SelfHealingIndex(cas.hash_table, BucketMerkleTree(12), MetadataJournal(12)).attach(cas)
            t = time.perf_counter()
            cas.upload_file(path)
            times.append(time.perf_counter() - t)
        med = statistics.median(times)
        out["configs"][name] = {"median_s": med, "min_s": min(times), "max_s": max(times),
                                "chunks_per_s": total / med, "mb_per_s": total * 4096 / 1e6 / med}
    os.remove(path)
    os.rmdir(d)
    base = out["configs"]["plain"]["median_s"]
    for c in out["configs"].values():
        c["overhead_vs_plain"] = c["median_s"] / base
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=1))

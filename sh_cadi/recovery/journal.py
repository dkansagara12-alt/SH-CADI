"""Sharded, append-only, per-record-checksummed metadata journal (the recovery information).

  * One shard per Merkle bucket (same hash-prefix partition as the tree), so
    repairing one bucket reads ONE small shard instead of replaying everything.
  * Every line carries its own checksum (first 64 bits of SHA-256): a damaged line
    is detected and skipped -- it can never silently 'heal' the index into a wrong
    state.  Final authority is always the Merkle anchor, not the journal.
  * Write-ahead: the journal line is appended BEFORE the tree/index is updated.
  * Backends: in-memory (benchmarks) or one file per shard (simulated cloud).
"""
import hashlib
import json
import os

_FIELDS = ("chunk_id", "storage_location", "size", "ref_count")


def entry_from_journal(e: dict) -> dict:
    """Rebuild a hash-index entry.  `referenced_by` is bookkeeping outside the Merkle
    digest and is not journaled: it is restored best-effort as [chunk_id]."""
    return {"chunk_id": e["chunk_id"], "storage_location": e["storage_location"],
            "size": e["size"], "ref_count": e["ref_count"], "referenced_by": [e["chunk_id"]]}


class MetadataJournal:
    def __init__(self, depth: int, directory: str = None):
        self.depth, self.directory = depth, directory
        self.mem = {}
        self.seq = 0
        if directory:
            os.makedirs(directory, exist_ok=True)

    # ------------------------------------------------------------ plumbing
    def shard_of(self, h: str) -> int:
        return int(h[:8], 16) >> (32 - self.depth)

    def _path(self, b):
        return os.path.join(self.directory, f"shard_{b}.log")

    @staticmethod
    def _checksum(body: dict) -> str:
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]

    def _lines(self, b):
        if not self.directory:
            return self.mem.get(b, [])
        p = self._path(b)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return f.read().splitlines()

    def _write_lines(self, b, lines):
        if not self.directory:
            self.mem[b] = lines
        else:
            with open(self._path(b), "w") as f:
                f.write("".join(l + "\n" for l in lines))

    def _append(self, b, line):
        if not self.directory:
            self.mem.setdefault(b, []).append(line)
        else:
            with open(self._path(b), "a") as f:
                f.write(line + "\n")

    def _write(self, h, op, e):
        self.seq += 1
        body = {"s": self.seq, "h": h, "op": op, "e": e}
        self._append(self.shard_of(h), json.dumps({**body, "c": self._checksum(body)}, separators=(",", ":")))

    def _parse(self, line):
        try:
            rec = json.loads(line)
            body = {"s": rec["s"], "h": rec["h"], "op": rec["op"], "e": rec["e"]}
            return rec if rec["c"] == self._checksum(body) else None
        except (ValueError, KeyError, TypeError):
            return None

    # ------------------------------------------------------------ API
    def append_put(self, h: str, entry: dict) -> None:
        self._write(h, "put", {k: entry[k] for k in _FIELDS})

    def append_del(self, h: str) -> None:
        self._write(h, "del", None)

    def read_shard(self, b: int) -> dict:
        """Purpose: load one shard.  Output: latest[h] (entry | None if deleted),
        history[h] (valid puts, newest first), line and invalid-line counts.
        Time O(lines in shard)."""
        lines = self._lines(b)
        latest, hist, invalid = {}, {}, 0
        for ln in lines:
            rec = self._parse(ln)
            if rec is None:
                invalid += 1
                continue
            h = rec["h"]
            if rec["op"] == "put":
                latest[h] = rec["e"]
                hist.setdefault(h, []).insert(0, rec["e"])
            else:
                latest[h], hist[h] = None, []
        return {"latest": latest, "history": hist, "lines": len(lines), "invalid": invalid}

    def shards(self):
        if not self.directory:
            return list(self.mem)
        return [int(f[6:-4]) for f in os.listdir(self.directory) if f.startswith("shard_")]

    def line_count(self) -> int:
        return sum(len(self._lines(b)) for b in self.shards())

    def size_bytes(self) -> int:
        if not self.directory:
            return sum(len(l) + 1 for ls in self.mem.values() for l in ls)
        return sum(os.path.getsize(self._path(b)) for b in self.shards())

    # ------------------------------------------------------------ fault hooks
    def tamper_last_record(self, h: str) -> bool:
        b, needle = self.shard_of(h), f'"h":"{h}"'
        lines = self._lines(b)
        for i in range(len(lines) - 1, -1, -1):
            if needle in lines[i]:
                ln = lines[i]
                lines[i] = ln[:-4] + ("0" if ln[-4] != "0" else "1") + ln[-3:]
                self._write_lines(b, lines)
                return True
        return False

    def wipe_shard(self, b: int) -> None:
        if not self.directory:
            self.mem.pop(b, None)
        elif os.path.exists(self._path(b)):
            os.remove(self._path(b))

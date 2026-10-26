"""Simulated multi-node chunk store with replication or erasure coding.

Fragment i of every chunk lives on 'node' i (directory root/node_i/<hash>.frag), so one node
failure costs each chunk at most one fragment.  Each fragment file is self-describing:
    8-byte checksum | 4-byte original chunk size | payload      (checksum covers size+payload)
The checksum only classifies a fragment as ok / corrupt / missing.  The AUTHORITY on chunk
correctness is always SHA-256(chunk) == chunk hash (the content address itself).
Drop-in for CloudStorage (store_chunk / read_chunk / exists / chunk_count_on_disk).
"""
import hashlib
import os
import struct
from collections import Counter

from sh_cadi.recovery.erasure import ReedSolomon, decode_verified


class ChunkLost(Exception):
    """Fewer than k healthy fragments remain."""


class ChunkCorrupt(Exception):
    """Healthy-looking fragments do not reproduce the expected hash."""


class ProtectedChunkStore:
    def __init__(self, root: str = "ProtectedStorage", scheme=None):
        self.root = root
        self.scheme = scheme or ReedSolomon(4, 2)
        self.degraded_listener = None          # optional: called with chunk_hash after a degraded read
        for i in range(self.scheme.n):
            os.makedirs(self.node_dir(i), exist_ok=True)

    # ------------------------------------------------------------ layout / format
    def node_dir(self, i):
        return os.path.join(self.root, f"node_{i}")

    def fragment_path(self, h, i):
        return os.path.join(self.node_dir(i), h + ".frag")

    @staticmethod
    def _pack(size, payload):
        sz = struct.pack(">I", size)
        return hashlib.sha256(sz + payload).digest()[:8] + sz + payload

    @staticmethod
    def _unpack(raw):
        if len(raw) < 12 or hashlib.sha256(raw[8:]).digest()[:8] != raw[:8]:
            return None
        return struct.unpack(">I", raw[8:12])[0], raw[12:]

    # ------------------------------------------------------------ fragment I/O
    def write_fragment(self, h, i, size, payload):
        with open(self.fragment_path(h, i), "wb") as f:
            f.write(self._pack(size, payload))

    def read_fragment(self, h, i):
        """-> (status, size, payload); status in ok | missing | corrupt."""
        p = self.fragment_path(h, i)
        if not os.path.exists(p):
            return "missing", None, None
        with open(p, "rb") as f:
            parsed = self._unpack(f.read())
        return ("corrupt", None, None) if parsed is None else ("ok", parsed[0], parsed[1])

    def fragment_states(self, h):
        return [self.read_fragment(h, i) for i in range(self.scheme.n)]

    def healthy(self, h):
        """-> (dict index->payload of ok fragments, majority chunk size or None, states)."""
        states = self.fragment_states(h)
        good = {i: s[2] for i, s in enumerate(states) if s[0] == "ok"}
        sizes = Counter(s[1] for s in states if s[0] == "ok")
        return good, (sizes.most_common(1)[0][0] if sizes else None), states

    def chunk_health(self, h):
        """Classify fragments and check the content hash from the k lowest healthy ones."""
        good, size, states = self.healthy(h)
        res = {"missing": [i for i, s in enumerate(states) if s[0] == "missing"],
               "corrupt": [i for i, s in enumerate(states) if s[0] == "corrupt"],
               "healthy": sorted(good), "content_ok": None}
        if len(good) >= self.scheme.k:
            sub = {i: good[i] for i in sorted(good)[: self.scheme.k]}
            res["content_ok"] = hashlib.sha256(self.scheme.decode(sub, size)).hexdigest() == h
        return res

    # ------------------------------------------------------------ CloudStorage interface
    def store_chunk(self, h: str, data: bytes) -> str:
        for i, frag in enumerate(self.scheme.encode(data)):
            if not os.path.exists(self.fragment_path(h, i)):
                self.write_fragment(h, i, len(data), frag)
        return os.path.join(self.root, h)          # logical location (fragments span nodes)

    def read_chunk(self, h: str) -> bytes:
        good, size, states = self.healthy(h)
        if len(good) < self.scheme.k:
            raise ChunkLost(f"{h[:12]}...: {len(good)} healthy fragments, need {self.scheme.k}")
        data, _ = decode_verified(self.scheme, good, size, h)
        if data is None:
            raise ChunkCorrupt(f"{h[:12]}...: no fragment subset reproduces the content hash")
        if len(good) < self.scheme.n and self.degraded_listener:
            self.degraded_listener(h)              # read-triggered repair
        return data

    def exists(self, h: str) -> bool:
        return sum(os.path.exists(self.fragment_path(h, i)) for i in range(self.scheme.n)) >= self.scheme.k

    def chunk_count_on_disk(self) -> int:
        names = set()
        for i in range(self.scheme.n):
            names |= {f[:-5] for f in os.listdir(self.node_dir(i)) if f.endswith(".frag")}
        return len(names)

    def bytes_on_disk(self) -> int:
        return sum(os.path.getsize(os.path.join(self.node_dir(i), f))
                   for i in range(self.scheme.n) for f in os.listdir(self.node_dir(i)))

    # ------------------------------------------------------------ fault hooks
    def remove_fragment(self, h, i):
        if os.path.exists(self.fragment_path(h, i)):
            os.remove(self.fragment_path(h, i))

    def corrupt_fragment(self, h, i, silent=False):
        """Flip payload bytes.  silent=True rewrites a VALID header around the wrong payload
        (damage that slips past the fragment checksum; only the content hash exposes it)."""
        st, size, payload = self.read_fragment(h, i)
        if st != "ok":
            return False
        bad = bytes([payload[0] ^ 0xFF]) + payload[1:]
        if silent:
            self.write_fragment(h, i, size, bad)
        else:
            with open(self.fragment_path(h, i), "wb") as f:
                f.write(self._pack(size, payload)[:12] + bad)      # header no longer matches
        return True

    def fail_node(self, i):
        for f in os.listdir(self.node_dir(i)):
            os.remove(os.path.join(self.node_dir(i), f))

    def remove_chunk(self, h):
        for i in range(self.scheme.n):
            self.remove_fragment(h, i)

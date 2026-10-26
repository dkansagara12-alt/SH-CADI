"""Redundancy schemes for actual chunk recovery: Reed-Solomon erasure coding and replication.

  * ReedSolomon(k, m): systematic MDS code over GF(2^8) (primitive poly 0x11d) with a Cauchy
    parity matrix.  A chunk becomes k data fragments + m parity fragments; ANY k of the
    n = k+m fragments reconstruct it.  Tolerates m lost/corrupt fragments per chunk.
  * Replication(r): r identical copies (k = 1).  Tolerates r-1 losses.

Existing technique: Reed-Solomon / Cauchy-RS coding and replication are standard and are NOT
claimed as SH-CADI contributions.  Speed trick: multiplying a whole fragment by a GF constant is
one C-level bytes.translate() call, and XOR uses big-int XOR, so pure Python stays usable.

Every reconstruction is accepted only if SHA-256(recovered chunk) == the expected content hash.
"""
import hashlib
import itertools

_EXP, _LOG = [0] * 512, [0] * 256


def _init_tables():
    x = 1
    for i in range(255):
        _EXP[i], _LOG[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_tables()
_TABLES = {}


def gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def gf_inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(256)")
    return _EXP[255 - _LOG[a]]


def _scale(frag: bytes, c: int) -> bytes:
    if c == 1:
        return frag
    if c == 0:
        return bytes(len(frag))
    t = _TABLES.get(c)
    if t is None:
        t = _TABLES[c] = bytes(gf_mul(c, x) for x in range(256))
    return frag.translate(t)


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _invert(M):
    """Gauss-Jordan inverse over GF(256).  O(k^3), k is tiny."""
    n = len(M)
    A = [list(r) + [1 if i == j else 0 for j in range(n)] for i, r in enumerate(M)]
    for c in range(n):
        p = next((r for r in range(c, n) if A[r][c]), None)
        if p is None:
            raise ValueError("singular matrix")
        A[c], A[p] = A[p], A[c]
        inv = gf_inv(A[c][c])
        A[c] = [gf_mul(inv, v) for v in A[c]]
        for r in range(n):
            if r != c and A[r][c]:
                f = A[r][c]
                A[r] = [a ^ gf_mul(f, b) for a, b in zip(A[r], A[c])]
    return [r[n:] for r in A]


class ReedSolomon:
    def __init__(self, k: int, m: int):
        if k < 1 or m < 1 or k + m > 256:
            raise ValueError("need k>=1, m>=1, k+m<=256")
        self.k, self.m, self.n = k, m, k + m
        self.name = f"RS({k},{m})"
        self.parity = [[gf_inv((k + i) ^ j) for j in range(k)] for i in range(m)]   # Cauchy
        self._inv = {}

    @property
    def tolerates(self) -> int:
        return self.m

    @property
    def overhead(self) -> float:
        return self.n / self.k

    def _row(self, idx):
        return [1 if j == idx else 0 for j in range(self.k)] if idx < self.k else self.parity[idx - self.k]

    def encode(self, data: bytes) -> list:
        """Purpose: chunk -> n fragments.  Time O(m*k*L) byte-ops in C, L = fragment length."""
        L = max(1, -(-len(data) // self.k))
        padded = data.ljust(self.k * L, b"\0")
        frags = [padded[i * L:(i + 1) * L] for i in range(self.k)]
        out = list(frags)
        for row in self.parity:
            acc = bytes(L)
            for c, f in zip(row, frags):
                acc = _xor(acc, _scale(f, c))
            out.append(acc)
        return out

    def decode(self, frags: dict, size: int) -> bytes:
        """Purpose: any k fragments (index -> bytes) -> original chunk of `size` bytes.
        Pseudocode: M <- rows of the generator for the chosen indices; data <- M^-1 * fragments."""
        idx = sorted(frags)[: self.k]
        if len(idx) < self.k:
            raise ValueError(f"need {self.k} fragments, got {len(frags)}")
        if idx == list(range(self.k)):
            return b"".join(frags[i] for i in idx)[:size]
        key = tuple(idx)
        inv = self._inv.get(key)
        if inv is None:
            inv = self._inv[key] = _invert([self._row(i) for i in idx])
        L = len(frags[idx[0]])
        out = []
        for j in range(self.k):
            acc = bytes(L)
            for t, i in enumerate(idx):
                acc = _xor(acc, _scale(frags[i], inv[j][t]))
            out.append(acc)
        return b"".join(out)[:size]


class Replication:
    def __init__(self, r: int):
        if r < 2:
            raise ValueError("need at least 2 copies")
        self.k, self.n, self.m = 1, r, r - 1
        self.name = f"Replication({r})"

    @property
    def tolerates(self) -> int:
        return self.m

    @property
    def overhead(self) -> float:
        return float(self.n)

    def encode(self, data: bytes) -> list:
        return [data] * self.n

    def decode(self, frags: dict, size: int) -> bytes:
        return frags[min(frags)][:size]


def decode_verified(scheme, healthy: dict, size: int, expected_hash: str, max_subsets: int = 5000):
    """Decode from `healthy` fragments and accept ONLY a result whose SHA-256 equals
    `expected_hash`.  Tries the lowest-index k fragments first (fast path), then other
    k-subsets, which also survives a fragment whose damage slipped past its checksum.
    Returns (data, used_indices) or (None, None)."""
    idx = sorted(healthy)
    if len(idx) < scheme.k:
        return None, None
    first = tuple(idx[: scheme.k])
    tried = 0
    for subset in itertools.chain([first], (s for s in itertools.combinations(idx, scheme.k) if s != first)):
        if tried >= max_subsets:
            break
        tried += 1
        data = scheme.decode({i: healthy[i] for i in subset}, size)
        if hashlib.sha256(data).hexdigest() == expected_hash:
            return data, list(subset)
    return None, None

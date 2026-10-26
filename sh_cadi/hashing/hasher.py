import hashlib


def compute_hash(data: bytes) -> str:
    """Return the SHA-256 hex digest of `data`."""
    return hashlib.sha256(data).hexdigest()

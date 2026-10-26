import hashlib


def record_digest(chunk_hash: str, entry: dict) -> str:
    """Digest of one hash-index record (the Merkle 'record leaf').

    Covers chunk_hash, chunk_id, storage_location, size and ref_count.
    `referenced_by` is deliberately excluded (unbounded, bookkeeping only).
    """
    payload = (f"{chunk_hash}|{entry['chunk_id']}|{entry['storage_location']}"
               f"|{entry['size']}|{entry['ref_count']}")
    return hashlib.sha256(payload.encode()).hexdigest()

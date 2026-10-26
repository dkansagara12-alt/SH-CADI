# Algorithm reference

Purpose / input / output / complexity for every algorithm. Pseudocode lives in the docstrings of the cited
functions. n = records, B = buckets (2^depth), s = records per bucket (O(1) expected), k = damaged buckets/records,
c = chunks, L = fragment length. **Existing** = standard technique, not an SH-CADI contribution.

| Algorithm | Status | Purpose -> Output | Time | Space | Code | Tests |
|---|---|---|---|---|---|---|
| Fixed-size chunking | existing | file -> chunks with IDs | O(F) | O(chunk) | `chunking/chunker.py` | test_phase1 |
| SHA-256 content hash | existing | chunk -> 256-bit address | O(chunk) | O(1) | `hashing/hasher.py` | test_phase1 |
| Chained hash table (insert/search/update/delete, resize at 0.75) | existing | hash -> metadata | avg O(1), worst O(n) | O(n) | `hash_index/hashtable.py` | test_phase1 |
| Dedup upload | existing | file -> unique chunks stored, refs counted | O(c) expected | O(unique) | `dedup_system.py` | test_phase1, test_phase3a |
| Phase-1 Merkle tree, proofs | existing | records -> root, inclusion proofs | build O(n), proof O(log n) | O(n) | `merkle/merkle_tree.py`, `integrity.py` | test_phase1 |
| Bucketed fixed-shape Merkle tree: `upsert`, `bulk_load`, `repair_leaves` | existing ideas (hash-prefix / sparse Merkle), our composition | records -> incrementally maintained authenticated root | upsert O(s log s + log B); bulk O(n+B); repair O(k log B) | O(n + B) | `merkle/bucket_merkle.py` | test_phase2a, test_phase2b |
| Path verification before update | existing practice | refuse writes on an inconsistent path | O(log B) | O(1) | `BucketMerkleTree._verify_path` | test_phase2a |
| **Detect** (live tree vs derived tree vs anchor) | existing idea | root comparison -> intact / data fault / tree fault | O(n + B) | O(n + B) | `merkle/auditor.py: detect` | test_phase2a |
| **Localize** (top-down descent + in-bucket diff) | descent is existing (Dynamo anti-entropy) | -> damaged buckets, changed/missing/extra records, leaf faults, ambiguous buckets | O(k log B + sum of flagged bucket sizes) | O(k log B) | `auditor.py: localize` | test_phase2a |
| Sharded checksummed journal | WAL idea is existing; sharding by Merkle bucket is our design choice | write-ahead recovery information, per-line checksum | append O(1); read shard O(lines) | O(history) | `recovery/journal.py` | test_phase2b |
| **Recover -> Repair -> Verify** (verify-before-install) | our composition | -> records restored only if digest == authenticated expectation; unrecoverable reported | detect + O(k log B + journal lines of k shards) | O(affected) | `recovery/healer.py: heal` | test_phase2b, test_phase3b |
| Authenticated read / write guard | existing practice applied to dedup path | verify bucket path, heal a single record | O(s + log B) | O(1) | `healer.py: verified_get, before_write` | test_phase2b |
| Conventional full rebuild | existing | replay whole journal, rebuild Merkle | O(n) | O(n) | `recovery/baseline.py` | test_phase2b |
| Reed-Solomon RS(k,m), GF(256), Cauchy parity | existing | chunk -> k+m fragments; any k reconstruct | encode O(m k L); decode O(k^3 + k^2 L) | O(n L) | `recovery/erasure.py` | test_phase3a |
| Replication(r) | existing | r copies | O(r L) | O(r L) | `recovery/erasure.py` | test_phase3a |
| Hash-verified k-subset decode | our composition | accept a reconstruction only if SHA-256 matches; survives checksum-bypassing damage | <= C(n,k) decodes (worst case) | O(n L) | `erasure.py: decode_verified` | test_phase3a |
| Chunk scrub / heal | existing idea | find bad fragments, rewrite only those | O(c n L) | O(damaged) | `recovery/chunk_healer.py` | test_phase3a |
| Evaluation harness | - | measured comparison of SH-CADI vs two conventional baselines | - | - | `benchmark/` | test_phase3b |

Complexity caveat: *detection* is O(n) for every system compared (all records are hashed once).
SH-CADI's measured advantage is in localization and recovery, not detection.

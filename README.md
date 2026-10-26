# SH-CADI
Self-Healing Content-Addressable Data Structure for Fault-Tolerant Cloud Storage Deduplication.

Repository: https://github.com/dkansagara12-alt/SH-CADI

Group B13: Dhruv Jayesh Kansagara (CB.AI.U4AID25114), Harish Kumaar S (CB.AI.U4AID25117),
Prithve K C (CB.AI.U4AID25122), S Sri Raghav Vatsan (CB.AI.U4AID25151).

Status: all phases complete -- deduplication, authenticated index, fault injection, detection,
localization, metadata self-healing, chunk-level recovery, and evaluation at scale.

Headline result (1,000,000 records, 100 corrupted): SH-CADI reconstructs 84 records instead of
1,000,000; recovery phase 13.7 ms vs 14.1 s for a full rebuild (1,030x) and 5.39 s for targeted
repair (393x); end to end 5.7x faster. Cost: inserts 6.7x slower, uploads 3.1x slower.
Full numbers: `benchmark_results/RESULTS.md`.

## Documents
| File | Contents |
|---|---|
| `report/SH-CADI_Report.tex` | Final project report, IEEE format (LaTeX; upload `report/SH-CADI_Report_Overleaf.zip` to Overleaf to compile) |
| `docs/ALGORITHMS.md` | Purpose, complexity, code and tests for every algorithm |
| `docs/LITERATURE.md` | Literature check and what is (and is not) claimed as new |
| `benchmark_results/RESULTS.md` | All measured results, tables and plots |

## Setup
Python 3.10+ (tested on 3.12 and 3.13, Linux and Windows):

    git clone https://github.com/dkansagara12-alt/SH-CADI.git
    cd SH-CADI
    pip install -r requirements.txt

## Run
    python -m sh_cadi.main               # full project demo (~1-2 min; scale section uses 100,000 records)
    python -m sh_cadi.main --quick       # same demo without the scale section (~5 s)
    python -m sh_cadi.main 1000000       # scale section at 1,000,000 records (several minutes, ~2.2 GB RAM)
    python -m unittest discover tests    # test suite
    python -m sh_cadi.benchmark.run_all --out benchmark_results   # full evaluation (minutes; ~2.2 GB RAM at 1M)
    python -m sh_cadi.benchmark.report benchmark_results/results.json   # regenerate RESULTS.md + plots

The demo (`sh_cadi/demo.py`) walks through the whole system in seven sections -- deduplication,
authenticated index, single-fault self-healing, mixed faults with damaged recovery data, automatic
read/write triggers, RS(4,2) chunk recovery, and SH-CADI vs conventional recovery at scale -- and ends
with a PASS/FAIL scorecard. Inputs are `sample_files/FileA.txt` and `FileB.txt`; working files go to a
temporary folder that is removed afterwards.

## Phase 2a design
- `merkle/bucket_merkle.py`  fixed-shape Merkle tree over hash-prefix buckets (O(log B) incremental
  updates, no odd-node duplication, domain-separated hashing, fail-stop writes on a corrupt path).
- `merkle/auditor.py`        `detect()` (root comparison) and `localize()` (top-down descent, then
  record-level diff inside flagged buckets).
- `fault_injection/injector.py`  modify location / chunk ref / ref_count, delete entry, spurious entry,
  corrupt Merkle leaf, delete chunk file, seeded multi-fault injection with a ground-truth log.
- Trust model: only `trusted_root` is trusted; stored leaves/nodes and per-record digests are untrusted caches.
- Cost model: detection reads all n records once; localization is O(k log B).
- Phase-1 `IntegritySystem` is kept unchanged as the conventional baseline.
- Known limits: corruption of stored *internal* nodes is not injected/handled yet; chunk-file loss is not
  visible to the metadata Merkle check (Phase 3); ambiguous buckets (leaf and records both damaged) need
  the Phase 2b journal.

## Phase 2b design
- `recovery/journal.py`  sharded (one shard per Merkle bucket), append-only, per-line checksummed
  metadata journal; write-ahead; in-memory or file-per-shard backend.
- `recovery/healer.py`   `SelfHealingIndex.heal()` = Detect -> Localize -> Recover -> Repair -> Verify.
  Candidate records are accepted ONLY if their digest equals the digest the authenticated Merkle bucket
  expects, so a stale/damaged journal line can never be installed silently.
  Recovery sources: newest valid journal line; older line + exact ref_count from the upload log.
  Ambiguous buckets (leaf and records both damaged) are rebuilt from their journal shard.
- `verified_get()` / `before_write()`  automatic triggers: reads and writes verify the bucket path
  (O(s + log B)) and heal locally before proceeding, so corruption is never written back as "truth".
- `recovery/baseline.py` conventional recovery: discard and rebuild the whole index + tree from the full journal.
- Recovery Locality = records reconstructed / total records.
- Limits: `referenced_by` is bookkeeping outside the Merkle digest and is restored best-effort as
  [chunk_id]; a record whose every journal line is lost (and cannot be re-derived) is reported as
  unrecoverable, never silently "fixed"; chunk-data recovery (erasure coding/replication) is Phase 3.

## Phase 3a design (chunk DATA recovery)
- `recovery/erasure.py`   Reed-Solomon RS(k,m) over GF(256) (systematic, Cauchy parity) and Replication(r).
  Existing techniques -- not SH-CADI contributions.  `decode_verified()` accepts a reconstruction only
  if SHA-256(chunk) == the content hash, and searches other k-subsets if a fragment's damage slipped
  past its checksum.
- `storage/protected_store.py`  multi-node simulated store (fragment i on node i), self-describing
  fragments (checksum | size | payload), drop-in for CloudStorage, degraded reads, read-triggered repair.
- `recovery/chunk_healer.py`    scrub (detect + localize fragments) -> recover -> repair (rewrite ONLY
  bad fragments) -> verify.  Metadata and chunk healing are separate layers: the Merkle root
  authenticates metadata, the content hash authenticates chunk data.
- `SelfHealingIndex.heal_all()` metadata first (it defines which chunks must exist), then chunks.
- Limits: more than m lost fragments in one chunk is unrecoverable and is reported; chunk scrub reads
  fragments (O(chunks)) -- it is not Merkle-accelerated; pure-Python coding, timings indicative only.

## Phase 3b: evaluation (see `benchmark_results/RESULTS.md`, `docs/LITERATURE.md`, `docs/ALGORITHMS.md`)
- Three systems on identical synthetic data and identical recovery information: SH-CADI; conventional full
  rebuild (the baseline the brief defines); conventional + targeted repair (a stronger baseline added so the
  comparison is not trivially one-sided).
- Each (system, size) runs in a fresh process; sizes 10k / 100k / 1M; k = 1, 10, 100 faults; corruption rates
  0.1%-25%; normal-operation throughput; memory; a journal-shard-loss scenario.
- Every number in RESULTS.md is generated from `results.json`. Read the Limitations section before citing.
- Literature check is limited (8 searches) and does NOT establish novelty; see `docs/LITERATURE.md`.

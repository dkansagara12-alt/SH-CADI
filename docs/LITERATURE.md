# SH-CADI literature check

**Scope and honesty.** This is a *limited* check: 8 web searches (ZFS, Dynamo/Cassandra anti-entropy,
deduplication reliability, dedup metadata recovery, self-healing structures, authenticated indexes, Venti,
erasure-coded repair). It was **not** a systematic survey of IEEE Xplore / ACM DL / Google Scholar / DBLP.
Absence of a match here is **not evidence of novelty**. Bibliographic details below come from search-result
pages and must be verified against the original papers before citing in a report.

## 1. What already exists (and is therefore NOT an SH-CADI contribution)

| Topic | Existing work found | Relevance to SH-CADI |
|---|---|---|
| Content-addressable storage | **Venti** (Quinlan & Dorward, FAST 2002): blocks addressed by hash of contents, write-once, duplicate blocks coalesced, inherent integrity checking. https://www.usenix.org/conference/fast-02/venti-new-approach-archival-data-storage . Its `plan9port` man page lists tools to check (and optionally fix) an index and to rebuild an index from scratch. https://plan9port.googlesource.com/plan9/+/3d5e34e146b5ba5c973230abb624ce9126024569/man/man8/venti.8 | Phase-1 design is standard CAS. Full index rebuild is the real-world conventional recovery, which justifies it as our baseline. |
| Merkle checksum tree + redundancy repair | **ZFS**: checksums stored in parent block pointers (so the pool forms a Merkle tree); on a bad block it fetches a good copy from redundancy and repairs; scrubs read the whole pool. https://docs.oracle.com/cd/E19253-01/819-5461/gaypb/index.html , https://klarasystems.com/articles/understanding-zfs-scrubs-and-data-integrity/ | Closest *conceptual* prior art: "detect with a checksum tree, repair from redundancy". SH-CADI does not invent this. ZFS operates on filesystem blocks, not on a deduplication hash index. |
| Merkle descent to localize differences | **Dynamo / Cassandra anti-entropy**: per-key-range Merkle trees; compare roots, recurse into differing children to find out-of-sync keys, minimizing data transferred. https://www.cs.cornell.edu/courses/cs6410/2017fa/slides/22-p2p-storage.pdf , https://cwiki.apache.org/confluence/x/8zoyBw . Noted drawback: key ranges change when nodes join/leave, forcing tree recomputation. | Top-down localization is a **known technique**; SH-CADI's Localize step is an application of it (between the live index and an anchored tree, not between two replicas). Our fixed hash-prefix bucket partition avoids the positional-shift problem inside one node. |
| Authenticated data structures | Merkle hash trees; Merkle B-tree / EMB-tree for outsourced databases ("Dynamic authenticated index structures for outsourced databases", https://www.cs.bu.edu/~reyzin/papers/auth-db.pdf); dynamic Merkle B-tree (Smith & Rusnak, https://arxiv.org/abs/2006.01994v3); sparse Merkle trees (cited in that paper's bibliography: Dahlberg, Pulls, Peeters 2016). | The bucketed fixed-shape tree is a hash-prefix / sparse-Merkle-style structure. Those works target *adversarial servers and query proofs*; SH-CADI uses the same machinery for *accidental-fault localization and repair authentication*. Verify-path-before-update is standard practice. |
| Dedup reliability (chunk data) | Per-File Parity (Wu et al., TPDS 2019 / SRDS 2018) and the DTR (dedup-then-RAID) and RCR (reference-count replication) families it surveys; reliability-aware dedup (chunk reliability / chunk loss severity); hybrid replication+erasure schemes (HRS). https://ranger.uta.edu/~jiang/publication/Journals/2019/2019-IEEE-TPDS(PFP%20Suzhen%20Wu).pdf , https://ece.iastate.edu/~mai/docs/papers/2021CP-Dedup.pdf | Protect *chunk data* after dedup. Phase 3a (RS / replication under a dedup index) is in this space and is **not novel**. These works do not (in the pages seen) address *recovering the dedup index/metadata itself*. |
| Dedup metadata consistency / recovery | Cluster-wide dedup with a per-chunk commit flag to keep reference counts consistent after failed transactions (https://arxiv.org/pdf/1803.07722). Patent literature: "Metadata recovery for de-duplicated data" (embeds chunk-level metadata with stored chunks so lost file metadata can be recovered; https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10606712 ) and "Data error detection and correction using hash values" (repairs a corrupted block from a duplicate; https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/9152502 ). | **Closest goal-level prior art for dedup metadata recovery.** Mechanisms differ (embedded metadata / duplicate-block repair vs. Merkle-localized journal repair), but SH-CADI must not claim to be the first to recover dedup metadata. No legal/patentability analysis is implied. |
| Self-healing file systems | Patent literature on self-healing file systems using block checksums and regenerative metadata (e.g. https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/7694191 ). | Confirms "self-healing storage metadata" is an established idea at filesystem level. |
| Erasure coding | Reed-Solomon MDS codes minimize storage overhead; conventional MDS repair reads k times the repaired data; regenerating and locally recoverable codes reduce repair bandwidth/degree (Kumar, "Codes for Distributed Storage", https://arxiv.org/pdf/2010.01344 ). | Our Phase-3a measurements (repair read/write = 4x, 6x, 10x for RS(4,2), (6,3), (10,4)) match this known theory. We implement plain RS only. |

## 2. Candidate SH-CADI contributions (to be validated, not asserted)

These are *design compositions*, not new primitives. In this limited search I did not find a published system
combining all of them for a deduplication index, but that is **unverified**.

1. **Co-partitioned detection and recovery.** The Merkle bucket partition (hash-prefix) is also the journal shard
   partition, so a localized detection result maps to a single small recovery-information read per bucket.
2. **Verify-before-install repair.** A reconstructed record is installed only if its digest equals the digest
   expected by the authenticated bucket; multiple recovery sources are tried (newest journal line, older line plus
   exact `ref_count` from the upload log). Stale or damaged recovery data is rejected and reported as unrecoverable.
3. **Write/read-path guards.** Verify-then-heal before touching a record, so corrupted values are never written
   back into the authenticated state (a standard Merkle-path check applied to the dedup write path).
4. **Evaluation framing.** *Recovery locality* (records reconstructed / total records) as the headline metric.

## 3. What the evaluation must therefore show (and not claim)

* Claim only **measured, scoped** advantages (recovery-phase cost and locality at the tested scales and fault rates),
  alongside measured **costs** (normal-operation overhead, memory, journal storage).
* Do **not** claim: first self-healing storage index; first Merkle localization; first dedup metadata recovery;
  novelty of erasure coding or replication; any security guarantee against adversaries (the threat model is
  accidental faults with a trusted anchor root).

## 4. To make the novelty claim defensible (future work)

Run a systematic search (Google Scholar, DBLP, ACM DL, IEEE Xplore, USENIX FAST/OSDI/ATC, VLDB/SIGMOD) for:
"deduplication index" + (corruption | recovery | repair | self-healing | scrub | fsck); "fingerprint index" +
(recovery | rebuild); "Merkle" + "deduplication" + (integrity | repair); "metadata" + "deduplication" + (reliability |
resilient | fault-tolerant); "ZFS dedup table" corruption/recovery; backup-system index rebuild (e.g. Data Domain,
Venti-based systems). Record each hit's mechanism and why SH-CADI differs.

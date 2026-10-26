"""Chunk-level self-healing: Detect -> Localize -> Recover -> Repair -> Verify for chunk DATA.

Kept separate from metadata healing (Phase 2): metadata is repaired from the journal and
authenticated by the Merkle anchor; chunk data is repaired from surviving fragments and
authenticated by its own SHA-256 content address.  A Merkle hash never rebuilds a chunk.
"""
import time


class ChunkHealer:
    def __init__(self, store, hashes):
        self.store = store
        self._hashes = hashes                  # callable -> iterable of chunk hashes to protect

    # ------------------------------------------------------------ DETECT + LOCALIZE
    def scrub(self, hashes=None) -> dict:
        """Purpose : find damaged chunks and exactly which fragments are missing/corrupt.
        Input     : optional subset of chunk hashes (e.g. one Merkle bucket) else all.
        Output    : damaged {hash: health}, lost (fewer than k healthy fragments), counters.
        Pseudocode: for h in hashes: classify each fragment (ok/missing/corrupt);
                    verify SHA-256(decode(k healthy fragments)) == h; record if any check fails.
        Time      : O(#chunks * n * fragment size)      Space: O(#damaged)"""
        t0 = time.perf_counter()
        k, scanned, damaged, lost = self.store.scheme.k, 0, {}, []
        for h in (self._hashes() if hashes is None else hashes):
            hl = self.store.chunk_health(h)
            scanned += 1
            if hl["missing"] or hl["corrupt"] or hl["content_ok"] is False:
                damaged[h] = hl
                if len(hl["healthy"]) < k:
                    lost.append(h)
        return {"chunks_scanned": scanned, "damaged": damaged, "lost": sorted(lost),
                "intact": not damaged, "time_s": time.perf_counter() - t0}

    # ------------------------------------------------------------ RECOVER + REPAIR + VERIFY
    def heal(self, hashes=None, scrub_report=None) -> dict:
        """Purpose : rebuild damaged fragments of damaged chunks only.
        Pseudocode: for each damaged chunk h:
                        healthy <- readable fragments
                        data <- first k-subset whose decode hashes to h   (else: unrecoverable)
                        expected <- encode(data)
                        rewrite ONLY fragments that are missing/corrupt/differ from expected
                    verify: every fragment ok and SHA-256(chunk) == h
        Time      : O(damaged * (k-subset decode + n fragment writes))"""
        rep = scrub_report if scrub_report is not None else self.scrub(hashes)
        T = {"scrub_s": rep["time_s"]}
        scheme, store = self.store.scheme, self.store
        if rep["intact"]:
            return {"healed": False, "success": True, "chunks_scanned": rep["chunks_scanned"],
                    "chunks_repaired": 0, "fragments_rewritten": 0, "unrecoverable": [], "timings": T}

        t = time.perf_counter()
        plan, unrecoverable, bytes_read = [], [], 0
        from sh_cadi.recovery.erasure import decode_verified
        for h in rep["damaged"]:
            good, size, states = store.healthy(h)
            data, used = decode_verified(scheme, good, size, h) if size is not None else (None, None)
            if data is None:
                unrecoverable.append(h)
                continue
            bytes_read += sum(len(good[i]) for i in used)
            plan.append((h, data, size, states))
        T["recover_s"] = time.perf_counter() - t

        t = time.perf_counter()
        rewritten, bytes_written = {}, 0
        for h, data, size, states in plan:
            expected = scheme.encode(data)
            for i in range(scheme.n):
                if states[i][0] != "ok" or states[i][2] != expected[i]:
                    store.write_fragment(h, i, size, expected[i])
                    rewritten.setdefault(h, []).append(i)
                    bytes_written += len(expected[i])
        T["repair_s"] = time.perf_counter() - t

        t = time.perf_counter()
        verified = True
        for h, *_ in plan:
            hl = store.chunk_health(h)
            verified &= not hl["missing"] and not hl["corrupt"] and hl["content_ok"] is True
        T["verify_s"] = time.perf_counter() - t

        n_frags = rep["chunks_scanned"] * scheme.n
        n_rew = sum(len(v) for v in rewritten.values())
        return {
            "healed": True, "success": bool(verified and not unrecoverable),
            "verified": verified, "timings": T,
            "chunks_scanned": rep["chunks_scanned"], "chunks_damaged": len(rep["damaged"]),
            "chunks_repaired": len(plan), "fragments_rewritten": n_rew, "rewritten": rewritten,
            "chunk_locality": len(plan) / rep["chunks_scanned"],
            "fragment_locality": n_rew / n_frags if n_frags else 0.0,
            "repair_bytes_read": bytes_read, "repair_bytes_written": bytes_written,
            "unrecoverable": sorted(unrecoverable),
        }

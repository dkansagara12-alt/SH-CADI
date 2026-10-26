import hashlib, itertools, os, random, shutil, tempfile, unittest
from sh_cadi.dedup_system import ContentAddressableStorage
from sh_cadi.fault_injection.injector import FaultInjector
from sh_cadi.merkle.bucket_merkle import BucketMerkleTree
from sh_cadi.recovery.chunk_healer import ChunkHealer
from sh_cadi.recovery.erasure import ReedSolomon, Replication, decode_verified, gf_mul, gf_inv
from sh_cadi.recovery.healer import SelfHealingIndex
from sh_cadi.recovery.journal import MetadataJournal
from sh_cadi.storage.protected_store import ProtectedChunkStore, ChunkLost


def H(b): return hashlib.sha256(b).hexdigest()


class TestGaloisAndCodes(unittest.TestCase):
    def test_field(self):
        for a in range(1, 256):
            self.assertEqual(gf_mul(a, gf_inv(a)), 1)

    def test_rs_every_k_subset_decodes(self):
        rng = random.Random(0)
        for k, m in ((4, 2), (3, 3), (6, 3), (1, 2), (10, 4)):
            rs = ReedSolomon(k, m)
            for size in (1, 5, k, 4096, 1000 + k - 1):
                data = bytes(rng.randrange(256) for _ in range(size))
                frags = rs.encode(data)
                self.assertEqual(len(frags), k + m)
                subsets = list(itertools.combinations(range(k + m), k))
                for sub in rng.sample(subsets, min(len(subsets), 40)):
                    self.assertEqual(rs.decode({i: frags[i] for i in sub}, size), data, (k, m, size, sub))

    def test_replication(self):
        r = Replication(3)
        f = r.encode(b"hello")
        self.assertEqual(r.decode({2: f[2]}, 5), b"hello")

    def test_too_few_fragments(self):
        rs = ReedSolomon(4, 2)
        with self.assertRaises(ValueError):
            rs.decode({0: b"a", 1: b"b"}, 2)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.data = os.urandom(1000)
        self.h = H(self.data)

    def tearDown(self): shutil.rmtree(self.d)

    def test_read_with_up_to_m_losses(self):
        for scheme in (ReedSolomon(4, 2), Replication(3)):
            st = ProtectedChunkStore(os.path.join(self.d, scheme.name), scheme)
            st.store_chunk(self.h, self.data)
            self.assertEqual(st.read_chunk(self.h), self.data)
            for i in random.Random(1).sample(range(scheme.n), scheme.m):
                st.remove_fragment(self.h, i)
            self.assertEqual(st.read_chunk(self.h), self.data)            # degraded read works
            st.remove_fragment(self.h, [j for j in range(scheme.n) if st.read_fragment(self.h, j)[0] == "ok"][0])
            with self.assertRaises(ChunkLost):
                st.read_chunk(self.h)

    def test_checksum_detects_corruption(self):
        st = ProtectedChunkStore(self.d, ReedSolomon(4, 2))
        st.store_chunk(self.h, self.data)
        st.corrupt_fragment(self.h, 1)
        self.assertEqual(st.read_fragment(self.h, 1)[0], "corrupt")
        self.assertEqual(st.read_chunk(self.h), self.data)

    def test_overhead(self):
        chunks = [os.urandom(4096) for _ in range(20)]
        for scheme, want in ((Replication(3), 3.0), (ReedSolomon(4, 2), 1.5), (ReedSolomon(10, 4), 1.4)):
            st = ProtectedChunkStore(os.path.join(self.d, scheme.name), scheme)
            for c in chunks: st.store_chunk(H(c), c)
            ratio = st.bytes_on_disk() / sum(map(len, chunks))
            self.assertAlmostEqual(ratio, want, delta=0.05, msg=scheme.name)   # padding + 12-byte headers


class TestChunkHealer(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = ProtectedChunkStore(self.d, ReedSolomon(4, 2))
        self.datas = [os.urandom(600 + i) for i in range(30)]
        self.hs = [H(x) for x in self.datas]
        for h, x in zip(self.hs, self.datas): self.st.store_chunk(h, x)
        self.ch = ChunkHealer(self.st, lambda: self.hs)

    def tearDown(self): shutil.rmtree(self.d)

    def snap(self):
        return {(h, i): self.st.read_fragment(h, i)[2] for h in self.hs for i in range(6)}

    def test_intact_noop(self):
        self.assertTrue(self.ch.scrub()["intact"])
        self.assertFalse(self.ch.heal()["healed"])

    def test_heal_restores_bytes_exactly_and_locally(self):
        before = self.snap()
        self.st.remove_fragment(self.hs[3], 0); self.st.remove_fragment(self.hs[3], 5)
        self.st.corrupt_fragment(self.hs[9], 2)
        rep = self.ch.scrub()
        self.assertEqual(sorted(rep["damaged"]), sorted([self.hs[3], self.hs[9]]))
        self.assertEqual(rep["damaged"][self.hs[3]]["missing"], [0, 5])
        self.assertEqual(rep["damaged"][self.hs[9]]["corrupt"], [2])
        out = self.ch.heal(scrub_report=rep)
        self.assertTrue(out["success"], out)
        self.assertEqual((out["chunks_repaired"], out["fragments_rewritten"]), (2, 3))
        self.assertAlmostEqual(out["chunk_locality"], 2 / 30)
        self.assertEqual(self.snap(), before)

    def test_node_failure(self):
        before = self.snap()
        self.st.fail_node(2)
        out = self.ch.heal()
        self.assertTrue(out["success"])
        self.assertEqual(out["chunks_repaired"], 30)
        self.assertEqual(self.snap(), before)

    def test_beyond_tolerance_reported_not_hidden(self):
        for i in (0, 1, 2): self.st.remove_fragment(self.hs[4], i)
        rep = self.ch.scrub()
        self.assertEqual(rep["lost"], [self.hs[4]])
        out = self.ch.heal(scrub_report=rep)
        self.assertFalse(out["success"])
        self.assertEqual(out["unrecoverable"], [self.hs[4]])

    def test_silent_corruption_found_by_content_hash(self):
        before = self.snap()
        self.st.corrupt_fragment(self.hs[7], 1, silent=True)        # checksum still valid
        self.assertEqual(self.st.read_fragment(self.hs[7], 1)[0], "ok")
        rep = self.ch.scrub()
        self.assertEqual(list(rep["damaged"]), [self.hs[7]])
        self.assertIs(rep["damaged"][self.hs[7]]["content_ok"], False)
        out = self.ch.heal(scrub_report=rep)
        self.assertTrue(out["success"], out)
        self.assertEqual(out["rewritten"], {self.hs[7]: [1]})
        self.assertEqual(self.snap(), before)

    def test_decode_verified_rejects_wrong_hash(self):
        good, size, _ = self.st.healthy(self.hs[0])
        self.assertEqual(decode_verified(self.st.scheme, good, size, "0" * 64), (None, None))

    def test_replication_heal(self):
        d2 = tempfile.mkdtemp()
        try:
            st = ProtectedChunkStore(d2, Replication(3))
            for h, x in zip(self.hs, self.datas): st.store_chunk(h, x)
            st.remove_fragment(self.hs[0], 0); st.corrupt_fragment(self.hs[0], 2)
            ch = ChunkHealer(st, lambda: self.hs)
            out = ch.heal()
            self.assertTrue(out["success"])
            self.assertEqual(out["fragments_rewritten"], 2)
            self.assertEqual(out["repair_bytes_read"], len(self.datas[0]))    # 1 copy read
        finally:
            shutil.rmtree(d2)


class TestIntegrated(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f1, self.f2 = os.path.join(self.d, "a.bin"), os.path.join(self.d, "b.bin")
        blk = [os.urandom(256) for _ in range(12)]
        with open(self.f1, "wb") as f: f.write(b"".join(blk))
        with open(self.f2, "wb") as f: f.write(b"".join(blk[:6]) + os.urandom(256 * 4))
        self.store = ProtectedChunkStore(os.path.join(self.d, "ps"), ReedSolomon(4, 2))
        self.cas = ContentAddressableStorage(chunk_size=256, storage=self.store)
        self.shi = SelfHealingIndex(self.cas.hash_table, BucketMerkleTree(5), MetadataJournal(5),
                                    chunk_store=self.store).attach(self.cas)
        self.inj = FaultInjector(self.cas.hash_table, self.shi.tree, self.store, seed=3)
        for p in (self.f1, self.f2, self.f1): self.cas.upload_file(p)

    def tearDown(self): shutil.rmtree(self.d)

    def same(self, p):
        out = os.path.join(self.d, "out")
        self.cas.reconstruct_file(p, out)
        with open(out, "rb") as a, open(p, "rb") as b: return a.read() == b.read()

    def test_dedup_over_protected_store(self):
        st = self.cas.global_stats()
        self.assertGreater(st["total_saved_bytes"], 0)
        self.assertEqual(self.store.chunk_count_on_disk(), st["unique_chunks_stored"])
        self.assertTrue(self.same(self.f1) and self.same(self.f2))

    def test_metadata_and_chunk_faults_together(self):
        self.inj.inject_random(3, merkle_leaves=1)
        self.inj.inject_chunk_faults(4, per_chunk=1)      # node failure + 1 more per chunk <= m=2
        self.inj.fail_node(5)
        self.assertTrue(self.same(self.f1))                     # degraded read is already correct
        rep = self.shi.heal_all(verify_full=True)
        self.assertTrue(rep["success"], rep)
        self.assertTrue(rep["metadata"]["healed"] and rep["chunks"]["healed"])
        self.assertTrue(self.shi.auditor.detect()["intact"])
        self.assertTrue(self.shi.chunks.scrub()["intact"])
        self.assertTrue(self.same(self.f1) and self.same(self.f2))

    def test_layers_are_independent(self):
        h = list(self.cas.hash_table.items())[0][0]
        self.inj.delete_fragment(h, 0)
        self.assertTrue(self.shi.auditor.detect()["intact"])    # metadata Merkle unaffected
        self.assertFalse(self.shi.chunks.scrub()["intact"])
        self.assertFalse(self.shi.heal()["healed"])             # metadata engine is a no-op
        self.assertTrue(self.shi.heal_all()["chunks"]["success"])

    def test_read_triggers_repair(self):
        h = self.cas.upload_log[0]["manifest"][0]
        self.inj.delete_fragment(h, 1); self.inj.corrupt_fragment(h, 4)
        self.assertTrue(self.same(self.f1))                     # read succeeds AND heals that chunk
        self.assertEqual(self.store.chunk_health(h)["healthy"], list(range(6)))

    def test_unrecoverable_chunk_reported(self):
        h = list(self.cas.hash_table.items())[2][0]
        for i in (0, 1, 2): self.inj.delete_fragment(h, i)
        rep = self.shi.heal_all()
        self.assertFalse(rep["success"])
        self.assertEqual(rep["chunks"]["unrecoverable"], [h])

    def test_all_intact_noop(self):
        rep = self.shi.heal_all()
        self.assertTrue(rep["success"])
        self.assertFalse(rep["metadata"]["healed"])
        self.assertFalse(rep["chunks"]["healed"])


if __name__ == "__main__":
    unittest.main()

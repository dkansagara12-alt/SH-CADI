import hashlib, os, shutil, tempfile, unittest
from sh_cadi.dedup_system import ContentAddressableStorage
from sh_cadi.hash_index.hashtable import HashTable
from sh_cadi.merkle.bucket_merkle import BucketMerkleTree
from sh_cadi.merkle.record_digest import record_digest
from sh_cadi.fault_injection.injector import FaultInjector
from sh_cadi.recovery.journal import MetadataJournal
from sh_cadi.recovery.healer import SelfHealingIndex
from sh_cadi.recovery.baseline import conventional_rebuild


def make(n=500, depth=6, seed=1, directory=None):
    ht, tree, jr = HashTable(), BucketMerkleTree(depth), MetadataJournal(depth, directory)
    for i in range(n):
        h = hashlib.sha256(str(i).encode()).hexdigest()
        ht.insert(h, {"chunk_id": f"c{i}", "storage_location": f"loc/{h}", "size": 64,
                      "ref_count": 1 + i % 3, "referenced_by": []})
    shi = SelfHealingIndex(ht, tree, jr)
    shi.build_baseline()
    return ht, tree, jr, shi, FaultInjector(ht, tree, seed=seed)


def snap(ht):
    return {h: record_digest(h, e) for h, e in ht.items()}


class TestJournal(unittest.TestCase):
    def _roundtrip(self, jr):
        h = hashlib.sha256(b"x").hexdigest()
        e = {"chunk_id": "c", "storage_location": "l", "size": 1, "ref_count": 1}
        jr.append_put(h, e)
        jr.append_put(h, {**e, "ref_count": 2})
        sh = jr.read_shard(jr.shard_of(h))
        self.assertEqual(sh["latest"][h]["ref_count"], 2)
        self.assertEqual([x["ref_count"] for x in sh["history"][h]], [2, 1])
        self.assertTrue(jr.tamper_last_record(h))
        sh = jr.read_shard(jr.shard_of(h))
        self.assertEqual(sh["invalid"], 1)
        self.assertEqual(sh["latest"][h]["ref_count"], 1)      # stale, but flagged via invalid count
        jr.append_del(h)
        self.assertIsNone(jr.read_shard(jr.shard_of(h))["latest"][h])
        jr.wipe_shard(jr.shard_of(h))
        self.assertEqual(jr.read_shard(jr.shard_of(h))["lines"], 0)

    def test_memory(self):
        self._roundtrip(MetadataJournal(6))

    def test_file_backend(self):
        d = tempfile.mkdtemp()
        try:
            self._roundtrip(MetadataJournal(6, d))
        finally:
            shutil.rmtree(d)


class TestHeal(unittest.TestCase):
    def _heal_single(self, fn, expect_removed=0):
        ht, tree, jr, shi, inj = make()
        before = snap(ht)
        h = list(ht.items())[77][0]
        fn(inj, h)
        rep = shi.heal(verify_full=True)
        self.assertTrue(rep["success"], rep)
        self.assertTrue(rep["verified_full"])
        self.assertEqual(snap(ht), before)                      # exact restoration
        self.assertEqual(rep["records_removed"], expect_removed)
        self.assertEqual(rep["expected_root"], rep["repaired_root"])
        self.assertLessEqual(rep["records_reconstructed"], 1)
        return rep

    def test_modify_location(self):
        r = self._heal_single(lambda i, h: i.modify_location(h))
        self.assertEqual(r["records_reconstructed"], 1)
        self.assertAlmostEqual(r["recovery_locality"], 1 / 500)

    def test_chunk_ref(self):
        self._heal_single(lambda i, h: i.change_chunk_ref(h, "Chunk_X"))

    def test_ref_count(self):
        self._heal_single(lambda i, h: i.modify_ref_count(h, 9))

    def test_delete_entry(self):
        self._heal_single(lambda i, h: i.delete_entry(h))

    def test_spurious_removed(self):
        ht, tree, jr, shi, inj = make()
        before = snap(ht)
        inj.insert_spurious()
        rep = shi.heal(verify_full=True)
        self.assertTrue(rep["success"])
        self.assertEqual((rep["records_reconstructed"], rep["records_removed"]), (0, 1))
        self.assertEqual(snap(ht), before)

    def test_intact_is_noop(self):
        *_, shi, _ = make()
        self.assertFalse(shi.heal()["healed"])

    def test_merkle_leaf_only(self):
        ht, tree, jr, shi, inj = make()
        inj.corrupt_merkle_leaf(11)
        rep = shi.heal(verify_full=True)
        self.assertTrue(rep["success"])
        self.assertEqual((rep["records_reconstructed"], rep["leaf_only_repairs"]), (0, 1))

    def test_multi_fault_exact_restore(self):
        for seed in range(6):
            ht, tree, jr, shi, inj = make(n=3000, depth=9, seed=seed)
            before = snap(ht)
            inj.inject_random(40, kinds=list(inj.METADATA_KINDS), merkle_leaves=3)
            rep = shi.heal(verify_full=True)
            self.assertTrue(rep["success"], rep)
            self.assertEqual(snap(ht), before)
            self.assertLess(rep["recovery_locality"], 0.02)

    def test_ambiguous_bucket_rebuilt_from_journal(self):
        ht, tree, jr, shi, inj = make()
        before = snap(ht)
        h = list(ht.items())[5][0]
        inj.corrupt_merkle_leaf(tree.bucket_of(h))
        inj.modify_location(h)
        rep = shi.heal(verify_full=True)
        self.assertEqual(rep["localization"]["ambiguous_buckets"], [tree.bucket_of(h)])
        self.assertTrue(rep["success"], rep)
        self.assertEqual(snap(ht), before)

    def test_unrecoverable_reported_not_hidden(self):
        ht, tree, jr, shi, inj = make()
        h = list(ht.items())[9][0]
        inj.delete_entry(h)
        inj.wipe_journal_shard(jr, tree.bucket_of(h))
        rep = shi.heal(verify_full=True)
        self.assertFalse(rep["success"])
        self.assertEqual(rep["unrecoverable"], [h])
        self.assertFalse(rep["verified_full"])

    def test_stale_journal_line_rejected(self):
        """Newest journal line damaged -> older (stale) line must NOT be installed blindly."""
        ht, tree, jr, shi, inj = make()
        h = list(ht.items())[3][0]
        e = dict(ht.search(h)); e["ref_count"] += 5
        ht.update(h, e); shi.on_upsert(h, e)                    # legit update -> journal line #2
        want = record_digest(h, e)
        inj.corrupt_journal_record(jr, h)                       # newest line now invalid
        inj.modify_ref_count(h, 1)
        rep = shi.heal()
        self.assertFalse(rep["success"])                        # stale line's digest != expected
        self.assertEqual(rep["unrecoverable"], [h])
        self.assertNotEqual(record_digest(h, ht.search(h)), want)


class TestBaselineComparison(unittest.TestCase):
    def test_localized_vs_full_rebuild(self):
        ht, tree, jr, shi, inj = make(n=4000, depth=10, seed=4)
        before = snap(ht)
        inj.inject_random(10)
        rep = shi.heal(verify_full=True)
        self.assertTrue(rep["success"])
        inj2 = FaultInjector(ht, tree, seed=4)
        inj2.inject_random(10)
        base = conventional_rebuild(ht, tree, jr)
        self.assertTrue(base["success"])
        self.assertEqual(snap(ht), before)
        self.assertEqual(base["records_reconstructed"], 4000)
        self.assertLess(rep["records_reconstructed"], 20)
        self.assertLess(rep["journal_entries_read"], base["journal_entries_read"])


class TestWithCAS(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "a.bin")
        with open(self.f, "wb") as fh:
            fh.write(b"".join(bytes([i]) * 64 for i in range(24)) + b"Z" * 64 * 4)
        self.cas = ContentAddressableStorage(chunk_size=64, storage_root=os.path.join(self.d, "cs"))
        self.shi = SelfHealingIndex(self.cas.hash_table, BucketMerkleTree(5), MetadataJournal(5)).attach(self.cas)
        self.inj = FaultInjector(self.cas.hash_table, self.shi.tree, self.cas.storage, seed=2)

    def tearDown(self):
        shutil.rmtree(self.d)

    def _truth(self):
        from collections import Counter
        c = Counter(h for r in self.cas.upload_log for h in r["manifest"])
        return all(e["ref_count"] == c[h] for h, e in self.cas.hash_table.items())

    def test_uploads_then_heal_ref_count_via_upload_log(self):
        for _ in range(3):
            self.cas.upload_file(self.f)
        h = list(self.cas.hash_table.items())[4][0]
        self.inj.modify_ref_count(h, 7)
        self.inj.corrupt_journal_record(self.shi.journal, h)    # latest line unusable
        rep = self.shi.heal(verify_full=True)
        self.assertTrue(rep["success"], rep)
        self.assertEqual(rep["sources"], {"journal+upload-log": 1})
        self.assertTrue(self._truth())

    def test_auto_heal_on_write_does_not_launder_corruption(self):
        self.cas.upload_file(self.f)
        h = list(self.cas.hash_table.items())[2][0]
        self.inj.modify_ref_count(h, 50)
        self.inj.corrupt_merkle_leaf(self.shi.tree.bucket_of(h))
        self.cas.upload_file(self.f)                             # write path triggers healing
        self.assertTrue(self.shi.auditor.detect()["intact"])
        self.assertTrue(self._truth())

    def test_verified_get_heals_single_record(self):
        self.cas.upload_file(self.f)
        hs = [h for h, _ in self.cas.hash_table.items()]
        self.inj.modify_location(hs[0]); self.inj.modify_location(hs[1])
        e, status = self.shi.verified_get(hs[0])
        self.assertTrue(status.startswith("healed-record"))
        self.assertFalse(self.shi.auditor.detect()["intact"])    # hs[1] still damaged: heal was local
        self.assertEqual(self.shi.auditor.localize()["affected_record_hashes"], [hs[1]])

    def test_system_continues_after_heal(self):
        self.cas.upload_file(self.f)
        self.inj.inject_random(4, merkle_leaves=1)
        self.assertTrue(self.shi.heal(verify_full=True)["success"])
        self.cas.upload_file(self.f)
        self.assertTrue(self.shi.auditor.detect()["intact"])
        out = os.path.join(self.d, "o.bin")
        self.cas.reconstruct_file(self.f, out)
        with open(out, "rb") as a, open(self.f, "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()

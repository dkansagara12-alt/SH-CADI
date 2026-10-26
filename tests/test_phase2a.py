import hashlib, os, shutil, tempfile, unittest
from sh_cadi.dedup_system import ContentAddressableStorage
from sh_cadi.hash_index.hashtable import HashTable
from sh_cadi.merkle.bucket_merkle import BucketMerkleTree, CorruptionDetected, build_levels, depth_for
from sh_cadi.merkle.auditor import IndexAuditor
from sh_cadi.merkle.record_digest import record_digest
from sh_cadi.fault_injection.injector import FaultInjector


def make_index(n=500, depth=6, seed=1):
    ht, tree = HashTable(), BucketMerkleTree(depth)
    for i in range(n):
        h = hashlib.sha256(str(i).encode()).hexdigest()
        ht.insert(h, {"chunk_id": f"c{i}", "storage_location": f"loc/{h}", "size": 64,
                      "ref_count": 1, "referenced_by": []})
    aud = IndexAuditor(ht, tree)
    aud.build_baseline()
    return ht, tree, aud, FaultInjector(ht, tree, seed=seed)


class TestBucketTree(unittest.TestCase):
    def test_incremental_equals_rebuild(self):
        ht, tree, aud, _ = make_index(200)
        h = hashlib.sha256(b"new").hexdigest()
        e = {"chunk_id": "n", "storage_location": "x", "size": 1, "ref_count": 1, "referenced_by": []}
        tree.upsert(h, record_digest(h, e))
        ref = BucketMerkleTree(6)
        ref.bulk_load((k, d) for b in tree.records.values() for k, d in b.items())
        self.assertEqual(tree.trusted_root, ref.trusted_root)
        self.assertEqual(tree.levels, ref.levels)

    def test_remove_restores_previous_root(self):
        ht, tree, aud, _ = make_index(50)
        before = tree.trusted_root
        h = hashlib.sha256(b"tmp").hexdigest()
        tree.upsert(h, "d")
        self.assertNotEqual(before, tree.trusted_root)
        self.assertTrue(tree.remove(h))
        self.assertEqual(before, tree.trusted_root)

    def test_depth_for(self):
        self.assertEqual(depth_for(100_000), 15)
        self.assertGreaterEqual(depth_for(1), 1)

    def test_write_refused_on_corrupt_path(self):
        ht, tree, aud, inj = make_index(100)
        h = next(iter(ht.items()))[0]
        inj.corrupt_merkle_leaf(tree.bucket_of(h))
        with self.assertRaises(CorruptionDetected):
            tree.upsert(h, "whatever")


class TestDetectLocalize(unittest.TestCase):
    def test_intact(self):
        _, _, aud, _ = make_index()
        r = aud.audit()
        self.assertTrue(r["detection"]["intact"])
        self.assertIsNone(r["localization"])

    def _single(self, fn):
        ht, tree, aud, inj = make_index()
        h = list(ht.items())[123][0]
        fn(inj, h)
        r = aud.audit()
        self.assertFalse(r["detection"]["intact"])
        loc = r["localization"]
        self.assertEqual(loc["affected_record_hashes"], [h])
        self.assertEqual(loc["flagged_buckets"], [tree.bucket_of(h)])
        self.assertLess(loc["nodes_compared"], loc["full_scan_comparisons"])
        return loc

    def test_modify_location(self):
        loc = self._single(lambda i, h: i.modify_location(h))
        self.assertEqual(len(loc["faulty_records"]["changed"]), 1)

    def test_change_chunk_ref(self):
        self._single(lambda i, h: i.change_chunk_ref(h, "Chunk_X"))

    def test_ref_count(self):
        self._single(lambda i, h: i.modify_ref_count(h, 5))

    def test_delete_entry(self):
        loc = self._single(lambda i, h: i.delete_entry(h))
        self.assertEqual(loc["faulty_records"]["missing"], loc["affected_record_hashes"])

    def test_spurious_entry(self):
        ht, tree, aud, inj = make_index()
        f = inj.insert_spurious()
        loc = aud.audit()["localization"]
        self.assertEqual(loc["faulty_records"]["extra"], [f["chunk_hash"]])

    def test_merkle_leaf_only(self):
        ht, tree, aud, inj = make_index()
        inj.corrupt_merkle_leaf(9)
        r = aud.audit()
        self.assertTrue(r["detection"]["tree_fault"])
        self.assertFalse(r["detection"]["data_fault"])
        loc = r["localization"]
        self.assertEqual(loc["tree_leaf_faults"], [9])
        self.assertEqual(loc["affected_record_hashes"], [])

    def test_multiple_faults_exact(self):
        for seed in range(5):
            ht, tree, aud, inj = make_index(n=2000, depth=9, seed=seed)
            inj.inject_random(20, kinds=list(inj.METADATA_KINDS[:4]) + ["insert_spurious"])
            inj.insert_spurious()
            loc = aud.audit()["localization"]
            self.assertEqual(set(loc["affected_record_hashes"]), inj.affected_hashes())
            self.assertFalse(loc["ambiguous_buckets"])

    def test_mixed_data_and_leaf_faults(self):
        ht, tree, aud, inj = make_index(n=1000, depth=8, seed=3)
        inj.inject_random(5, merkle_leaves=3)
        loc = aud.audit()["localization"]
        self.assertTrue(inj.affected_hashes() <= set(loc["affected_record_hashes"]))
        self.assertTrue(inj.affected_leaf_buckets() <= set(loc["flagged_buckets"]))

    def test_nodes_compared_bounded(self):
        ht, tree, aud, inj = make_index(n=4000, depth=10, seed=7)
        inj.inject_random(4)
        loc = aud.audit()["localization"]
        self.assertLessEqual(loc["nodes_compared"], 1 + 2 * 4 * tree.depth)


class TestIntegrationWithCAS(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "a.bin")
        with open(self.f, "wb") as fh:
            fh.write(b"".join(bytes([i]) * 64 for i in range(20)) + b"A" * 64 * 3)
        self.cas = ContentAddressableStorage(chunk_size=64, storage_root=os.path.join(self.d, "cs"))
        self.aud = IndexAuditor(self.cas.hash_table, BucketMerkleTree(5)).attach(self.cas)

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_uploads_keep_tree_in_sync(self):
        self.cas.upload_file(self.f)
        self.cas.upload_file(self.f)          # duplicates bump ref_count -> leaves change
        self.assertTrue(self.aud.audit()["detection"]["intact"])

    def test_chunk_deletion_invisible_to_metadata_merkle(self):
        self.cas.upload_file(self.f)
        h = next(iter(self.cas.hash_table.items()))[0]
        FaultInjector(self.cas.hash_table, self.aud.tree, self.cas.storage).delete_chunk(h)
        self.assertTrue(self.aud.audit()["detection"]["intact"])   # needs chunk-level check (Phase 3)
        self.assertFalse(self.cas.storage.exists(h))


if __name__ == "__main__":
    unittest.main()

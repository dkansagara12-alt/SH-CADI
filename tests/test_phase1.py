import os, shutil, tempfile, unittest
from sh_cadi.dedup_system import ContentAddressableStorage
from sh_cadi.hash_index.hashtable import HashTable
from sh_cadi.merkle.merkle_tree import MerkleTree
from sh_cadi.merkle.integrity import IntegritySystem


class TestHashTable(unittest.TestCase):
    def test_crud_and_resize(self):
        ht = HashTable(capacity=4)
        keys = [f"{i:08x}" + "0" * 56 for i in range(50)]
        for k in keys:
            ht.insert(k, {"v": k})
        self.assertEqual(len(ht), 50)
        self.assertEqual(ht.search(keys[7])["v"], keys[7])
        self.assertTrue(ht.update(keys[7], {"v": "x"}))
        self.assertTrue(ht.delete(keys[7]))
        self.assertIsNone(ht.search(keys[7]))
        self.assertFalse(ht.delete(keys[7]))


class TestMerkle(unittest.TestCase):
    def test_proofs(self):
        leaves = [str(i) for i in range(7)]
        t = MerkleTree(leaves)
        for i, l in enumerate(leaves):
            self.assertTrue(MerkleTree.verify_proof(l, t.get_proof(i), t.get_root()))
        self.assertFalse(MerkleTree.verify_proof("bad", t.get_proof(0), t.get_root()))


class TestDedupIntegrity(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, "a.bin")
        with open(self.f, "wb") as fh:
            fh.write(b"A" * 64 * 5 + b"B" * 64)
        self.cas = ContentAddressableStorage(
            chunk_size=64, storage_root=os.path.join(self.d, "cs"))

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_dedup_and_reconstruct(self):
        r = self.cas.upload_file(self.f)
        self.assertEqual((r["unique_chunks"], r["duplicate_chunks"]), (2, 4))
        out = os.path.join(self.d, "out.bin")
        self.cas.reconstruct_file(self.f, out)
        with open(out, "rb") as a, open(self.f, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_ref_count_tamper_detected(self):
        self.cas.upload_file(self.f)
        ig = IntegritySystem(self.cas)
        ig.generate_root_hash()
        self.assertTrue(ig.verify_integrity()["valid"])
        h, e = next(iter(self.cas.hash_table.items()))
        e = dict(e)
        e["ref_count"] += 1
        self.cas.hash_table.update(h, e)
        self.assertFalse(ig.verify_integrity()["valid"])
        self.assertEqual(ig.diagnose_changes()["changed"], [h])


if __name__ == "__main__":
    unittest.main()

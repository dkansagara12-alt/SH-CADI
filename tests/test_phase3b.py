import json, os, shutil, tempfile, unittest
from sh_cadi.benchmark.systems import (ConventionalIndex, SHCADIIndex, TargetedConventionalIndex,
                                       build, make_record)
from sh_cadi.benchmark.worker import shard_loss, summarize, trial_conv, trial_shcadi
from sh_cadi.benchmark import normal_ops
from sh_cadi.fault_injection.injector import FaultInjector
from sh_cadi.hash_index.hashtable import HashTable


class TestSystems(unittest.TestCase):
    def test_clean_systems_report_intact(self):
        for cls in (ConventionalIndex, TargetedConventionalIndex, SHCADIIndex):
            s = build(cls, 800)
            self.assertFalse(s.detect(), cls.name)
            self.assertIsNotNone(s.lookup(make_record(5)[0]))
            self.assertIsNone(s.lookup(make_record(5, "absent")[0]))

    def test_all_systems_restore_exactly_and_localize_exactly(self):
        for cls, fn in ((SHCADIIndex, trial_shcadi), (ConventionalIndex, trial_conv),
                        (TargetedConventionalIndex, trial_conv)):
            s = build(cls, 1500)
            for k, seed in ((1, 1), (25, 2), (150, 3)):
                r = fn(s, k, seed)
                self.assertTrue(r["success"], (cls.name, k))
                self.assertTrue(r["localized_exactly"], (cls.name, k))
            self.assertFalse(s.detect())

    def test_locality_ordering(self):
        full, targeted, sh = (build(c, 1500) for c in (ConventionalIndex, TargetedConventionalIndex, SHCADIIndex))
        rf, rt, rs = trial_conv(full, 20, 9), trial_conv(targeted, 20, 9), trial_shcadi(sh, 20, 9)
        self.assertEqual(rf["reconstructed"], 1500)
        self.assertEqual(rt["reconstructed"], rs["reconstructed"])
        self.assertLess(rs["locality"], 0.02)

    def test_shard_loss_scenario(self):
        out = {}
        for cls, shc in ((ConventionalIndex, False), (TargetedConventionalIndex, False), (SHCADIIndex, True)):
            out[cls.name] = shard_loss(build(cls, 2000), shc, 10, 7)
        self.assertFalse(out["conventional"]["success"])
        self.assertGreater(out["conventional"]["records_lost_by_recovery"], 0)
        for name in ("conventional_targeted", "shcadi"):
            self.assertTrue(out[name]["success"], name)
            self.assertEqual(out[name]["records_lost_by_recovery"], 0)

    def test_shcadi_insert_keeps_index_intact(self):
        s = build(SHCADIIndex, 500)
        for i in range(100):
            s.insert(*make_record(i, "new"))
        self.assertFalse(s.detect())
        self.assertEqual(s.verified_lookup(make_record(3, "new")[0])[1], "ok")


class TestHarness(unittest.TestCase):
    def test_injector_without_tree(self):
        ht = HashTable()
        for i in range(50):
            h, e = make_record(i); ht.insert(h, e)
        inj = FaultInjector(ht, None, seed=1)
        self.assertEqual(len(inj.inject_random(5)), 5)

    def test_summarize(self):
        s = summarize([{"a": 1.0, "ok": True}, {"a": 3.0, "ok": False}])
        self.assertEqual(s["a"]["median"], 2.0)
        self.assertEqual(s["ok"], 0.5)

    def test_normal_ops_runs(self):
        r = normal_ops.run(reps=1, unique=100, total=200)
        self.assertEqual(set(r["configs"]), {"plain", "conventional", "shcadi"})
        self.assertAlmostEqual(r["configs"]["plain"]["overhead_vs_plain"], 1.0)


if __name__ == "__main__":
    unittest.main()

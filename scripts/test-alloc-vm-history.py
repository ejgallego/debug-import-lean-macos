#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest
s=importlib.util.spec_from_file_location('vm',Path(__file__).with_name('summarize-lean-alloc-vm.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class HistoriesTest(unittest.TestCase):
    def test_partial_purge_and_remap(self):
        h=m.Histories();h.apply(100,200,'map');h.apply(120,140,'purge')
        self.assertFalse(h.get(119)['purge_seen']);self.assertTrue(h.get(120)['purge_seen']);self.assertFalse(h.get(140)['purge_seen'])
        h.apply(125,135,'unmap');self.assertEqual(h.get(130),{});self.assertTrue(h.get(124)['purge_seen'])
        h.apply(125,135,'map');self.assertFalse(h.get(130)['purge_seen']);self.assertTrue(h.get(139)['purge_seen'])
        self.assertEqual(h.get(200),{})
    def test_purge_without_observed_map(self):
        h=m.Histories();h.apply(100,200,'purge');self.assertTrue(h.get(150)['purge_seen']);self.assertNotIn('map_seen',h.get(150))
        h.apply(0,300,'map');self.assertFalse(h.get(150)['purge_seen'])
if __name__=='__main__':unittest.main()

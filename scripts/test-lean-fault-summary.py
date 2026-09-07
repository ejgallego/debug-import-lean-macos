#!/usr/bin/env python3
"""Check trace coverage failures and target identity on known event fixtures."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('summary',Path(__file__).with_name('summarize-lean-faults.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class TraceTests(unittest.TestCase):
    def parse(self,text,fn):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'trace';p.write_text(text);return fn(p,42)
    def test_mac_types_and_process_filter(self):
        text='100 0.0 1300009 1 1234000 0 0 abc 0(AP) lean(42)\n101 0.1(0.1) 130000a 1 1234000 0 9 abc 0(AP) lean(42)\n102 0.0 1300009 1 1234000 0 0 def 0(AP) lean(43)\n'
        faults,errors,other=self.parse(text,m.mac_faults)
        self.assertEqual(faults,[(100,101,0x1234000,'compressor')]);self.assertFalse(any(errors.values()));self.assertEqual(other,{'43':1})
    def test_mac_incomplete_and_malformed_capture(self):
        text='100 0.0 1300009 1 1234000 0 0 abc 0(AP) lean(42)\nLOST EVENTS\nmalformed 130000a\n'
        faults,errors,_=self.parse(text,m.mac_faults)
        self.assertFalse(faults);self.assertEqual(errors,{'loss_messages':1,'unparsed_fault_lines':1,'unfinished':1})
    def test_linux_timestamp_address_and_identity(self):
        text='lean 42/43 123.000001: minor-faults: 1234000\nlean 44/44 123.1: major-faults: 1234000\n'
        faults,errors,other=self.parse(text,m.linux_faults)
        self.assertEqual(faults,[(123000001000,123000001000,0x1234000,'minor-faults')]);self.assertFalse(errors);self.assertEqual(other,{'44':1})
    def test_ranges_are_half_open(self):
        ranges=m.Ranges([(0x1000,0x2000,'artifact'),(0x3000,0x4000,'anonymous')])
        self.assertEqual(ranges.find(0x1fff)[2],'artifact');self.assertIsNone(ranges.find(0x2000))

if __name__=='__main__':unittest.main()

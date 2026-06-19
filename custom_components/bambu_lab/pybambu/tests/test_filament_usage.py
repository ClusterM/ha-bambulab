"""Tests for per-layer 3MF filament usage parsing."""

import unittest
import zipfile
from io import BytesIO

from pybambu.filament_usage import (
    EXTERNAL_SPOOL_TRAY,
    LayerUsage,
    analyze_plate_from_zipfile,
    decode_mapping_value,
    decode_mapping_list,
    parse_gcode,
    parse_slice_info,
)


MINIMAL_GCODE = """; total filament length [mm] : 100.0
; total filament weight [g] : 1.0
M620 S0A
G1 X1 Y1 E10
; CHANGE_LAYER
G1 X2 Y2 E5
; CHANGE_LAYER
G1 X3 Y3 E3
M621 S0A
"""

SLICE_INFO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" used_m="0.1" used_g="1.0"/>
  </plate>
</config>
"""


def _make_3mf_zip(gcode: str = MINIMAL_GCODE) -> zipfile.ZipFile:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", gcode)
        zf.writestr("Metadata/slice_info.config", SLICE_INFO_XML)
    buffer.seek(0)
    return zipfile.ZipFile(buffer, "r")


class TestDecodeMapping(unittest.TestCase):
    def test_flat_tray_indices(self):
        self.assertEqual(decode_mapping_value(2), 2)
        self.assertEqual(decode_mapping_value(1), 1)

    def test_snow_encoding(self):
        self.assertEqual(decode_mapping_value(258), 6)
        self.assertEqual(decode_mapping_value(65280), EXTERNAL_SPOOL_TRAY)

    def test_unused(self):
        self.assertIsNone(decode_mapping_value(-1))

    def test_decode_list(self):
        self.assertEqual(decode_mapping_list([2, 1]), [2, 1])


class TestParseGcode(unittest.TestCase):
    def test_layer_deltas(self):
        layers, _, _ = parse_gcode(MINIMAL_GCODE, include_flush=False)
        by_layer = {layer.layer: layer for layer in layers}
        self.assertIn(0, by_layer)
        self.assertIn(1, by_layer)
        self.assertGreater(by_layer[0].length_mm.get(0, 0), 0)
        self.assertGreater(by_layer[1].length_mm.get(0, 0), 0)


class TestAnalyzePlateFromZipfile(unittest.TestCase):
    def test_analyze_plate_from_zipfile(self):
        with _make_3mf_zip() as archive:
            usage = analyze_plate_from_zipfile(archive, 1, include_flush=False)
        self.assertEqual(usage.plate, 1)
        self.assertTrue(usage.calibrated)
        self.assertGreater(len(usage.layers), 0)
        self.assertGreater(usage.total_mass_g(), 0)


class TestParseSliceInfo(unittest.TestCase):
    def test_parse_slice_info(self):
        refs = parse_slice_info(SLICE_INFO_XML, 1)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].index, 0)
        self.assertEqual(refs[0].mass_g, 1.0)

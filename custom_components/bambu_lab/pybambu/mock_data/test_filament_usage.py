"""Tests for per-layer 3MF filament usage parsing."""

import unittest
import zipfile
from io import BytesIO

from pybambu.filament_usage import (
    EXTERNAL_SPOOL_TRAY,
    SECOND_EXTERNAL_SPOOL_TRAY,
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
        self.assertEqual(decode_mapping_value(65024), SECOND_EXTERNAL_SPOOL_TRAY)
        self.assertEqual(decode_mapping_value(65279), SECOND_EXTERNAL_SPOOL_TRAY)

    def test_legacy_flat_external_spool(self):
        self.assertEqual(decode_mapping_value(254), EXTERNAL_SPOOL_TRAY)
        self.assertEqual(decode_mapping_value(255), EXTERNAL_SPOOL_TRAY)

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


CHANGES_GCODE = """M620 S0A
M621 S0A
M620 S0A
M621 S0A
G1 X1 Y1 E1
; CHANGE_LAYER
G1 X1 Y1 E1
M620 S1A B H1
M620.10 A0 F100 L0.0 H0.4 T220 P220 S1
M620.10 A1 F100 L0.0 H0.4 T220 P220 S1
T1 H1
M620.10 R0.8
M621 S1A B
G1 X1 Y1 E1
; CHANGE_LAYER
M620 S2A B H0
M620.10 A0 F100 L57.4 H0.4 T220 P220 S1
M620.10 A1 F100 L57.4 H0.4 T220 P220 S1
T2 H0
M621 S2A B
G1 X1 Y1 E1
; CHANGE_LAYER
M620 S0A
T0
; FLUSH_START
G1 E10 F300
; FLUSH_END
M621 S0A
G1 X1 Y1 E1
M620 S255A
M621 S255A
"""


class TestParseFilamentChanges(unittest.TestCase):
    def test_changes_and_purges(self):
        changes = []
        parse_gcode(CHANGES_GCODE, filament_changes=changes)

        self.assertEqual(
            [(c.layer, c.from_filament, c.to_filament) for c in changes],
            [(0, 0, 1), (1, 1, 2), (2, 2, 0)],
        )
        self.assertFalse(changes[0].is_purge)
        self.assertTrue(changes[1].is_purge)
        self.assertAlmostEqual(changes[1].flush_length_mm, 114.8)
        self.assertTrue(changes[2].is_purge)
        self.assertEqual(changes[2].flush_length_mm, 0.0)

    def test_changes_collected_without_flush_usage(self):
        changes = []
        parse_gcode(CHANGES_GCODE, include_flush=False, filament_changes=changes)
        self.assertEqual(sum(c.is_purge for c in changes), 2)

    def test_changes_not_collected_by_default(self):
        layers, _, _ = parse_gcode(CHANGES_GCODE)
        self.assertGreater(len(layers), 0)


class TestAnalyzePlateFromZipfile(unittest.TestCase):
    def test_analyze_plate_from_zipfile(self):
        with _make_3mf_zip() as archive:
            usage = analyze_plate_from_zipfile(archive, 1, include_flush=False)
        self.assertEqual(usage.plate, 1)
        self.assertTrue(usage.calibrated)
        self.assertGreater(len(usage.layers), 0)
        self.assertGreater(usage.total_mass_g(), 0)
        self.assertEqual(usage.filament_changes, [])

    def test_analyze_plate_collects_filament_changes(self):
        with _make_3mf_zip(CHANGES_GCODE) as archive:
            usage = analyze_plate_from_zipfile(archive, 1, calibrate=False)
        self.assertEqual(len(usage.filament_changes), 3)
        self.assertEqual(sum(c.is_purge for c in usage.filament_changes), 2)


class TestParseSliceInfo(unittest.TestCase):
    def test_parse_slice_info(self):
        refs = parse_slice_info(SLICE_INFO_XML, 1)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].index, 0)
        self.assertEqual(refs[0].mass_g, 1.0)

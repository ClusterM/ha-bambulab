"""Tests for per-layer filament used events and tray usage accumulation."""

import unittest
from unittest.mock import MagicMock

from pybambu.filament_usage import LayerUsage, PlateUsage, FilamentInfo
from pybambu.print_filament import (
    FilamentPrintState,
    clear_filament_print_state,
    get_tray_planned,
    get_tray_usage,
    pop_filament_used_events,
    process_layer_updates,
    update_filament_mapping,
)


def _minimal_plate_usage() -> PlateUsage:
    return PlateUsage(
        plate=1,
        filaments=[FilamentInfo(index=0, length_mm=100.0, mass_g=10.0)],
        objects=[],
        layers=[
            LayerUsage(layer=0, length_mm={0: 4.0}, mass_g={0: 0.4}),
            LayerUsage(layer=1, length_mm={0: 6.0}, mass_g={0: 0.6}),
        ],
        object_usage=[],
        calibrated=True,
    )


class TestFilamentUsedEvents(unittest.TestCase):
    def setUp(self):
        self.state = FilamentPrintState()
        self.state.plate_usage = _minimal_plate_usage()
        self.device = MagicMock()
        self.device.supports_feature.return_value = True
        self.device.ams.active_ams_index = 0
        self.device.ams.active_tray_index = 2

    def test_mapping_refill_decode(self):
        update_filament_mapping(self.state, {"mapping": [2]})
        self.assertEqual(self.state.filament_mapping, [2])
        update_filament_mapping(self.state, {"mapping": [1]})
        self.assertEqual(self.state.filament_mapping, [1])

    def test_layer_change_emits_events(self):
        update_filament_mapping(self.state, {"mapping": [2]})
        process_layer_updates(
            self.state,
            previous_mqtt_layer=0,
            current_mqtt_layer=2,
            gcode_state="RUNNING",
            ftp_enabled=True,
            device=self.device,
        )
        events = pop_filament_used_events(self.state)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].layer, 0)
        self.assertEqual(events[0].tray_id, 2)
        self.assertAlmostEqual(events[0].mass_g, 0.4)

        usage = get_tray_usage(self.state, 2)
        self.assertAlmostEqual(usage.mass_g, 0.4)

    def test_finish_flushes_remaining_layers(self):
        update_filament_mapping(self.state, {"mapping": [2]})
        self.state.last_emitted_layer = 0
        process_layer_updates(
            self.state,
            previous_mqtt_layer=1,
            current_mqtt_layer=1,
            gcode_state="FINISH",
            ftp_enabled=True,
            device=self.device,
        )
        events = pop_filament_used_events(self.state)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].layer, 1)

    def test_planned_includes_remaining_with_new_mapping(self):
        update_filament_mapping(self.state, {"mapping": [2]})
        process_layer_updates(
            self.state,
            previous_mqtt_layer=0,
            current_mqtt_layer=2,
            gcode_state="RUNNING",
            ftp_enabled=True,
            device=self.device,
        )
        planned_tray2 = get_tray_planned(
            self.state,
            2,
            device=self.device,
            gcode_state="RUNNING",
            ftp_enabled=True,
        )
        self.assertAlmostEqual(planned_tray2.mass_g, 1.0, places=1)

        update_filament_mapping(self.state, {"mapping": [1]})
        planned_tray1 = get_tray_planned(
            self.state,
            1,
            device=self.device,
            gcode_state="RUNNING",
            ftp_enabled=True,
        )
        planned_tray2_after_refill = get_tray_planned(
            self.state,
            2,
            device=self.device,
            gcode_state="RUNNING",
            ftp_enabled=True,
        )
        self.assertAlmostEqual(planned_tray1.mass_g, 0.6, places=1)
        self.assertAlmostEqual(planned_tray2_after_refill.mass_g, 0.4, places=1)

    def test_layer_jump_credits_only_last_completed(self):
        """MQTT layer_num jump > 1 (skipped object) must not emit intermediate 3MF layers."""
        layers = [
            LayerUsage(layer=i, length_mm={0: 1.0}, mass_g={0: 0.1})
            for i in range(10)
        ]
        self.state.plate_usage = PlateUsage(
            plate=1,
            filaments=[FilamentInfo(index=0, length_mm=100.0, mass_g=1.0)],
            objects=[],
            layers=layers,
            object_usage=[],
            calibrated=True,
        )
        update_filament_mapping(self.state, {"mapping": [1]})
        self.state.last_emitted_layer = 3

        process_layer_updates(
            self.state,
            previous_mqtt_layer=5,
            current_mqtt_layer=11,
            gcode_state="RUNNING",
            ftp_enabled=True,
            device=self.device,
        )
        events = pop_filament_used_events(self.state)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].layer, 9)
        self.assertEqual(self.state.last_emitted_layer, 9)
        self.assertAlmostEqual(get_tray_usage(self.state, 1).mass_g, 0.1)

    def test_incremental_mqtt_still_catches_up_3mf(self):
        """MQTT +1 while 3MF is behind (late download) still emits all missing layers."""
        layers = [
            LayerUsage(layer=i, length_mm={0: 1.0}, mass_g={0: 0.1})
            for i in range(5)
        ]
        self.state.plate_usage = PlateUsage(
            plate=1,
            filaments=[FilamentInfo(index=0, length_mm=100.0, mass_g=0.5)],
            objects=[],
            layers=layers,
            object_usage=[],
            calibrated=True,
        )
        update_filament_mapping(self.state, {"mapping": [1]})
        self.state.last_emitted_layer = -1

        process_layer_updates(
            self.state,
            previous_mqtt_layer=3,
            current_mqtt_layer=4,
            gcode_state="RUNNING",
            ftp_enabled=True,
            device=self.device,
        )
        events = pop_filament_used_events(self.state)
        self.assertEqual(len(events), 3)
        self.assertEqual([e.layer for e in events], [0, 1, 2])
        self.assertAlmostEqual(get_tray_usage(self.state, 1).mass_g, 0.3)

    def test_clear_resets_state(self):
        update_filament_mapping(self.state, {"mapping": [2]})
        process_layer_updates(
            self.state,
            previous_mqtt_layer=0,
            current_mqtt_layer=2,
            gcode_state="RUNNING",
            ftp_enabled=True,
            device=self.device,
        )
        clear_filament_print_state(self.state)
        self.assertEqual(len(pop_filament_used_events(self.state)), 0)
        self.assertEqual(get_tray_usage(self.state, 2).mass_g, 0.0)

"""Per-layer filament usage tracking for active prints (FTPS + print.mapping)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import Features, LOGGER
from .filament_usage import (
    EXTERNAL_SPOOL_TRAY,
    PlateUsage,
    decode_mapping_list,
    decode_mapping_value,
)

ACTIVE_PRINT_STATES = frozenset({"RUNNING", "PAUSE", "PREPARE"})


@dataclass
class TrayPrintUsage:
    mass_g: float = 0.0
    length_mm: float = 0.0


@dataclass
class FilamentUsedEvent:
    filament_index: int
    tray_id: int
    layer: int
    mass_g: float
    length_mm: float


@dataclass
class FilamentPrintState:
    plate_usage: PlateUsage | None = None
    filament_mapping: list[int | None] = field(default_factory=list)
    last_emitted_layer: int = -1
    layer_mqtt_offset: int | None = None
    current_3mf_layer: int = -1
    tray_print_usage: dict[int, TrayPrintUsage] = field(default_factory=dict)
    filament_used_events: list[FilamentUsedEvent] = field(default_factory=list)
    slice_metadata_planned: dict[int, TrayPrintUsage] = field(default_factory=dict)


def clear_filament_print_state(state: FilamentPrintState) -> None:
    state.plate_usage = None
    state.filament_mapping = []
    state.last_emitted_layer = -1
    state.layer_mqtt_offset = None
    state.current_3mf_layer = -1
    state.tray_print_usage = {}
    state.filament_used_events = []
    state.slice_metadata_planned = {}


def update_filament_mapping(state: FilamentPrintState, data: dict) -> None:
    if "mapping" in data:
        state.filament_mapping = decode_mapping_list(data["mapping"])


def get_filament_tray_index(
    state: FilamentPrintState,
    filament_index: int,
    *,
    device,
) -> int | None:
    if state.filament_mapping and filament_index < len(state.filament_mapping):
        tray = state.filament_mapping[filament_index]
        if tray is not None:
            return tray

    if (
        state.plate_usage
        and len(state.plate_usage.filaments) == 1
        and filament_index == 0
        and device is not None
    ):
        return _active_tray_from_device(device)

    return None


def _active_tray_from_device(device) -> int | None:
    if device is None:
        return None
    if device.supports_feature(Features.AMS):
        ams_index = device.ams.active_ams_index
        tray_index = device.ams.active_tray_index
        if ams_index == 255:
            return EXTERNAL_SPOOL_TRAY
        if ams_index >= 128:
            return ams_index
        if ams_index < 255 and tray_index < 4:
            return ams_index * 4 + tray_index
    return EXTERNAL_SPOOL_TRAY


def _mqtt_to_last_completed_3mf_layer(mqtt_layer: int, offset: int) -> int:
    return mqtt_layer - offset - 1


def _ensure_layer_offset(state: FilamentPrintState, mqtt_layer: int) -> None:
    if state.layer_mqtt_offset is None:
        state.layer_mqtt_offset = 1 if mqtt_layer >= 1 else 0


def _layer_usage_by_index(plate_usage: PlateUsage, layer_index: int):
    for layer in plate_usage.layers:
        if layer.layer == layer_index:
            return layer
    return None


def _accumulate_layer(
    state: FilamentPrintState,
    layer_index: int,
    *,
    device,
) -> None:
    if state.plate_usage is None:
        return

    layer = _layer_usage_by_index(state.plate_usage, layer_index)
    if layer is None:
        return

    for filament_index, mass_g in layer.mass_g.items():
        if mass_g <= 0 and layer.length_mm.get(filament_index, 0) <= 0:
            continue
        length_mm = layer.length_mm.get(filament_index, 0.0)
        if mass_g <= 0 and length_mm <= 0:
            continue

        tray_id = get_filament_tray_index(state, filament_index, device=device)
        if tray_id is None:
            LOGGER.debug(
                "Skipping layer %s filament %s: no tray mapping",
                layer_index,
                filament_index,
            )
            continue

        usage = state.tray_print_usage.setdefault(tray_id, TrayPrintUsage())
        usage.mass_g += mass_g
        usage.length_mm += length_mm

        state.filament_used_events.append(
            FilamentUsedEvent(
                filament_index=filament_index,
                tray_id=tray_id,
                layer=layer_index,
                mass_g=mass_g,
                length_mm=length_mm,
            )
        )


def _emit_layers_up_to(
    state: FilamentPrintState,
    last_completed: int,
    *,
    device,
) -> None:
    if last_completed <= state.last_emitted_layer:
        return
    for layer_index in range(state.last_emitted_layer + 1, last_completed + 1):
        _accumulate_layer(state, layer_index, device=device)
    state.last_emitted_layer = last_completed


def _max_3mf_layer(plate_usage: PlateUsage) -> int:
    if not plate_usage.layers:
        return -1
    return max(layer.layer for layer in plate_usage.layers)


def process_layer_updates(
    state: FilamentPrintState,
    *,
    previous_mqtt_layer: int,
    current_mqtt_layer: int,
    gcode_state: str,
    ftp_enabled: bool,
    device,
) -> None:
    if not ftp_enabled or state.plate_usage is None:
        return

    _ensure_layer_offset(state, current_mqtt_layer)
    offset = state.layer_mqtt_offset or 0

    if gcode_state in ACTIVE_PRINT_STATES:
        if current_mqtt_layer != previous_mqtt_layer:
            last_completed = _mqtt_to_last_completed_3mf_layer(current_mqtt_layer, offset)
            mqtt_delta = current_mqtt_layer - previous_mqtt_layer
            LOGGER.debug(
                "Layer change MQTT %s -> %s (delta %s), 3MF completed through %s",
                previous_mqtt_layer,
                current_mqtt_layer,
                mqtt_delta,
                last_completed,
            )
            if mqtt_delta == 1:
                # Normal step or late 3MF catch-up while MQTT advances one layer at a time.
                _emit_layers_up_to(state, last_completed, device=device)
            elif mqtt_delta > 1:
                # Skipped print objects (or sparse MQTT): intermediate 3MF layers were not printed.
                if last_completed > state.last_emitted_layer:
                    first_skipped = state.last_emitted_layer + 1
                    last_skipped = last_completed - 1
                    _accumulate_layer(state, last_completed, device=device)
                    state.last_emitted_layer = last_completed
                    if first_skipped <= last_skipped:
                        LOGGER.debug(
                            "Layer jump: credited only 3MF layer %s (skipped %s..%s)",
                            last_completed,
                            first_skipped,
                            last_skipped,
                        )
                    else:
                        LOGGER.debug(
                            "Layer jump: credited 3MF layer %s",
                            last_completed,
                        )
            else:
                LOGGER.debug(
                    "Non-monotonic layer_num %s -> %s, no filament events",
                    previous_mqtt_layer,
                    current_mqtt_layer,
                )
            state.current_3mf_layer = last_completed

    elif gcode_state == "FINISH":
        max_layer = _max_3mf_layer(state.plate_usage)
        if max_layer >= 0:
            _emit_layers_up_to(state, max_layer, device=device)
            state.current_3mf_layer = max_layer


def build_slice_metadata_planned(
    plate_usage: PlateUsage,
    filament_mapping: list[int | None],
) -> dict[int, TrayPrintUsage]:
    planned: dict[int, TrayPrintUsage] = {}
    for filament in plate_usage.filaments:
        if filament.index >= len(filament_mapping):
            continue
        tray_id = (
            filament_mapping[filament.index]
            if filament_mapping
            else None
        )
        if tray_id is None:
            continue
        entry = planned.setdefault(tray_id, TrayPrintUsage())
        entry.mass_g += filament.mass_g
        entry.length_mm += filament.length_mm
    return planned


def get_tray_usage(state: FilamentPrintState, flat_tray: int) -> TrayPrintUsage:
    return state.tray_print_usage.get(flat_tray, TrayPrintUsage())


def get_tray_planned(
    state: FilamentPrintState,
    flat_tray: int,
    *,
    device,
    gcode_state: str,
    ftp_enabled: bool,
) -> TrayPrintUsage:
    if not ftp_enabled or (
        gcode_state not in ACTIVE_PRINT_STATES and gcode_state != "FINISH"
    ):
        return TrayPrintUsage()

    used = get_tray_usage(state, flat_tray)
    if state.plate_usage is None:
        meta = state.slice_metadata_planned.get(flat_tray, TrayPrintUsage())
        return TrayPrintUsage(
            mass_g=meta.mass_g,
            length_mm=meta.length_mm,
        )

    remaining_mass = 0.0
    remaining_length = 0.0
    current_completed = state.last_emitted_layer

    for layer in state.plate_usage.layers:
        if layer.layer <= current_completed:
            continue
        for filament_index, mass_g in layer.mass_g.items():
            length_mm = layer.length_mm.get(filament_index, 0.0)
            if mass_g <= 0 and length_mm <= 0:
                continue
            tray_id = get_filament_tray_index(state, filament_index, device=device)
            if tray_id == flat_tray:
                remaining_mass += mass_g
                remaining_length += length_mm

    return TrayPrintUsage(
        mass_g=used.mass_g + remaining_mass,
        length_mm=used.length_mm + remaining_length,
    )


def pop_filament_used_events(state: FilamentPrintState) -> list[FilamentUsedEvent]:
    events = list(state.filament_used_events)
    state.filament_used_events = []
    return events

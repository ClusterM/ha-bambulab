from __future__ import annotations

"""Parse Bambu Studio 3MF gcode bundles and estimate filament usage per layer.

The main entry point is :func:`analyze_plate`. For lower-level control, combine
:func:`read_plate_gcode`, :func:`parse_slice_info`, and :func:`parse_gcode`.
"""

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CHANGE_LAYER_MARKER = "; CHANGE_LAYER"
M620_PATTERN = re.compile(r"^M620 S(\d+)A")
M621_PATTERN = re.compile(r"^M621 S(\d+)A")
M620_10_PATTERN = re.compile(r"^M620\.10 A(\d+) .* L([\d.]+)")
EXTRUSION_PATTERN = re.compile(r"^G[123]\b")
E_VALUE_PATTERN = re.compile(r"(?:^|\s)E(-?\d*\.?\d+(?:e[+-]?\d+)?)", re.IGNORECASE)
HEADER_LENGTH_PATTERN = re.compile(
    r"; total filament length \[mm\] : (.+)$", re.IGNORECASE
)
HEADER_WEIGHT_PATTERN = re.compile(
    r"; total filament weight \[g\] : (.+)$", re.IGNORECASE
)
OBJECT_ID_PATTERN = re.compile(r"; OBJECT_ID: (\d+)")
START_OBJECT_PATTERN = re.compile(
    r"; start printing object, unique label id: (\d+)"
)
STOP_OBJECT_PATTERN = re.compile(
    r"; stop printing object, unique label id: (\d+)"
)
SHARED_OBJECT_NAME = "__shared__"


@dataclass(frozen=True)
class FilamentInfo:
    index: int
    length_mm: float
    mass_g: float

    @property
    def grams_per_mm(self) -> float:
        if self.length_mm <= 0:
            return 0.0
        return self.mass_g / self.length_mm


@dataclass(frozen=True)
class ObjectInfo:
    identify_id: int
    name: str


@dataclass
class LayerUsage:
    layer: int
    length_mm: dict[int, float] = field(default_factory=dict)
    mass_g: dict[int, float] = field(default_factory=dict)

    def total_length_mm(self, filament: int | None = None) -> float:
        if filament is None:
            return sum(self.length_mm.values())
        return self.length_mm.get(filament, 0.0)

    def total_mass_g(self, filament: int | None = None) -> float:
        if filament is None:
            return sum(self.mass_g.values())
        return self.mass_g.get(filament, 0.0)


@dataclass
class ObjectUsage:
    identify_id: int | None
    name: str
    layers: list[LayerUsage] = field(default_factory=list)
    raw_length_mm: dict[int, float] = field(default_factory=dict)

    def total_length_mm(self, filament: int | None = None) -> float:
        if filament is None:
            return sum(layer.total_length_mm() for layer in self.layers)
        return sum(layer.total_length_mm(filament) for layer in self.layers)

    def total_mass_g(self, filament: int | None = None) -> float:
        if filament is None:
            return sum(layer.total_mass_g() for layer in self.layers)
        return sum(layer.total_mass_g(filament) for layer in self.layers)

    def cumulative_layers(
        self,
        filaments: list[int] | None = None,
    ) -> list[LayerUsage]:
        """Return this object's layers as running totals."""
        return cumulative_layers(self.layers, filaments)


@dataclass
class PlateUsage:
    plate: int
    filaments: list[FilamentInfo]
    objects: list[ObjectInfo]
    layers: list[LayerUsage]
    object_usage: list[ObjectUsage]
    raw_length_mm: dict[int, float] = field(default_factory=dict)
    calibrated: bool = False

    def total_length_mm(self, filament: int | None = None) -> float:
        if filament is None:
            return sum(layer.total_length_mm() for layer in self.layers)
        return sum(layer.total_length_mm(filament) for layer in self.layers)

    def total_mass_g(self, filament: int | None = None) -> float:
        if filament is None:
            return sum(layer.total_mass_g() for layer in self.layers)
        return sum(layer.total_mass_g(filament) for layer in self.layers)

    def get_object_usage(self, identify_id: int | None) -> ObjectUsage | None:
        for item in self.object_usage:
            if item.identify_id == identify_id:
                return item
        return None

    def cumulative_layers(self) -> list[LayerUsage]:
        """Return plate layers as running totals instead of per-layer deltas."""
        return cumulative_layers(self.layers, [item.index for item in self.filaments])


def _filament_indices_from_layers(layers: list[LayerUsage]) -> list[int]:
    indices: set[int] = set()
    for layer in layers:
        indices.update(layer.length_mm)
        indices.update(layer.mass_g)
    return sorted(indices)


def cumulative_layers(
    layers: list[LayerUsage],
    filaments: list[int] | None = None,
) -> list[LayerUsage]:
    """Convert per-layer deltas into running totals.

    Each returned ``LayerUsage`` contains the sum of consumption from the
    first layer up to and including that layer. The input list is not modified;
    a new list of new ``LayerUsage`` instances is returned.

    Args:
        layers: Per-layer usage values, typically from ``analyze_plate`` or
            ``parse_gcode``.
        filaments: Filament indices to include in the output. When omitted,
            indices are inferred from all keys present in the input layers.

    Returns:
        A new list sorted by layer number with cumulative ``length_mm`` and
        ``mass_g`` values per filament.
    """
    if not layers:
        return []

    filament_indices = (
        list(filaments)
        if filaments is not None
        else _filament_indices_from_layers(layers)
    )
    running_length = dict.fromkeys(filament_indices, 0.0)
    running_mass = dict.fromkeys(filament_indices, 0.0)
    cumulative: list[LayerUsage] = []

    for layer in sorted(layers, key=lambda item: item.layer):
        for index in filament_indices:
            running_length[index] += layer.length_mm.get(index, 0.0)
            running_mass[index] += layer.mass_g.get(index, 0.0)
        cumulative.append(
            LayerUsage(
                layer=layer.layer,
                length_mm=dict(running_length),
                mass_g=dict(running_mass),
            )
        )

    return cumulative


def read_plate_gcode(thmf_path: str | Path, plate: int) -> str:
    path = Path(thmf_path)
    gcode_name = f"Metadata/plate_{plate}.gcode"

    with zipfile.ZipFile(path) as archive:
        try:
            return archive.read(gcode_name).decode("utf-8")
        except KeyError as exc:
            raise FileNotFoundError(
                f"G-code for plate {plate} not found in {path.name}"
            ) from exc


def read_slice_info(thmf_path: str | Path) -> str:
    path = Path(thmf_path)
    with zipfile.ZipFile(path) as archive:
        try:
            return archive.read("Metadata/slice_info.config").decode("utf-8")
        except KeyError as exc:
            raise FileNotFoundError(
                f"Metadata/slice_info.config not found in {path.name}"
            ) from exc


def _parse_csv_floats(value: str) -> list[float]:
    return [float(part.strip()) for part in value.split(",") if part.strip()]


def _filament_index_from_slice_id(slice_id: str) -> int:
    # Bambu slice_info uses 1-based filament ids; gcode uses 0-based slots.
    return int(slice_id) - 1


def _find_plate_node(root: ET.Element, plate: int) -> ET.Element:
    for node in root.findall("plate"):
        index_node = node.find("./metadata[@key='index']")
        if index_node is not None and int(index_node.get("value", "0")) == plate:
            return node

    available = []
    for node in root.findall("plate"):
        index_node = node.find("./metadata[@key='index']")
        if index_node is not None:
            available.append(int(index_node.get("value", "0")))
    raise ValueError(
        f"Plate {plate} not found in slice_info.config "
        f"(available plates: {sorted(available) or 'none'})"
    )


def parse_slice_info_objects(slice_info_xml: str, plate: int) -> list[ObjectInfo]:
    root = ET.fromstring(slice_info_xml)
    plate_node = _find_plate_node(root, plate)
    objects = [
        ObjectInfo(
            identify_id=int(node.get("identify_id", "0")),
            name=node.get("name", ""),
        )
        for node in plate_node.findall("object")
    ]
    return sorted(objects, key=lambda item: item.identify_id)


def parse_slice_info(
    slice_info_xml: str,
    plate: int,
    *,
    gcode_header: str | None = None,
) -> list[FilamentInfo]:
    root = ET.fromstring(slice_info_xml)
    plate_node = _find_plate_node(root, plate)

    filaments: list[FilamentInfo] = []
    for filament_node in plate_node.findall("filament"):
        index = _filament_index_from_slice_id(filament_node.get("id", "1"))
        used_m = float(filament_node.get("used_m", "0"))
        used_g = float(filament_node.get("used_g", "0"))
        filaments.append(
            FilamentInfo(
                index=index,
                length_mm=used_m * 1000.0,
                mass_g=used_g,
            )
        )

    if filaments:
        return sorted(filaments, key=lambda item: item.index)

    if gcode_header is None:
        raise ValueError(
            f"No filament usage found for plate {plate} in slice_info.config"
        )

    return _filament_info_from_gcode_header(gcode_header)


def _find_first_change_layer(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == CHANGE_LAYER_MARKER:
            return index
    return None


def _find_next_object_id(lines: list[str], start_index: int) -> int | None:
    for line in lines[start_index : start_index + 300]:
        stripped = line.strip()
        for pattern in (OBJECT_ID_PATTERN, START_OBJECT_PATTERN):
            match = pattern.match(stripped)
            if match:
                return int(match.group(1))
    return None


def _filament_info_from_gcode_header(gcode_text: str) -> list[FilamentInfo]:
    lengths: list[float] | None = None
    weights: list[float] | None = None

    for line in gcode_text.splitlines():
        stripped = line.strip()
        length_match = HEADER_LENGTH_PATTERN.search(stripped)
        if length_match:
            lengths = _parse_csv_floats(length_match.group(1))
            continue
        weight_match = HEADER_WEIGHT_PATTERN.search(stripped)
        if weight_match:
            weights = _parse_csv_floats(weight_match.group(1))

    if not lengths:
        raise ValueError("Could not find filament length totals in gcode header")

    if weights is None:
        weights = [0.0] * len(lengths)

    if len(weights) != len(lengths):
        raise ValueError("Filament length/weight header values do not match")

    return [
        FilamentInfo(index=index, length_mm=length, mass_g=weight)
        for index, (length, weight) in enumerate(zip(lengths, weights))
    ]


def _set_object_context(
    object_id: int,
    *,
    current_object: int | None,
    last_object: int | None,
    filament_object_map: dict[int, int],
    current_filament: int | None,
) -> tuple[int, int | None]:
    last_object = object_id
    current_object = object_id
    if current_filament is not None:
        filament_object_map[current_filament] = object_id
    return current_object, last_object


def _resolve_flush_object(
    side: int,
    *,
    pending_old: int | None,
    pending_new: int | None,
    last_object: int | None,
    lines: list[str],
    line_number: int,
    filament_object_map: dict[int, int],
) -> int | None:
    if side == 0:
        if pending_old is not None:
            mapped = filament_object_map.get(pending_old)
            if mapped is not None:
                return mapped
        return last_object

    if pending_new is not None:
        mapped = filament_object_map.get(pending_new)
        if mapped is not None:
            return mapped
    return _find_next_object_id(lines, line_number + 1) or last_object


def parse_gcode(
    gcode_text: str,
    *,
    include_flush: bool = True,
) -> tuple[list[LayerUsage], list[ObjectUsage], dict[int, float]]:
    lines = gcode_text.splitlines()
    first_change_layer = _find_first_change_layer(lines)

    current_filament: int | None = None
    pending_old: int | None = None
    pending_new: int | None = None
    current_object: int | None = None
    last_object: int | None = None
    layer_index = -1
    filament_object_map: dict[int, int] = {}

    layer_lengths: dict[int, dict[int | None, dict[int, float]]] = {}
    raw_totals: dict[int, float] = {}

    def ensure_bucket(layer: int, object_id: int | None) -> dict[int, float]:
        return layer_lengths.setdefault(layer, {}).setdefault(object_id, {})

    def add_usage(
        filament: int,
        layer: int,
        amount: float,
        object_id: int | None,
    ) -> None:
        if amount <= 0:
            return
        bucket = ensure_bucket(layer, object_id)
        bucket[filament] = bucket.get(filament, 0.0) + amount
        raw_totals[filament] = raw_totals.get(filament, 0.0) + amount

    for line_number, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        if line == CHANGE_LAYER_MARKER:
            layer_index += 1
            continue

        if line.startswith(";"):
            object_match = OBJECT_ID_PATTERN.match(line)
            if object_match:
                current_object, last_object = _set_object_context(
                    int(object_match.group(1)),
                    current_object=current_object,
                    last_object=last_object,
                    filament_object_map=filament_object_map,
                    current_filament=current_filament,
                )
                continue

            start_match = START_OBJECT_PATTERN.match(line)
            if start_match:
                current_object, last_object = _set_object_context(
                    int(start_match.group(1)),
                    current_object=current_object,
                    last_object=last_object,
                    filament_object_map=filament_object_map,
                    current_filament=current_filament,
                )
                continue

            stop_match = STOP_OBJECT_PATTERN.match(line)
            if stop_match:
                last_object = int(stop_match.group(1))
                current_object = None
                continue

            continue

        if line == "M625":
            current_object = None
            continue

        m620_match = M620_PATTERN.match(line)
        if m620_match:
            filament_id = int(m620_match.group(1))
            if filament_id < 256:
                pending_old = current_filament
                pending_new = filament_id
                current_filament = filament_id
            continue

        if include_flush:
            flush_match = M620_10_PATTERN.match(line)
            if (
                flush_match
                and layer_index >= 0
                and pending_old is not None
                and pending_new is not None
                and pending_old != pending_new
            ):
                side = int(flush_match.group(1))
                length = float(flush_match.group(2))
                flush_object = _resolve_flush_object(
                    side,
                    pending_old=pending_old,
                    pending_new=pending_new,
                    last_object=last_object,
                    lines=lines,
                    line_number=line_number,
                    filament_object_map=filament_object_map,
                )
                filament = pending_old if side == 0 else pending_new
                add_usage(filament, layer_index, length, flush_object)
                continue

        if M621_PATTERN.match(line):
            pending_old = None
            pending_new = None
            continue

        target_layer = layer_index
        if target_layer < 0:
            if (
                first_change_layer is not None
                and line_number < first_change_layer
                and current_filament is not None
            ):
                target_layer = 0
            else:
                continue

        if current_filament is None:
            continue

        if not EXTRUSION_PATTERN.match(line):
            continue

        e_match = E_VALUE_PATTERN.search(line)
        if not e_match:
            continue

        extrusion = float(e_match.group(1))
        if extrusion <= 0:
            continue

        if current_object is not None:
            filament_object_map[current_filament] = current_object

        add_usage(current_filament, target_layer, extrusion, current_object)

    plate_layers: list[LayerUsage] = []
    object_layer_data: dict[int | None, dict[int, dict[int, float]]] = {}

    for layer, object_buckets in sorted(layer_lengths.items()):
        plate_filaments: dict[int, float] = {}
        for object_id, filament_amounts in object_buckets.items():
            object_layer_data.setdefault(object_id, {}).setdefault(layer, {})
            for filament, amount in filament_amounts.items():
                plate_filaments[filament] = (
                    plate_filaments.get(filament, 0.0) + amount
                )
                object_layer_data[object_id][layer][filament] = (
                    object_layer_data[object_id][layer].get(filament, 0.0) + amount
                )
        plate_layers.append(LayerUsage(layer=layer, length_mm=plate_filaments))

    object_usage: list[ObjectUsage] = []
    for object_id, layer_map in sorted(
        object_layer_data.items(),
        key=lambda item: (item[0] is None, item[0] or -1),
    ):
        layers = [
            LayerUsage(layer=layer, length_mm=lengths)
            for layer, lengths in sorted(layer_map.items())
        ]
        raw_by_filament: dict[int, float] = {}
        for layer in layers:
            for filament, amount in layer.length_mm.items():
                raw_by_filament[filament] = (
                    raw_by_filament.get(filament, 0.0) + amount
                )
        object_usage.append(
            ObjectUsage(
                identify_id=object_id,
                name=SHARED_OBJECT_NAME if object_id is None else "",
                layers=layers,
                raw_length_mm=raw_by_filament,
            )
        )

    return plate_layers, object_usage, raw_totals


def _compute_calibration_scales(
    references: list[FilamentInfo],
    raw_totals: dict[int, float],
) -> dict[int, float]:
    reference_by_index = {item.index: item for item in references}
    scales: dict[int, float] = {}
    for filament, reference in reference_by_index.items():
        raw_total = raw_totals.get(filament, 0.0)
        if raw_total > 0:
            scales[filament] = reference.length_mm / raw_total
        else:
            scales[filament] = 0.0
    return scales


def _apply_calibration_to_layers(
    layers: list[LayerUsage],
    references: list[FilamentInfo],
    scales: dict[int, float],
) -> None:
    reference_by_index = {item.index: item for item in references}
    for layer in layers:
        layer.mass_g = {}
        calibrated_lengths: dict[int, float] = {}
        for filament, amount in layer.length_mm.items():
            scale = scales.get(filament, 1.0)
            calibrated = amount * scale
            calibrated_lengths[filament] = calibrated
            grams_per_mm = reference_by_index[filament].grams_per_mm
            layer.mass_g[filament] = calibrated * grams_per_mm
        layer.length_mm = calibrated_lengths


def calibrate_usage(
    layers: list[LayerUsage],
    references: list[FilamentInfo],
    *,
    scales: dict[int, float] | None = None,
) -> dict[int, float]:
    if scales is None:
        raw_totals: dict[int, float] = {}
        for layer in layers:
            for filament, amount in layer.length_mm.items():
                raw_totals[filament] = raw_totals.get(filament, 0.0) + amount
        scales = _compute_calibration_scales(references, raw_totals)

    _apply_calibration_to_layers(layers, references, scales)
    return scales


def analyze_plate(
    thmf_path: str | Path,
    plate: int,
    *,
    include_flush: bool = True,
    calibrate: bool = True,
) -> PlateUsage:
    """Analyze filament usage for one plate inside a 3MF archive.

    Args:
        thmf_path: Path to a ``.gcode.3mf`` or ``.3mf`` file.
        plate: 1-based plate number inside the archive.
        include_flush: Count AMS purge volume from ``M620.10 ... L<length>``
            commands during filament changes.
        calibrate: Scale parsed totals per filament to match reference values
            from ``Metadata/slice_info.config`` and derive ``mass_g`` from the
            reference density.

    Returns:
        :class:`PlateUsage` with per-layer totals for the whole plate and for
        each object label id found in the gcode.
    """
    gcode_text = read_plate_gcode(thmf_path, plate)
    slice_info_xml = read_slice_info(thmf_path)
    references = parse_slice_info(
        slice_info_xml,
        plate,
        gcode_header=gcode_text,
    )
    objects = parse_slice_info_objects(slice_info_xml, plate)
    object_names = {item.identify_id: item.name for item in objects}

    layers, object_usage, raw_totals = parse_gcode(
        gcode_text,
        include_flush=include_flush,
    )

    for item in object_usage:
        if item.identify_id is None:
            item.name = SHARED_OBJECT_NAME
        else:
            item.name = object_names.get(item.identify_id, "")

    scales: dict[int, float] | None = None
    if calibrate and references:
        scales = calibrate_usage(layers, references)
        for item in object_usage:
            calibrate_usage(item.layers, references, scales=scales)

    return PlateUsage(
        plate=plate,
        filaments=references,
        objects=objects,
        layers=layers,
        object_usage=object_usage,
        raw_length_mm=raw_totals,
        calibrated=calibrate,
    )


EXTERNAL_SPOOL_TRAY = 255


def decode_mapping_value(value: int) -> int | None:
    """Decode one print.mapping entry to a flat tray index or external spool."""
    if value < 0:
        return None
    if value in (254, 255) or value == 65280:
        return EXTERNAL_SPOOL_TRAY
    if value >= 256:
        ams_id = value >> 8
        tray = value & 0x3
        if ams_id >= 128:
            return ams_id
        return ams_id * 4 + tray
    return value


def decode_mapping_list(values: list) -> list[int | None]:
    return [decode_mapping_value(int(v)) for v in values]


def read_plate_gcode_from_zipfile(archive: zipfile.ZipFile, plate: int) -> str:
    gcode_name = f"Metadata/plate_{plate}.gcode"
    try:
        return archive.read(gcode_name).decode("utf-8")
    except KeyError as exc:
        raise FileNotFoundError(f"G-code for plate {plate} not found in archive") from exc


def read_slice_info_from_zipfile(archive: zipfile.ZipFile) -> str:
    try:
        return archive.read("Metadata/slice_info.config").decode("utf-8")
    except KeyError as exc:
        raise FileNotFoundError("Metadata/slice_info.config not found in archive") from exc


def analyze_plate_from_zipfile(
    archive: zipfile.ZipFile,
    plate: int,
    *,
    include_flush: bool = True,
    calibrate: bool = True,
) -> PlateUsage:
    """Analyze filament usage for one plate inside an open 3MF ZipFile."""
    gcode_text = read_plate_gcode_from_zipfile(archive, plate)
    slice_info_xml = read_slice_info_from_zipfile(archive)
    references = parse_slice_info(
        slice_info_xml,
        plate,
        gcode_header=gcode_text,
    )
    objects = parse_slice_info_objects(slice_info_xml, plate)
    object_names = {item.identify_id: item.name for item in objects}

    layers, object_usage, raw_totals = parse_gcode(
        gcode_text,
        include_flush=include_flush,
    )

    for item in object_usage:
        if item.identify_id is None:
            item.name = SHARED_OBJECT_NAME
        else:
            item.name = object_names.get(item.identify_id, "")

    scales: dict[int, float] | None = None
    if calibrate and references:
        scales = calibrate_usage(layers, references)
        for item in object_usage:
            calibrate_usage(item.layers, references, scales=scales)

    return PlateUsage(
        plate=plate,
        filaments=references,
        objects=objects,
        layers=layers,
        object_usage=object_usage,
        raw_length_mm=raw_totals,
        calibrated=calibrate,
    )

"""Drive the AnycubicSlicerNext CLI (BambuStudio-style) and read its output."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .presets import EXE, PresetStore

MODEL_SUFFIXES = {".stl", ".3mf", ".obj", ".step", ".stp", ".amf"}
TRANSFORMABLE = {".stl", ".obj"}
GCODE_SUFFIXES = {".gcode"}
NOZZLE_VARIANTS = ("_HS", "_BRASS")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# post_process makes the slicer run arbitrary programs after slicing; it is never
# accepted from a tool call or from a 3MF handed to the tools.
SCRIPT_KEYS = {"post_process"}
# Raw G-code templates go to the printer verbatim; opt-in only.
ALLOW_GCODE = os.environ.get("ANYCUBIC_MCP_ALLOW_GCODE_TEMPLATES") == "1"
# Opening \\host\share paths makes Windows send the user's NTLM credentials to that host.
ALLOW_NETWORK = os.environ.get("ANYCUBIC_MCP_ALLOW_NETWORK_PATHS") == "1"
PROJECT_ENTRY_LIMIT = 20 * 1024 * 1024
TRANSFORM_LIMIT = 200 * 1024 * 1024
ACCESS_VIOLATION = -1073741819
HEX_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")
PLACEHOLDER_COLOURS = ("#F2754E", "#1E6FD9", "#3CB043", "#F4D03F", "#8E44AD", "#FFFFFF", "#2C2C2C", "#E91E63")
GCODE_ENTRY = re.compile(r"Metadata/plate_\d+\.gcode(\.md5|\.metadata)?")
# Which bed temperature key the slicer uses for each plate type.
BED_KEYS = {
    "Cool Plate": "cool_plate_temp", "Engineering Plate": "eng_plate_temp", "High Temp Plate": "hot_plate_temp",
    "Textured PEI Plate": "textured_plate_temp", "Textured Cool Plate": "textured_cool_plate_temp",
    "Supertack Plate": "supertack_plate_temp",
}
ESTIMATE_KEYS = {
    "filament used [mm]": "filament_mm",
    "filament used [cm3]": "filament_cm3",
    "filament used [g]": "filament_g",
    "total filament cost": "filament_cost",
    "total layers count": "layers",
    "estimated printing time (normal mode)": "time_normal",
    "estimated printing time (silent mode)": "time_silent",
    "estimated printing time (sport mode)": "time_sport",
    "estimated first layer printing time (normal mode)": "first_layer_time",
    "max_z_height": "max_z_mm",
}
SETTING_KEYS = {
    "filament_type": "filament_type",
    "layer_height": "layer_height",
    "nozzle_temperature": "nozzle_temperature",
    "curr_bed_type": "bed_type",
    "sparse_infill_density": "sparse_infill_density",
    "sparse_infill_pattern": "sparse_infill_pattern",
    "wall_loops": "wall_loops",
    "enable_support": "enable_support",
    "support_type": "support_type",
    "brim_type": "brim_type",
    "printer_settings_id": "printer",
    "print_settings_id": "process",
    "filament_settings_id": "filament",
}
NUMBER = re.compile(r"-?\d+(\.\d+)?")


def _is_remote_drive(path: str) -> bool:
    drive = os.path.splitdrive(path)[0]
    if os.name != "nt" or len(drive) != 2:
        return False
    import ctypes
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE (mapped share)


def _local_path(path: str) -> Path:
    """Absolute local path, decided before anything touches the filesystem: resolving or even
    stat-ing a \\\\host\\share path already makes Windows authenticate to that host."""
    raw = os.path.expanduser(str(path))
    absolute = os.path.abspath(raw)
    if not ALLOW_NETWORK and (raw.replace("/", "\\").startswith("\\\\")
                              or absolute.startswith("\\\\") or _is_remote_drive(absolute)):
        raise ValueError(f"Network/UNC paths are not allowed: {path} "
                         f"(set ANYCUBIC_MCP_ALLOW_NETWORK_PATHS=1 to permit them)")
    return Path(absolute)


def checked_file(path: str, suffixes: set[str]) -> Path:
    """Resolve a tool-supplied path and insist on an existing local file of a known type."""
    src = _local_path(path)
    if not src.is_file():
        raise FileNotFoundError(f"No such file: {src}")
    if src.suffix.lower() not in suffixes:
        raise ValueError(f"Unsupported file type '{src.suffix}' (expected one of {sorted(suffixes)})")
    if src.name.startswith("-"):
        # Passed to the CLI by bare name, where it would read as an option.
        raise ValueError(f"File names starting with '-' are not supported: {src.name}")
    return src


def check_setting_keys(keys) -> None:
    for key in keys:
        if key in SCRIPT_KEYS:
            raise ValueError(f"'{key}' runs external programs and cannot be set through this server")
        if key.endswith("_gcode") and key != "emit_machine_limits_to_gcode" and not ALLOW_GCODE:
            raise ValueError(f"'{key}' is a raw G-code template; set ANYCUBIC_MCP_ALLOW_GCODE_TEMPLATES=1 "
                             f"in the server environment to allow editing it")


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or (data[:200].count(b"\0") > 20):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _json_scripts(node) -> bool:
    """Any post_process key, at any depth and with JSON escapes decoded, that is not empty."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "post_process":
                scripts = value if isinstance(value, list) else [value]
                if any(str(s).strip() for s in scripts):
                    return True
            if _json_scripts(value):
                return True
    elif isinstance(node, list):
        return any(_json_scripts(v) for v in node)
    return False


def _scripts_in(text: str) -> bool:
    """True unless every post_process the text mentions is provably empty."""
    try:
        pairs: list = []
        cfg = json.loads(text, object_pairs_hook=lambda kv: pairs.append(kv) or dict(kv))
    except (ValueError, RecursionError):
        cfg = None
    if cfg is not None:
        # Duplicate keys: the slicer may keep a different copy than json.loads does.
        dup = any(len({k.lower() for k, _ in kv}) != len(kv) for kv in pairs)
        return dup or _json_scripts(cfg)
    lowered = text.lower()
    if "post_process" not in lowered:
        # Escapes are only decoded by a JSON parser, which just rejected this text.
        return bool(re.search(r"\\u00[0-9a-f]{2}", lowered)) and "post" in lowered
    # INI-style lines ("; post_process = ...") or anything we cannot read precisely.
    values = re.findall(r"post_process[ \t]*[=:][ \t]*([^\r\n]*)", text, flags=re.IGNORECASE)
    if text.lower().count("post_process") != len(values):
        return True
    return any(v.strip().strip(",").strip() not in ("", "[]", '""', '"[]"') for v in values)


def check_project_scripts(src: Path) -> None:
    """Refuse 3MF projects that carry (or might carry) post-processing scripts."""
    if src.suffix.lower() != ".3mf":
        return
    try:
        with zipfile.ZipFile(src) as archive:
            for entry in archive.infolist():
                name = entry.filename.lower()
                if entry.is_dir() or not (name.startswith("metadata/") or name.endswith((".config", ".json"))):
                    continue
                if name.endswith((".png", ".jpg", ".jpeg", ".model", ".rels", ".gcode")):
                    continue
                if entry.file_size > PROJECT_ENTRY_LIMIT:
                    raise ValueError(f"{src.name}: settings entry {entry.filename} is too large to check")
                if _scripts_in(_decode(archive.read(entry))):
                    raise ValueError(f"{src.name} contains post-processing scripts ({entry.filename}); "
                                     f"open it in the slicer yourself if you trust it")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{src.name} is not a valid 3MF archive") from exc


def run_cli(args: list[str], cwd: Path, timeout: int = 900) -> tuple[int, str]:
    if not EXE.is_file():
        raise FileNotFoundError(f"Anycubic Slicer Next not found at {EXE}; set ANYCUBIC_SLICER_EXE")
    proc = subprocess.run([str(EXE), *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    code = proc.returncode
    if code > 0x7FFFFFFF:  # Windows reports NTSTATUS codes unsigned
        code -= 1 << 32
    return code, proc.stdout + proc.stderr


def cli_errors(log: str) -> list[str]:
    lines = [ln.split("]", 3)[-1].strip() for ln in log.splitlines() if "[error]" in ln]
    # Harmless in headless mode.
    return [ln for ln in lines if "calc_exclude_triangles" not in ln and "is not there, create it" not in ln]


def model_info(path: str) -> dict:
    src = checked_file(path, MODEL_SUFFIXES)
    check_project_scripts(src)
    code, log = run_cli(["--info", src.name], cwd=src.parent, timeout=300)
    objects: list[dict] = [{}]
    for line in log.splitlines():
        if "=" in line and not line.startswith("["):
            k, v = line.split("=", 1)
            if k.strip() in objects[-1]:  # the CLI prints one block per object
                objects.append({})
            objects[-1][k.strip()] = _number(v.strip())
    if not objects[0]:
        raise RuntimeError(f"--info failed (exit {code}): {cli_errors(log) or log[-800:]}")
    return objects[0] if len(objects) == 1 else {"object_count": len(objects), "objects": objects}


# ---- mesh transforms ---------------------------------------------------
# The CLI's --scale (>1) and --rotate crash it (access violation), so STL/OBJ
# models are transformed here before slicing.
def _transform(points: list[tuple[float, float, float]], scale: float, rotate_z: float):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    z0 = min(p[2] for p in points)
    c, s = math.cos(math.radians(rotate_z)), math.sin(math.radians(rotate_z))

    def turn(p):
        x, y = (p[0] - cx) * scale, (p[1] - cy) * scale
        return x * c - y * s, x * s + y * c, z0 + (p[2] - z0) * scale
    # Keep the footprint's lower-left corner where it was, so arrange=false stays on the bed.
    turned = [turn(p) for p in points]
    dx, dy = min(xs) - min(t[0] for t in turned), min(ys) - min(t[1] for t in turned)

    def apply(p):
        x, y, z = turn(p)
        return x + dx, y + dy, z
    return apply, (c, s)


def transform_model(src: Path, dst: Path, scale: float, rotate_z: float) -> None:
    suffix = src.suffix.lower()
    if src.stat().st_size > TRANSFORM_LIMIT:
        raise ValueError(f"{src.name} is too large to scale/rotate here (limit 200 MB)")
    data = src.read_bytes()
    if suffix == ".obj":
        text = data.decode("utf-8", errors="replace").splitlines()
        verts = [tuple(map(float, ln.split()[1:4])) for ln in text if ln.startswith("v ")]
        apply, _ = _transform(verts, scale, rotate_z)
        out = []
        for ln in text:
            if ln.startswith("v "):
                parts = ln.split()
                x, y, z = apply(tuple(map(float, parts[1:4])))
                ln = " ".join(["v", f"{x:.6f}", f"{y:.6f}", f"{z:.6f}", *parts[4:]])
            out.append(ln)
        dst.write_bytes(("\n".join(out) + "\n").encode("utf-8"))
        return
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if len(data) == 84 + 50 * count:  # binary STL
        tris = [struct.unpack_from("<12f", data, 84 + 50 * i) for i in range(count)]
        apply, (c, s) = _transform([t[j:j + 3] for t in tris for j in (3, 6, 9)], scale, rotate_z)
        buf = bytearray(data[:84])
        for i, t in enumerate(tris):
            n = (t[0] * c - t[1] * s, t[0] * s + t[1] * c, t[2])
            pts = [apply(t[j:j + 3]) for j in (3, 6, 9)]
            buf += struct.pack("<12f", *n, *pts[0], *pts[1], *pts[2]) + data[84 + 50 * i + 48:84 + 50 * i + 50]
        dst.write_bytes(bytes(buf))
        return
    text = data.decode("utf-8", errors="replace")  # ASCII STL
    vertex = re.compile(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)")
    points = [tuple(map(float, m)) for m in vertex.findall(text)]
    if not points:
        raise ValueError(f"{src.name} has no vertices")
    apply, _ = _transform(points, scale, rotate_z)
    text = vertex.sub(lambda m: "vertex %.6f %.6f %.6f" % apply(tuple(map(float, m.groups()))), text)
    # Normals are recomputed by the slicer when they don't match the facets.
    dst.write_bytes(text.encode("utf-8"))  # keep the file's own line endings


# ---- G-code reading ----------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")


def _remaining_at(marks: list[tuple[int, float]], line_no: int) -> float | None:
    """Normal-mode remaining seconds at a line, interpolated between M73 marks."""
    before = [m for m in marks if m[0] <= line_no]
    after = [m for m in marks if m[0] > line_no]
    if not before or not after:
        return None
    (l0, r0), (l1, r1) = before[-1], after[0]
    return r0 + (r1 - r0) * (line_no - l0) / (l1 - l0)


def _number(value: str):
    if NUMBER.fullmatch(value):
        return float(value) if "." in value else int(value)
    return value


def _clean(value: str):
    """'"A";"B"' -> 'A, B'; numbers become numbers."""
    value = value.strip()
    if '";"' in value:
        value = ", ".join(v.strip('"') for v in value.split('";"'))
    return _number(value.strip('"'))


def read_config_block(path: str | Path) -> dict[str, str]:
    """The '; key = value' settings the slicer appends between CONFIG_BLOCK markers."""
    cfg: dict[str, str] = {}
    inside = False
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("; CONFIG_BLOCK_START"):
                inside = True
            elif line.startswith("; CONFIG_BLOCK_END"):
                break
            elif inside and line.startswith(";") and "=" in line:
                key, value = line[1:].split("=", 1)
                cfg[key.strip()] = value.strip()
    return cfg


def gcode_stats(path: str | Path) -> dict:
    """Estimates, key settings and the real bed temperature of a sliced file."""
    path = Path(path)
    stats: dict = {}
    marks: list[tuple[int, float]] = []
    layer_starts: list[int] = []
    bed = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh):
            if line.startswith("M73"):
                m = re.search(r"NormalR([\d.]+)s", line)
                if m:
                    marks.append((line_no, float(m.group(1))))
                continue
            if bed is None and line.startswith(("M140", "M190", "G9111")):
                m = re.search(r"(?:\bS|bedTemp=)(\d+(?:\.\d+)?)", line)
                if m and float(m.group(1)) > 0:
                    bed = _number(m.group(1))
            if line.startswith(";LAYER_CHANGE") and len(layer_starts) < 2:
                layer_starts.append(line_no)
            if line.startswith("; CONFIG_BLOCK_START"):
                break
            if not line.startswith(";"):
                continue
            body = line[1:].strip()
            sep = "=" if "=" in body else (":" if ":" in body else None)
            if not sep:
                continue
            key, value = (s.strip() for s in body.split(sep, 1))
            if key in ESTIMATE_KEYS and ESTIMATE_KEYS[key] not in stats:
                stats[ESTIMATE_KEYS[key]] = _clean(value)
    config = read_config_block(path)
    for key, name in SETTING_KEYS.items():
        if key in config:
            stats[name] = _clean(config[key])
    bed_key = BED_KEYS.get(config.get("curr_bed_type", ""))
    if bed is not None:
        stats["bed_temperature"] = bed
    elif bed_key and bed_key in config:
        stats["bed_temperature"] = _clean(config[bed_key].split(",")[0])
    # The header's first-layer estimate is far too low (5s for a 118 mm brim), so
    # measure layer 1 from the M73 remaining-time marks when the file has them.
    stats["first_layer_time_source"] = "header (often underestimated)"
    if len(layer_starts) == 2:
        start, end = (_remaining_at(marks, n) for n in layer_starts)
        if start is not None and end is not None:
            stats["first_layer_time_header"] = stats.get("first_layer_time")
            stats["first_layer_time"] = _fmt_duration(start - end)
            stats["first_layer_time_source"] = "M73 progress marks"
    return stats


# ---- settings ------------------------------------------------------------
def set_value(cfg: dict, key: str, value) -> None:
    """Store a value the way preset JSON does: strings, lists mirrored per extruder/nozzle."""
    multiline = ALLOW_GCODE and key.endswith("_gcode")

    def text(v) -> str:
        if v is None or isinstance(v, (dict, list)):
            raise ValueError(f"Invalid value for '{key}': {v!r}")
        s = ("1" if v else "0") if isinstance(v, bool) else str(v)
        # A newline in a value that a G-code template expands would become a raw G-code line.
        if not multiline and any(ord(ch) < 32 and ch != "\t" for ch in s):
            raise ValueError(f"Value for '{key}' may not contain line breaks or control characters")
        return s
    current = cfg.get(key)
    if isinstance(value, list):
        cfg[key] = [text(v) for v in value]
    elif isinstance(current, list):
        cfg[key] = [text(value)] * max(1, len(current))
    else:
        cfg[key] = text(value)


def variant_keys(cfg: dict, key: str, given) -> list[str]:
    """Per-nozzle copies of a key (nozzle_temperature_HS...) that would otherwise win over it."""
    return [key + v for v in NOZZLE_VARIANTS if key + v in cfg and key + v not in given]


def apply_overrides(configs: dict[str, dict], overrides: dict, extra_filaments: list[dict] = ()) -> dict[str, str]:
    """Route each override to the config that owns the key; filament keys hit every loaded filament."""
    check_setting_keys(overrides)
    unknown = [k for k in overrides if not any(k in configs[c] for c in ("process", "filament", "machine"))]
    if unknown:
        raise ValueError(f"Unknown setting keys {unknown}; look them up with get_preset(search=...)")
    placed = {}
    for key, value in overrides.items():
        target = next(k for k in ("process", "filament", "machine") if key in configs[k])
        cfgs = [configs[target], *(extra_filaments if target == "filament" else ())]
        for k in [key, *variant_keys(configs[target], key, overrides)]:
            placed[k] = target
            for cfg in cfgs:
                if k in cfg:
                    set_value(cfg, k, value)
    return placed


def _same(requested, actual: str) -> bool:
    probe: dict = {}
    set_value(probe, "k", requested)
    wanted = probe["k"] if isinstance(probe["k"], list) else [probe["k"]]
    got = [a.strip().strip('"') for a in actual.split(",")]
    if len(wanted) == 1:
        wanted = wanted * len(got)
    for w, g in zip(wanted, got):
        try:
            if abs(float(w.rstrip("%")) - float(g.rstrip("%"))) > 1e-6:
                return False
        except ValueError:
            if w.lower() != g.lower():
                return False
    return True


def override_warnings(overrides: dict, placed: dict, gcode: Path) -> list[str]:
    """Overrides that did not make it into the G-code (clamped, invalid or shadowed)."""
    config = read_config_block(gcode)
    warnings = []
    for key, value in overrides.items():
        if key in config and not _same(value, config[key]):
            warnings.append(f"{key}: requested {value!r} but the G-code has {config[key]!r}")
    bed_key = BED_KEYS.get(config.get("curr_bed_type", ""))
    for key in overrides:
        if key.endswith("plate_temp") and bed_key and key != bed_key:
            warnings.append(f"{key} has no effect on a {config['curr_bed_type']}; set {bed_key} instead")
    return warnings


# ---- slicing ---------------------------------------------------------------
def set_colours(filament_cfgs: list[dict], colours: list[str]) -> list[str]:
    """Give every loaded filament its own colour; without one, all of them end up in a
    one-element colour list and the GUI and printer treat the job as single-filament."""
    for i, cfg in enumerate(filament_cfgs):
        preset = cfg.get("default_filament_colour")
        preset = preset[0] if isinstance(preset, list) and preset else preset
        if i < len(colours):
            colour = colours[i]
        elif len(filament_cfgs) == 1 and isinstance(preset, str) and HEX_COLOUR.fullmatch(preset):
            colour = preset
        else:
            colour = PLACEHOLDER_COLOURS[i % len(PLACEHOLDER_COLOURS)]
        cfg["filament_colour"] = [colour]
        cfg["default_filament_colour"] = [colour]
    if len(filament_cfgs) > 1 and len(colours) < len(filament_cfgs):
        return ["filament_colours not given for every filament; placeholder colours were used. Pass the "
                "colours of the loaded spools so the printer maps each filament to the right one."]
    return []


def _rewrite_zip(src: Path, dst: Path, edit) -> None:
    """Copy a 3MF entry by entry; edit(name, data) returns new bytes, or None to drop the entry."""
    partial = dst.with_name(dst.name + ".tmp")
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zout:
        for entry in zin.infolist():
            data = edit(entry.filename, zin.read(entry))
            if data is not None:
                zout.writestr(entry.filename, data)
    os.replace(partial, dst)


def assign_extruders(xml: bytes, by_name: dict[str, int]) -> bytes:
    """Set the extruder of each object in Metadata/model_settings.config by its name."""
    root = ElementTree.fromstring(xml)
    found = set()
    for obj in root.iter("object"):
        meta = {m.get("key"): m for m in obj.findall("metadata")}
        name = meta.get("name")
        if name is None or name.get("value") not in by_name:
            continue
        ext = meta.get("extruder")
        if ext is None:
            ext = ElementTree.SubElement(obj, "metadata", key="extruder")
        ext.set("value", str(by_name[name.get("value")]))
        found.add(name.get("value"))
    missing = sorted(set(by_name) - found)
    if missing:
        raise RuntimeError(f"Could not find {missing} in the laid-out project")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root, encoding="utf-8")


def _stage_objects(work: Path, settings: list[str], layout: list[str], local: list[str],
                   object_filaments: list[int], timeout: int) -> str:
    (work / "stage").mkdir()
    code, log = run_cli(["--outputdir", "stage", *settings, "--export-3mf", "stage.3mf", *layout, *local],
                        cwd=work, timeout=timeout)
    laid_out = work / "stage" / "stage.3mf"
    if code != 0 or not laid_out.exists():
        raise RuntimeError(json.dumps({"exit_code": code, "stage": "layout",
                                       "errors": cli_errors(log) or log[-1500:]}, ensure_ascii=False))
    by_name = dict(zip(local, object_filaments))
    _rewrite_zip(laid_out, work / "staged.3mf", lambda name, data: assign_extruders(data, by_name)
                 if name == "Metadata/model_settings.config" else data)
    return "staged.3mf"


def mark_changed_settings(project: Path, placed: dict[str, str], filament_count: int) -> None:
    """Record overridden keys in different_settings_to_system. The GUI loads the named presets
    and only takes the keys listed there from the project, so overrides vanish otherwise."""
    order = ["process", *(["filament"] * filament_count), "machine"]

    def edit(name: str, data: bytes):
        if name != "Metadata/project_settings.config":
            return data
        cfg = json.loads(data.decode("utf-8-sig"))
        current = cfg.get("different_settings_to_system")
        if not isinstance(current, list) or len(current) != len(order):
            current = [""] * len(order)
        cfg["different_settings_to_system"] = [
            ";".join(sorted(set(filter(None, str(old).split(";"))) | {k for k, t in placed.items() if t == kind}))
            for old, kind in zip(current, order)]
        colours = cfg.get("filament_colour")
        if isinstance(colours, list) and len(colours) == filament_count:
            cfg["filament_colour_info"] = list(colours)
        return json.dumps(cfg, indent=4, ensure_ascii=False).encode("utf-8")
    _rewrite_zip(project, project, edit)


def strip_gcode(project: Path, editable: Path) -> None:
    """A copy without the sliced G-code, which the GUI opens as an editable project
    instead of a G-code preview."""
    def edit(name: str, data: bytes):
        if GCODE_ENTRY.fullmatch(name) or name == "Metadata/slice_info.config":
            return None
        if name == "Metadata/_rels/model_settings.config.rels":
            return re.sub(rb"\s*<Relationship [^>]*\.gcode\"[^>]*/>", b"", data)
        return data
    _rewrite_zip(project, editable, edit)


def _output_stem(output_name: str | None, sources: list[Path]) -> str:
    stem = output_name or (sources[0].stem if len(sources) == 1 else "plate")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    return re.sub(r"(\.gcode)+$", "", stem, flags=re.IGNORECASE).strip(" .") or "plate"


def temperature_warnings(filament: dict, overrides: dict) -> list[str]:
    """Nozzle temperatures outside the filament's own recommended range."""
    try:
        low = float(str(filament.get("nozzle_temperature_range_low", ["0"])[0]))
        high = float(str(filament.get("nozzle_temperature_range_high", ["0"])[0]))
    except (TypeError, ValueError, IndexError):
        return []
    out = []
    for key, value in overrides.items():
        if key.startswith("nozzle_temperature") and "range" not in key and high > 0:
            try:
                temp = float(value[0] if isinstance(value, list) else value)
            except (TypeError, ValueError, IndexError):
                continue
            if not low <= temp <= high:
                out.append(f"{key}={temp:g} is outside this filament's range {low:g}-{high:g} °C")
    return out


def _publish(src: Path, target: Path) -> None:
    """Copy next to the target, then swap it in, so readers never see a half-written file."""
    partial = target.with_name(f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.part")
    shutil.copy2(src, partial)
    os.replace(partial, target)


def slice_models(store: PresetStore, models: list[str], printer: str | None, process: str | None,
                 filaments: list[str] | None, overrides: dict | None, output_dir: str | None,
                 output_name: str | None, plate: int, arrange: bool, orient: bool,
                 scale: float | None, rotate_z: float | None, copies: int | None,
                 use_project_settings: bool, timeout: int, object_filaments: list[int] | None = None,
                 filament_colours: list[str] | None = None) -> dict:
    if not models:
        raise ValueError("Give at least one model file")
    sources = [checked_file(m, MODEL_SUFFIXES) for m in models]
    names = [s.name.lower() for s in sources]
    if len(set(names)) != len(names):
        raise ValueError("Model file names must be unique (they are copied into one folder)")
    for src in sources:
        check_project_scripts(src)
    if use_project_settings:
        if overrides or printer or process or filaments or object_filaments or filament_colours:
            raise ValueError("use_project_settings uses the 3MF's own settings; don't pass printer/process/"
                             "filaments/overrides/object_filaments/filament_colours with it")
        if sources[0].suffix.lower() != ".3mf":
            raise ValueError("use_project_settings needs a .3mf project as the first model")
    if object_filaments is not None:
        if len(object_filaments) != len(sources):
            raise ValueError("object_filaments needs one filament number per model, in the same order")
        if any(s.suffix.lower() == ".3mf" for s in sources):
            raise ValueError("object_filaments works with STL/OBJ/STEP/AMF models, not 3MF projects")
    for colour in filament_colours or []:
        if not isinstance(colour, str) or not HEX_COLOUR.fullmatch(colour):
            raise ValueError(f"Filament colours must look like '#1E6FD9', got {colour!r}")
    transform = (scale not in (None, 1)) or bool(rotate_z)
    if transform:
        odd = [s.name for s in sources if s.suffix.lower() not in TRANSFORMABLE]
        if odd:
            raise ValueError(f"scale/rotate_z are supported for STL and OBJ only, not {odd}")
    if copies and copies > 1 and not arrange:
        raise ValueError("copies needs arrange=true; otherwise the copies overlap")
    dest = _local_path(output_dir) if output_dir else sources[0].parent
    stem = _output_stem(output_name, sources)
    outputs = {(dest / f"{stem}{ext}").as_posix().lower() for ext in (".gcode", ".gcode.3mf", "_project.3mf")}
    clash = [s.name for s in sources if s.as_posix().lower() in outputs]
    if clash:
        raise ValueError(f"The output would overwrite the input {clash}; pass output_name or output_dir")

    work = Path(tempfile.mkdtemp(prefix="acs_"))
    try:
        return _slice_in(work, store, sources, printer, process, filaments, overrides or {}, dest,
                         output_name, plate, arrange, orient, scale or 1, rotate_z or 0, copies,
                         use_project_settings, timeout, object_filaments, filament_colours)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _slice_in(work: Path, store: PresetStore, sources: list[Path], printer, process, filaments,
              overrides: dict, dest: Path, output_name, plate, arrange, orient, scale, rotate_z,
              copies, use_project_settings, timeout, object_filaments=None, filament_colours=None) -> dict:
    # Copy inputs so object names inside the gcode/3mf are clean file names.
    local = []
    for src in sources:
        if scale != 1 or rotate_z:
            transform_model(src, work / src.name, scale, rotate_z)
        else:
            shutil.copy2(src, work / src.name)
        local.append(src.name)

    args = ["--slice", str(plate), "--outputdir", "out"]
    resolved: dict = {}
    placed: dict = {}
    warnings: list[str] = []
    if not use_project_settings:
        printer = printer or store.current_selection().get("machine")
        if not printer:
            raise ValueError("No printer given and none selected in the GUI")
        printer = store.get("machine", printer).name
        defaults = None if (process and filaments) else store.defaults_for(printer)
        process = store.get("process", process or defaults["process"], printer).name
        filaments = [store.get("filament", f, printer).name for f in (filaments or defaults["filaments"])]
        for kind, name in [("process", process), *(("filament", f) for f in filaments)]:
            if not store.is_compatible(kind, name, printer):
                warnings.append(f"{kind} '{name}' is not made for '{printer}'; used anyway")

        system_printer = store.system_root_name("machine", printer)
        configs = {"machine": store.resolve("machine", printer),
                   "process": store.resolve("process", process),
                   "filament": None}
        # The CLI matches process/filament compatible_printers against the
        # machine's system parent, read from the machine's "inherits".
        if configs["machine"]["name"] != system_printer:
            configs["machine"]["inherits"] = system_printer
        compat = sorted({system_printer, printer})
        configs["process"]["compatible_printers"] = compat
        configs["process"]["compatible_printers_condition"] = ""
        filament_cfgs = []
        for f in filaments:
            cfg = store.resolve("filament", f)
            cfg["compatible_printers"] = compat
            cfg["compatible_printers_condition"] = ""
            cfg["compatible_prints"] = []
            cfg["compatible_prints_condition"] = ""
            filament_cfgs.append(cfg)
        configs["filament"] = filament_cfgs[0]
        placed = apply_overrides(configs, overrides, filament_cfgs[1:])
        warnings += temperature_warnings(store.resolve("filament", filaments[0]), overrides)
        if object_filaments and any(not 1 <= n <= len(filaments) for n in object_filaments):
            raise ValueError(f"object_filaments values must be between 1 and {len(filaments)} (the filaments given)")
        if filament_colours and len(filament_colours) > len(filaments):
            raise ValueError("More filament_colours than filaments")
        warnings += set_colours(filament_cfgs, filament_colours or [])

        (work / "machine.json").write_text(json.dumps(configs["machine"], indent=1), encoding="utf-8")
        (work / "process.json").write_text(json.dumps(configs["process"], indent=1), encoding="utf-8")
        fil_files = []
        for i, cfg in enumerate(filament_cfgs):
            name = f"filament_{i}.json"
            (work / name).write_text(json.dumps(cfg, indent=1), encoding="utf-8")
            fil_files.append(name)
        settings = ["--load-settings", "machine.json;process.json", "--load-filaments", ";".join(fil_files)]
        args += settings
        resolved = {"printer": printer, "process": process, "filaments": filaments}

    stem = _output_stem(output_name, sources)
    layout = []
    if rotate_z:
        layout.append("--allow-rotations=0")  # otherwise arrange turns the part back
    layout += ["--arrange", "1" if arrange else "0", "--orient", "1" if orient else "0", "--ensure-on-bed"]
    if copies and copies > 1:
        # --repetitions is rejected with --slice 0; clone count is the total per object.
        layout += ["--clone-objects", ",".join([str(copies)] * len(local))]

    started = time.time()
    try:
        if object_filaments:
            # The CLI has no per-object filament option: lay the plate out into a project first,
            # set each object's extruder in it, then slice that project with the same settings.
            staged = _stage_objects(work, settings, layout, local, object_filaments, timeout)
            args += ["--export-3mf", f"{stem}.gcode.3mf", "--arrange", "0", "--orient", "0", staged]
        else:
            args += ["--export-3mf", f"{stem}.gcode.3mf", *layout, *local]
        code, log = run_cli(args, cwd=work, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Slicing took longer than {timeout}s; raise timeout_s if the model is large") from exc
    elapsed = round(time.time() - started, 1)
    out = work / "out"
    gcodes = sorted(out.glob("plate_*.gcode")) if out.exists() else []
    if code != 0 or not gcodes:
        report = {"exit_code": code, "errors": cli_errors(log) or log[-1500:]}
        if code == ACCESS_VIOLATION:
            report["hint"] = "The slicer crashed (access violation) while processing these models/settings."
        elif plate:
            report["hint"] = f"Check that plate {plate} exists; plate=0 slices all plates."
        elif not arrange:
            report["hint"] = "With arrange=false models keep their file coordinates and may be off the bed."
        raise RuntimeError(json.dumps(report, ensure_ascii=False))

    dest.mkdir(parents=True, exist_ok=True)
    plates = []
    for g in gcodes:
        suffix = "" if len(gcodes) == 1 else "_" + g.stem.split("_")[-1]
        target = dest / f"{stem}{suffix}.gcode"
        # Stats come from our own copy, even if a concurrent slice replaces the target.
        plates.append({"gcode": str(target), **gcode_stats(g)})
        _publish(g, target)
    project = out / f"{stem}.gcode.3mf"
    result = {"ok": True, "elapsed_s": elapsed, **resolved, "plates": plates}
    if project.exists():
        if not use_project_settings:
            mark_changed_settings(project, placed, len(filaments))
        editable = work / f"{stem}_project.3mf"
        strip_gcode(project, editable)
        target = dest / project.name
        _publish(project, target)
        result["project_3mf"] = str(target)
        target = dest / editable.name
        _publish(editable, target)
        result["editable_3mf"] = str(target)
    if object_filaments:
        result["object_filaments"] = {name: n for name, n in zip(local, object_filaments)}
    if placed:
        result["overrides_applied"] = placed
        warnings += override_warnings(overrides, placed, gcodes[0])
    if warnings:
        result["warnings"] = warnings
    return result

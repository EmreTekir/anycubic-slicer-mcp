"""MCP server exposing Anycubic Slicer Next (headless CLI) to agents."""
from __future__ import annotations

import functools
import json
import re
import subprocess

import anyio
from mcp.server.fastmcp import FastMCP, Image

from . import __version__, preview, slicer
from .presets import DATA, EXE, KINDS, PresetStore

mcp = FastMCP(
    "anycubic-slicer",
    instructions=(
        "Headless Anycubic Slicer Next. Typical flow: slicer_status -> list_presets "
        "(process/filament filtered by printer) -> slice_model -> preview_gcode to check the result. "
        "Omitted printer/process/filament fall back to what is selected in the GUI. "
        "Use get_preset with a search term to discover setting keys before passing overrides "
        "(e.g. sparse_infill_density='20%', enable_support=true, support_type='tree(auto)', "
        "brim_type='outer_only', wall_loops=3, layer_height=0.16, nozzle_temperature=215). "
        "Read the 'warnings' in slice results: they flag overrides the slicer clamped or ignored."
    ),
)
mcp._mcp_server.version = __version__  # otherwise the mcp library's own version is reported
store = PresetStore()

# Windows rejects these file names regardless of extension.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
PRESET_NAME = re.compile(r'[^<>:"/\\|?*\x00-\x1f]{1,120}')
# Identity and inheritance fields of a preset file; settings may not touch them.
META_KEYS = {"name", "inherits", "from", "type", "instantiation", "is_custom_defined", "setting_id",
             "version", "printer_settings_id", "print_settings_id", "filament_settings_id",
             "compatible_printers", "compatible_printers_condition", "compatible_prints",
             "compatible_prints_condition", "base_id", "filament_id", "user_id"}


def threaded(fn):
    """Run a blocking tool in a worker thread so slicing doesn't stall the MCP session."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return wrapper


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    return kind


def _in_range(name: str, value, low, high):
    if value is not None and not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@mcp.tool()
@threaded
def slicer_status() -> dict:
    """Slicer install, version, active user preset folder and the GUI's current printer/process/filament."""
    store.reload()
    version = None
    if EXE.is_file():
        try:
            out = subprocess.run([str(EXE), "--help"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                                 creationflags=slicer.NO_WINDOW).stdout
            version = next((ln.rstrip(":") for ln in out.splitlines() if ln.startswith("AnycubicSlicerNext-")),
                           None)
        except (OSError, subprocess.SubprocessError):
            pass
    # The GUI build prints nothing for --help; the app config records the last version that ran.
    version = version or (store.conf.get("app") or {}).get("version")
    return {
        "server_version": __version__,
        "exe": str(EXE), "exe_exists": EXE.is_file(), "version": version, "data_dir": str(DATA),
        "user_preset_folder": store.user_folder,
        "preset_counts": {k: sum(p.visible for p in store.presets[k].values()) for k in KINDS},
        "gui_selection": store.current_selection(),
    }


@mcp.tool()
@threaded
def list_presets(kind: str, printer: str | None = None, search: str | None = None,
                 include_incompatible: bool = False) -> dict:
    """List selectable presets. kind: machine | process | filament.
    For process/filament pass printer to keep only compatible ones (user presets listed first).
    search is a case-insensitive substring filter (e.g. 'S1', 'PLA', '0.16mm')."""
    store.reload()
    if printer:
        printer = store.get("machine", printer).name
    presets = store.list(_check_kind(kind), printer, search, include_incompatible)
    return {"count": len(presets), "presets": presets}


@mcp.tool()
@threaded
def get_preset(name: str, kind: str, search: str | None = None, keys: list[str] | None = None,
               printer: str | None = None) -> dict:
    """Fully resolved (inheritance-merged) preset values. Name may be a unique substring;
    printer narrows ambiguous process/filament names.
    Use search to filter keys by substring (e.g. 'infill', 'support', 'temp', 'speed') or keys for exact ones;
    with neither, long G-code template values are truncated."""
    store.reload()
    preset = store.get(_check_kind(kind), name, printer)
    cfg = store.resolve(kind, preset.name)
    if keys:
        return {k: cfg.get(k) for k in keys}
    if search:
        return {k: v for k, v in cfg.items() if search.lower() in k.lower()}
    trimmed = {}
    for k, v in cfg.items():
        text = json.dumps(v)
        trimmed[k] = v if len(text) <= 200 else f"<{len(text)} chars; request via keys>"
    return trimmed


@mcp.tool()
@threaded
def model_info(path: str) -> dict:
    """Size (mm), bounding box, facet count, volume (mm3) and manifold check for a model file."""
    return slicer.model_info(path)


@mcp.tool()
@threaded
def slice_model(models: list[str], printer: str | None = None, process: str | None = None,
                filaments: list[str] | None = None, overrides: dict | None = None,
                output_dir: str | None = None, output_name: str | None = None, plate: int = 0,
                arrange: bool = True, orient: bool = False, scale: float | None = None,
                rotate_z: float | None = None, copies: int | None = None,
                use_project_settings: bool = False, timeout_s: int = 900,
                object_filaments: list[int] | None = None, filament_colours: list[str] | None = None) -> dict:
    """Slice STL/3MF/OBJ/STEP files to G-code, a .gcode.3mf (G-code inside, sendable to the printer)
    and a _project.3mf without G-code that the GUI opens as an editable project.

    printer/process/filaments: preset names or unique substrings; omitted -> GUI selection for that printer.
    object_filaments: for multi-colour prints, the 1-based filament number of each model, in models order
    (e.g. models=[body.stl, logo.stl], filaments=[white PLA, blue PLA], object_filaments=[1, 2]).
    filament_colours: '#RRGGBB' per filament; give the loaded spools' colours so the printer maps them.
    overrides: setting key -> value, routed automatically to process/filament/machine
    (per-nozzle variants like nozzle_temperature_HS/_BRASS follow the plain key).
    output_dir defaults to the first model's folder; existing files with the same name are replaced.
    plate: 0 = all plates. arrange/orient: auto-place and auto-orient (arrange=false keeps the file's
    coordinates). scale / rotate_z (degrees): STL and OBJ only. copies: total instances of each model.
    use_project_settings: slice a 3MF with the settings stored inside it (no presets/overrides).
    Returns gcode paths with print time, filament grams/meters, layer count, key settings and warnings."""
    _in_range("plate", plate, 0, 999)
    _in_range("scale", scale, 0.01, 100)
    _in_range("rotate_z", rotate_z, -360, 360)
    _in_range("copies", copies, 1, 500)
    _in_range("timeout_s", timeout_s, 10, 7200)
    store.reload()
    return slicer.slice_models(store, models, printer, process, filaments, overrides, output_dir,
                               output_name, plate, arrange, orient, scale, rotate_z, copies,
                               use_project_settings, timeout_s, object_filaments, filament_colours)


@mcp.tool()
@threaded
def gcode_summary(path: str) -> dict:
    """Print-time / filament / layer estimates and key settings read from an existing .gcode file."""
    return slicer.gcode_stats(slicer.checked_file(path, slicer.GCODE_SUFFIXES))


@mcp.tool()
@threaded
def preview_gcode(path: str, layer: int | None = None, size: int = 900) -> list:
    """Top-down PNG render of a .gcode. layer=None: all layers shaded by height (footprint/placement check).
    layer=N (1-based): that layer colored by feature: outer wall orange, inner wall yellow, sparse infill
    dark red, solid infill magenta, top surface red, bottom surface blue, bridge light blue,
    overhang wall dark blue, support green, brim/skirt cyan, gap fill white. size: image edge in pixels (200-2000)."""
    _in_range("size", size, 200, 2000)
    png, meta = preview.render(str(slicer.checked_file(path, slicer.GCODE_SUFFIXES)), layer, size)
    return [Image(data=png, format="png"), json.dumps(meta)]


@mcp.tool()
@threaded
def open_in_slicer(path: str) -> str:
    """Open a model/3MF/G-code in the Anycubic Slicer Next GUI. Only call this when the user asks to see it."""
    target = slicer.checked_file(path, slicer.MODEL_SUFFIXES | slicer.GCODE_SUFFIXES)
    slicer.check_project_scripts(target)
    if not EXE.is_file():
        raise FileNotFoundError(f"Anycubic Slicer Next not found at {EXE}; set ANYCUBIC_SLICER_EXE")
    # Detach from our stdio: the MCP protocol runs over stdout.
    subprocess.Popen([str(EXE), str(target)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    return f"Opened {target.name} in Anycubic Slicer Next"


@mcp.tool()
@threaded
def save_user_preset(kind: str, name: str, inherits: str, settings: dict) -> dict:
    """Create/overwrite a user preset that shows up in the GUI (restart the slicer to see it).
    inherits: parent preset name; settings: only the keys that differ from the parent."""
    _check_kind(kind)
    store.reload()
    if (not PRESET_NAME.fullmatch(name) or name != name.strip(" .")
            or name.split(".")[0].strip().upper() in RESERVED_NAMES):
        raise ValueError("Preset name must be 1-120 characters without <>:\"/\\|?* and "
                         "must not start/end with a space or dot")
    parent = store.get(kind, inherits)
    existing = store.presets[kind].get(name)
    if name == parent.name or (existing and existing.source == "system"):
        raise ValueError("Name collides with a system preset; choose a new name")
    if not settings:
        raise ValueError("settings is empty; give the keys that differ from the parent")
    meta = sorted(set(settings) & META_KEYS)
    if meta:
        raise ValueError(f"{meta} describe the preset itself and cannot be set; use name/inherits instead")
    parent_cfg = store.resolve(kind, parent.name)
    slicer.check_setting_keys(settings)
    unknown = [k for k in settings if k not in parent_cfg and not slicer.slicer_knows(k)]
    if unknown:
        raise ValueError(f"Unknown setting keys {unknown}; look them up with get_preset(search=...)")
    data: dict = {"from": "User", "inherits": parent.name, "is_custom_defined": "0", "name": name,
                  "version": parent_cfg.get("version", "")}
    id_key = {"machine": "printer_settings_id", "process": "print_settings_id",
              "filament": "filament_settings_id"}[kind]
    data[id_key] = [name] if kind == "filament" else name
    for key, value in settings.items():
        for k in [key, *slicer.variant_keys(parent_cfg, key, settings)]:
            data[k] = parent_cfg.get(k)
            slicer.set_value(data, k, value)
    folder = store.user_root / kind
    path = folder / f"{name}.json"
    if path.resolve().parent != folder.resolve():
        raise ValueError("Preset name resolves outside the user preset folder")
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    info = folder / f"{name}.info"
    if not info.exists():
        base_id = parent_cfg.get("setting_id", "")
        info.write_text(f"sync_info = create\nuser_id = \nsetting_id = \nbase_id = {base_id}\n"
                        f"updated_time = 0\n", encoding="utf-8")
    store.reload()
    return {"saved": str(path), "preset": data}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

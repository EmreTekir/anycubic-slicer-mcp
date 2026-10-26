# anycubic-slicer-mcp

[![tests](https://github.com/EmreTekir/anycubic-slicer-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/EmreTekir/anycubic-slicer-mcp/actions/workflows/tests.yml)

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Code, Claude Desktop, Cursor, …)
use **Anycubic Slicer Next** without its GUI: list presets, slice models, read print time and filament
estimates, and look at a rendered preview of the toolpaths.

> Unofficial community project. Not affiliated with or endorsed by Anycubic.

```
You:    Slice examples/calibration_cube.stl for my Kobra S1 with 15% infill.
Agent:  slice_model → 20m 53s, 3.56 g PLA, 100 layers, first layer 24s
        preview_gcode(layer=1) → [PNG of the first layer, colored by feature]
```

`examples/calibration_cube.stl` is a plain 20 mm cube (regenerate it with `python examples/make_cube.py`).

## Requirements

- Windows (the slicer's headless CLI and preset folders are used as installed by Anycubic)
- [Anycubic Slicer Next](https://wiki.anycubic.com/en/software-and-app/new-page-anycubic-slicer-beta(orca-version)) installed and opened at least once
- Python 3.10+

Tested with Anycubic Slicer Next 2.0.0.3 on Kobra S1, S1 Max, Kobra 3 / 3 Max, Kobra 4, Kobra X,
Kobra 2 Neo / 2 Pro and Kobra 1 presets. Presets are read from the slicer itself, so other Anycubic
printers should work too.

## Install

```powershell
git clone https://github.com/EmreTekir/anycubic-slicer-mcp
cd anycubic-slicer-mcp
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install .
```

Register it with Claude Code (for all your projects):

```powershell
claude mcp add --scope user anycubic-slicer -- "$PWD\.venv\Scripts\anycubic-slicer-mcp.exe"
```

Or in any MCP client config:

```json
{
  "mcpServers": {
    "anycubic-slicer": {
      "command": "C:\\path\\to\\anycubic-slicer-mcp\\.venv\\Scripts\\anycubic-slicer-mcp.exe"
    }
  }
}
```

With [uv](https://docs.astral.sh/uv/) you can skip the clone:
`uvx --from git+https://github.com/EmreTekir/anycubic-slicer-mcp anycubic-slicer-mcp`

### Environment variables

| Variable | Default |
|---|---|
| `ANYCUBIC_SLICER_EXE` | `C:\Program Files\AnycubicSlicerNext\AnycubicSlicerNext.exe` |
| `ANYCUBIC_SLICER_DATA` | `%APPDATA%\AnycubicSlicerNext` |
| `ANYCUBIC_MCP_ALLOW_GCODE_TEMPLATES` | unset; `1` allows editing raw G-code templates (see Safety) |
| `ANYCUBIC_MCP_ALLOW_NETWORK_PATHS` | unset; `1` allows `\\server\share` paths (see Safety) |

## Tools

| Tool | What it does |
|---|---|
| `slicer_status` | Install path, version, active preset folder, printer/process/filament selected in the GUI |
| `list_presets` | Machine / process / filament presets, filtered to those compatible with a printer |
| `get_preset` | Fully resolved preset (inheritance merged); `search` finds setting keys |
| `model_info` | Size, bounding box, volume, facet count, manifold check |
| `slice_model` | STL/3MF/OBJ/STEP → `.gcode` + `.gcode.3mf` with time, grams, meters, layers and warnings |
| `gcode_summary` | The same estimates read from an existing `.gcode` |
| `preview_gcode` | Top-down PNG: all layers shaded by height, or one layer colored by feature |
| `open_in_slicer` | Open a file in the slicer GUI (only when you ask for it) |
| `save_user_preset` | Save a user preset that appears in the GUI |

Settings are changed with `overrides`, e.g.
`{"sparse_infill_density": "20%", "enable_support": true, "brim_type": "outer_only", "nozzle_temperature": 215}`.
Each key is routed to the process, filament or machine config that owns it. Unknown keys are rejected,
and the result lists `warnings` for values the slicer clamped or ignored (for example a bed temperature
key that doesn't match the selected plate type).

Notes:
- Filament names can be given naturally: with a printer selected, `"Anycubic PLA"` means
  `Anycubic PLA @<that printer>`.
- `scale` and `rotate_z` work on STL and OBJ files (the model is transformed before slicing).
- `arrange=false` keeps the coordinates stored in the file, so a model at the origin may land off the bed.
- Results are written next to the first model unless `output_dir` is given; existing files with the
  same name are replaced.

## How it works

- Presets live as JSON under `%APPDATA%\AnycubicSlicerNext\system\Anycubic\…` and `user\<folder>\…`.
  Each may `inherits` a parent; the server merges the chain into the flat config the CLI expects.
  The active user folder and the GUI's selection come from `AnycubicSlicerNext.conf`.
- The CLI checks `compatible_printers` against the printer's system parent, so flattened user printers
  get that parent written back. Using a process or filament made for another printer works, and is
  reported as a warning.
- Anycubic filaments carry per-nozzle variants (`nozzle_temperature_HS`, `_BRASS`) that win over the
  plain key; setting the plain key updates them too.
- The slicer's own first-layer estimate is far too low, so `first_layer_time` is measured from the
  `M73` progress marks when the G-code has them (`first_layer_time_source` says which was used).
- The slicer's own `--scale` above 1 and `--rotate` crash it, so models are transformed in Python.
- Tools run in worker threads: a long slice does not block other requests.

## Safety

The server runs locally over stdio with your user's permissions. It is built so a confused or
manipulated agent (for example one following instructions hidden in a downloaded file) cannot turn it
into more than a slicer:

- `post_process` (programs the slicer runs after slicing) can never be set through the tools.
  3MF files are scanned before they are sliced, inspected or opened, and refused if any settings entry
  carries a post-processing script or cannot be checked reliably.
- Raw G-code templates (`machine_start_gcode`, `filament_end_gcode`, …) are read-only unless you set
  `ANYCUBIC_MCP_ALLOW_GCODE_TEMPLATES=1`.
- Setting values may not contain line breaks or control characters, so a value that a G-code template
  expands (a filament type, a note) cannot smuggle in extra G-code lines.
- Network paths (`\\server\share\…`, mapped network drives) are refused before anything touches the
  filesystem, because merely opening one makes Windows send your credentials to that server.
- File tools only accept existing files with model or `.gcode` extensions. `slice_model` writes only
  `.gcode` / `.gcode.3mf` files; `save_user_preset` writes only into the slicer's user preset folder and
  cannot change a preset's name, parent or compatibility fields.
- Image size, timeout, copies, scale and plate number are bounded.

What it does **not** protect against:
- Your own presets: a user preset that already contains a `post_process` script (set up in the GUI) is
  used as-is, exactly as the GUI would use it.
- A 3MF sliced with `use_project_settings` brings its own start/end G-code, as it would in the GUI.
- Temperatures and speeds are ordinary settings and are passed through. Check what the agent sliced
  before you print it.

## Development

```powershell
.venv\Scripts\python.exe -m pip install -e .[test]
.venv\Scripts\python.exe -m pytest
```

The tests need no slicer install. Changes that touch slicing should also be checked against a real
slicer with `examples/calibration_cube.stl`.

## License

MIT

"""Tests that need no slicer install: safety checks, parsing, presets, transforms."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from anycubic_mcp import presets, preview, server, slicer

CLEAN_PROJECT = json.dumps({"layer_height": "0.2", "post_process": []}, indent=4)


def make_3mf(tmp_path: Path, name: str, entries: dict[str, bytes]) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", b"<model/>")
        for entry, data in entries.items():
            archive.writestr(entry, data)
    return path


# ---- 3MF post-processing scripts ---------------------------------------
@pytest.mark.parametrize("entries", [
    {"Metadata/project_settings.config": CLEAN_PROJECT.encode()},
    {"Metadata/plate_1.gcode.metadata": b"; layer_height = 0.2\r\n; post_process = \r\n; z_hop = 0.4\r\n"},
    {"Metadata/project_settings.config": CLEAN_PROJECT.replace("\n", "\r\n").encode()},
])
def test_clean_projects_pass(tmp_path, entries):
    slicer.check_project_scripts(make_3mf(tmp_path, "ok.3mf", entries))


@pytest.mark.parametrize("entries", [
    {"Metadata/project_settings.config": json.dumps({"post_process": ["calc.exe"]}).encode()},
    {"Metadata/project_settings.config": json.dumps({"post_process": "calc.exe"}).encode()},
    {"Metadata/project_settings.config": b"\xef\xbb\xbf" + json.dumps({"post_process": ["x"]}).encode()},
    {"Metadata/project_settings.config": json.dumps({"post_process": ["x"]}).encode("utf-16")},
    {"Metadata/Slic3r_PE.config": b"; layer_height = 0.2\n; post_process = C:\\evil.bat\n"},
    {"Metadata/project_settings.config": b'{"POST_PROCESS": ["x"], broken'},
    {"Metadata/project_settings.config": b'{"post_process": [], "post_process": ["x"]}'},
    {"Metadata/project_settings.config": b" " * (slicer.PROJECT_ENTRY_LIMIT + 1)},
    {"Metadata/project_settings.config": b'{"post\\u005fprocess": ["calc.exe"]}'},
    {"Metadata/project_settings.config": b'{"plate": {"post_process": ["calc.exe"]}}'},
    {"Metadata/project_settings.config": b'{"post\\u005fprocess": ["x"], broken'},
])
def test_scripted_projects_refused(tmp_path, entries):
    with pytest.raises(ValueError):
        slicer.check_project_scripts(make_3mf(tmp_path, "evil.3mf", entries))


def test_not_a_zip(tmp_path):
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        slicer.check_project_scripts(bad)


# ---- paths and keys ------------------------------------------------------
def test_unc_paths_refused():
    for path in (r"\\attacker\share\x.stl", "//attacker/share/x.stl", r"\\?\UNC\attacker\share\x.stl",
                 r"\\.\pipe\x.stl"):
        with pytest.raises(ValueError, match="UNC"):
            slicer.checked_file(path, slicer.MODEL_SUFFIXES)


def test_unc_refused_before_touching_the_filesystem(monkeypatch):
    def boom(*_):
        raise AssertionError("filesystem was touched")
    monkeypatch.setattr(slicer.Path, "resolve", boom)
    monkeypatch.setattr(slicer.Path, "is_file", boom)
    monkeypatch.setattr(slicer.Path, "stat", boom)
    with pytest.raises(ValueError, match="UNC"):
        slicer.checked_file(r"\\attacker\share\x.stl", slicer.MODEL_SUFFIXES)


def test_temperature_warnings():
    pla = {"nozzle_temperature_range_low": ["190"], "nozzle_temperature_range_high": ["240"]}
    assert slicer.temperature_warnings(pla, {"nozzle_temperature": 400})
    assert not slicer.temperature_warnings(pla, {"nozzle_temperature": 215, "wall_loops": 9})


def test_values_cannot_carry_gcode_lines():
    for value in ("PLA\nM104 S300", "PLA\rG28", "x\x00y"):
        with pytest.raises(ValueError, match="line breaks"):
            slicer.set_value({}, "filament_type", value)


def test_file_checks(tmp_path):
    (tmp_path / "-x.stl").write_bytes(b"x")
    (tmp_path / "x.exe").write_bytes(b"x")
    with pytest.raises(ValueError):
        slicer.checked_file(str(tmp_path / "-x.stl"), slicer.MODEL_SUFFIXES)
    with pytest.raises(ValueError):
        slicer.checked_file(str(tmp_path / "x.exe"), slicer.MODEL_SUFFIXES)
    with pytest.raises(FileNotFoundError):
        slicer.checked_file(str(tmp_path / "missing.stl"), slicer.MODEL_SUFFIXES)


@pytest.mark.parametrize("key", ["post_process", "machine_start_gcode", "filament_end_gcode"])
def test_blocked_keys(key):
    with pytest.raises(ValueError):
        slicer.check_setting_keys([key])


def test_allowed_gcode_named_keys():
    slicer.check_setting_keys(["gcode_flavor", "emit_machine_limits_to_gcode", "wall_loops"])


def test_set_value():
    cfg = {"flag": ["1"], "plain": "0", "temps": ["200", "200"]}
    slicer.set_value(cfg, "flag", False)
    slicer.set_value(cfg, "plain", True)
    slicer.set_value(cfg, "temps", 215)
    assert cfg == {"flag": ["0"], "plain": "1", "temps": ["215", "215"]}
    with pytest.raises(ValueError):
        slicer.set_value(cfg, "plain", None)


def test_apply_overrides_routes_and_syncs_variants():
    configs = {"process": {"wall_loops": "2"}, "machine": {"z_hop": ["0.4"]},
               "filament": {"nozzle_temperature": ["220"], "nozzle_temperature_HS": ["230"]}}
    second = {"nozzle_temperature": ["210"], "nozzle_temperature_HS": ["225"]}
    placed = slicer.apply_overrides(configs, {"wall_loops": 3, "nozzle_temperature": 205, "z_hop": 0.2}, [second])
    assert placed == {"wall_loops": "process", "nozzle_temperature": "filament",
                      "nozzle_temperature_HS": "filament", "z_hop": "machine"}
    assert configs["filament"]["nozzle_temperature_HS"] == ["205"]
    assert second == {"nozzle_temperature": ["205"], "nozzle_temperature_HS": ["205"]}
    with pytest.raises(ValueError, match="Unknown"):
        slicer.apply_overrides(configs, {"wal_loops": 3})


# ---- G-code reading --------------------------------------------------------
GCODE = """; HEADER_BLOCK_START
; total layers count = 2
; HEADER_BLOCK_END
; layer_height = 999
M73 P0 R10
; NormalR is the marker below
M73 P0 NormalR600s
M140 S0
M140 S60
M83
;LAYER_CHANGE
G1 Z0.2
;TYPE:Outer wall
G1 X10 Y0 E1
M73 P50 NormalR500s
G1X10 Y10 E1
G3 X0 Y10 I-5 J0 E1
;LAYER_CHANGE
G1 Z0.4
M73 P90 NormalR100s
G1 X0 Y0 E1
G91
G1 X-5 Y-5
G90
M73 P100 NormalR0s
; filament used [g] = 3.56
; CONFIG_BLOCK_START = begin
; layer_height = 0.2
; curr_bed_type = Textured PEI Plate
; textured_plate_temp = 60
; sparse_infill_density = 100%
; filament_settings_id = "PLA A";"PLA B"
; CONFIG_BLOCK_END = end
"""


@pytest.fixture
def gcode(tmp_path):
    path = tmp_path / "t.gcode"
    path.write_text(GCODE, encoding="utf-8")
    return path


def test_gcode_stats(gcode):
    stats = slicer.gcode_stats(gcode)
    assert stats["layer_height"] == 0.2  # from the config block, not the stray header comment
    assert stats["layers"] == 2 and stats["filament_g"] == 3.56
    assert stats["bed_temperature"] == 60
    assert stats["filament"] == "PLA A, PLA B"
    assert stats["first_layer_time_source"] == "M73 progress marks"


def test_override_warnings(gcode):
    warnings = slicer.override_warnings({"sparse_infill_density": "250%", "hot_plate_temp": 55,
                                         "layer_height": 0.20}, {}, gcode)
    assert any("250%" in w for w in warnings)
    assert any("textured_plate_temp" in w for w in warnings)
    assert not any(w.startswith("layer_height") for w in warnings)


def test_preview_segments(gcode):
    segs = list(preview.segments(gcode))
    layers = {s[0] for s in segs}
    assert layers == {1, 2}
    # The arc ends at (0, 10) and is drawn as several short pieces, not one chord.
    arc = [s for s in segs if s[0] == 1 and s[3] >= 10 and s[5] >= 10]
    assert len(arc) > 3 and arc[-1][4:6] == (0.0, 10.0)
    # The G91 park move is travel and must not leave a bogus segment at negative coordinates.
    assert all(min(s[2:6]) >= -1e-6 for s in segs)
    png, meta = preview.render(str(gcode), layer=1, size=200)
    assert png.startswith(b"\x89PNG") and meta["layer"] == 1
    with pytest.raises(ValueError):
        preview.render(str(gcode), layer=5)


def test_absolute_extrusion(tmp_path):
    path = tmp_path / "abs.gcode"
    path.write_text("M82\n;LAYER_CHANGE\nG1 X0 Y0\nG1 X10 E1\nG1 X20 E0.5\nG92 E0\nG1 X30 E1\n")
    assert [s[4] for s in preview.segments(path)] == [10.0, 30.0]


# ---- presets ---------------------------------------------------------------
def write_preset(root: Path, rel: str, data: dict | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    sys_dir = "system/Anycubic"
    write_preset(root, f"{sys_dir}/machine/base.json", {"name": "fdm_machine_common", "instantiation": "false"})
    write_preset(root, f"{sys_dir}/machine/s1.json", {"name": "Kobra S1 0.4", "inherits": "fdm_machine_common",
                                                      "default_print_profile": "0.20mm @S1",
                                                      "default_filament_profile": ["PLA @S1"]})
    write_preset(root, f"{sys_dir}/machine/model.json", {"name": "Kobra S1", "type": "machine_model"})
    write_preset(root, f"{sys_dir}/process/p.json", {"name": "0.20mm @S1", "wall_loops": "2",
                                                     "compatible_printers": ["Kobra S1 0.4"]})
    for name in ("PLA @S1", "PLA Matte @S1", "PLA Silk @S1"):
        write_preset(root, f"{sys_dir}/filament/{name}.json",
                     {"name": name, "nozzle_temperature": ["220"], "nozzle_temperature_HS": ["230"],
                      "compatible_printers": ["Kobra S1 0.4"]})
    user = "user/default"
    write_preset(root, f"{user}/process/same.json", {"name": "0.20mm @S1", "inherits": "0.20mm @S1",
                                                     "wall_loops": "4"})
    write_preset(root, f"{user}/machine/a.json", {"name": "Loop A", "inherits": "Loop B"})
    write_preset(root, f"{user}/machine/b.json", {"name": "Loop B", "inherits": "Loop A"})
    write_preset(root, f"{user}/filament/cp1254.json", '{"name": "Şeffaf"}'.encode("cp1254"))
    write_preset(root, f"{user}/filament/badname.json", {"name": ["not", "a", "string"]})
    monkeypatch.setattr(presets, "DATA", root)
    store = presets.PresetStore()
    monkeypatch.setattr(server, "store", store)
    return store


def test_store_loads_despite_bad_files(data_dir):
    assert "Kobra S1" not in data_dir.presets["machine"]  # machine_model skipped
    assert "Şeffaf" not in data_dir.presets["filament"]


def test_user_preset_shadowing_system_parent(data_dir):
    assert data_dir.resolve("process", "0.20mm @S1")["wall_loops"] == "4"


def test_cycles_do_not_hang(data_dir):
    with pytest.raises(ValueError, match="cycle"):
        data_dir.resolve("machine", "Loop A")
    assert data_dir.system_root_name("machine", "Loop A") == "Loop A"
    assert data_dir.list("process", "Loop A") == []


def test_natural_filament_name(data_dir):
    assert data_dir.get("filament", "PLA", "Kobra S1 0.4").name == "PLA @S1"
    with pytest.raises(LookupError):
        data_dir.get("filament", "PLA")


def test_save_user_preset(data_dir):
    save = server.save_user_preset.__wrapped__
    out = save("filament", "My PLA", "PLA @S1", {"nozzle_temperature": 210})
    assert out["preset"]["nozzle_temperature"] == ["210"]
    assert out["preset"]["nozzle_temperature_HS"] == ["210"]
    for name, settings in [(r"..\..\evil", {"nozzle_temperature": 1}), ("NUL", {"nozzle_temperature": 1}),
                           ("ok", {"name": "PLA @S1"}), ("ok", {"inherits": "ok"}),
                           ("ok", {"post_process": ["x"]}), ("ok", {"nope": 1}), ("PLA @S1", {"nozzle_temperature": 1})]:
        with pytest.raises((ValueError, LookupError)):
            save("filament", name, "PLA @S1", settings)


# ---- transforms --------------------------------------------------------------
def binary_stl(path: Path, tris: list[tuple]) -> None:
    with path.open("wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            fh.write(struct.pack("<12f", 0, 0, 1, *t) + b"\0\0")


def test_transform_binary_stl(tmp_path):
    src, dst = tmp_path / "a.stl", tmp_path / "b.stl"
    binary_stl(src, [(0, 0, 0, 10, 0, 0, 0, 10, 0)])
    slicer.transform_model(src, dst, 2, 90)
    data = dst.read_bytes()
    pts = struct.unpack_from("<9f", data, 84 + 12)
    xs, ys = pts[0::3], pts[1::3]
    assert max(xs) - min(xs) == pytest.approx(20) and max(ys) - min(ys) == pytest.approx(20)
    assert len(data) == len(src.read_bytes())


def test_transform_ascii_stl_and_obj(tmp_path):
    ascii_stl = tmp_path / "a.stl"
    ascii_stl.write_text("solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\n"
                         "vertex 0 10 0\nendloop\nendfacet\nendsolid t\n")
    slicer.transform_model(ascii_stl, tmp_path / "b.stl", 0.5, 0)
    # Scaled, with the footprint's lower-left corner kept at (0, 0).
    out = (tmp_path / "b.stl").read_bytes()
    assert b"vertex 5.000000 0.000000 0.000000" in out and b"\r\r" not in out
    obj = tmp_path / "a.obj"
    obj.write_text("v 0 0 0\nv 10 0 0\nv 0 10 0\nf 1 2 3\n")
    slicer.transform_model(obj, tmp_path / "b.obj", 1, 180)
    assert (tmp_path / "b.obj").read_text().splitlines()[0] == "v 10.000000 10.000000 0.000000"

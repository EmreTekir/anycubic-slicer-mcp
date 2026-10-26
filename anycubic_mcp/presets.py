"""Anycubic Slicer Next preset discovery and inheritance resolution.

Anycubic Slicer Next is a BambuStudio/OrcaSlicer fork. Presets are JSON files:
  <data>/system/<Vendor>/{machine,process,filament}/*.json   (vendor presets)
  <data>/user/<preset_folder>/{machine,process,filament}/**/*.json  (user presets)
Each preset may name a parent via "inherits"; the effective config is the
parent chain merged root-first. The CLI wants flat configs, so we resolve them.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("machine", "process", "filament")

EXE = Path(os.environ.get("ANYCUBIC_SLICER_EXE", r"C:\Program Files\AnycubicSlicerNext\AnycubicSlicerNext.exe"))
DATA = Path(os.environ.get("ANYCUBIC_SLICER_DATA", Path(os.environ.get("APPDATA", "")) / "AnycubicSlicerNext"))


@dataclass
class Preset:
    name: str
    kind: str
    source: str  # "system" | "user"
    path: Path
    raw: dict = field(repr=False)

    @property
    def visible(self) -> bool:
        # System base presets ("fdm_*_common", "@acbase") are not selectable.
        return self.source == "user" or str(self.raw.get("instantiation", "true")).lower() == "true"


def read_app_config() -> dict:
    """The .conf file is JSON followed by a '# MD5 checksum' trailer."""
    path = DATA / "AnycubicSlicerNext.conf"
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    text = re.sub(r"\n#[^\n]*\s*$", "", text.rstrip())
    try:
        conf = json.loads(text)
    except ValueError:
        return {}
    return conf if isinstance(conf, dict) else {}


class PresetStore:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        # Build everything first and swap at the end, so tools running in other
        # threads never see a half-loaded store.
        conf = read_app_config()
        user_folder = (conf.get("app") or {}).get("preset_folder") or "default"
        presets: dict[str, dict[str, Preset]] = {k: {} for k in KINDS}
        shadowed: dict[str, dict[str, Preset]] = {k: {} for k in KINDS}
        system_root = DATA / "system"
        if not system_root.is_dir():
            system_root = EXE.parent / "resources" / "profiles"
        # Missing install: start empty so slicer_status can report it.
        vendor_dirs = sorted(p for p in system_root.iterdir() if p.is_dir()) if system_root.is_dir() else []
        for vendor_dir in vendor_dirs:
            for kind in KINDS:
                self._scan(presets[kind], vendor_dir / kind, kind, "system")
        # User presets override system ones with the same name; keep the system
        # one reachable for a user preset that inherits from it.
        user_root = DATA / "user" / user_folder
        for kind in KINDS:
            system = dict(presets[kind])
            self._scan(presets[kind], user_root / kind, kind, "user")
            shadowed[kind] = {n: p for n, p in system.items() if presets[kind][n] is not p}
        self.conf, self.user_folder, self.user_root = conf, user_folder, user_root
        self.presets, self.shadowed = presets, shadowed

    @staticmethod
    def _scan(table: dict[str, Preset], folder: Path, kind: str, source: str) -> None:
        if not folder.is_dir():
            return
        for path in sorted(folder.rglob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):  # unreadable, not UTF-8 or not JSON
                continue
            if not isinstance(raw, dict) or raw.get("type") == "machine_model":
                continue  # printer model descriptions are not printer presets
            name = raw.get("name") or path.stem
            if isinstance(name, str):
                table[name] = Preset(name, kind, source, path, raw)

    def _parent(self, kind: str, preset: Preset) -> Preset | None:
        parent = preset.raw.get("inherits") or ""
        if not isinstance(parent, str) or not parent:
            return None
        if parent == preset.name:
            return self.shadowed[kind].get(parent)
        return self.presets[kind].get(parent)

    # ---- lookup -------------------------------------------------------
    def get(self, kind: str, name: str, printer: str | None = None) -> Preset:
        """Exact name or unique substring; printer narrows ambiguous process/filament matches."""
        table = self.presets[kind]
        if name in table:
            return table[name]
        lowered = name.lower()
        matches = [p for n, p in table.items() if lowered in n.lower() and p.visible]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"No {kind} preset matches '{name}'")
        exact = [p for p in matches if p.name.lower() == lowered]
        if exact:
            return exact[0]
        if printer and kind != "machine":
            fitting = [p for p in matches if self.is_compatible(kind, p.name, printer)]
            if len(fitting) == 1:
                return fitting[0]
            # "Anycubic PLA" should mean "Anycubic PLA @<printer>", not PLA Matte/Silk.
            base = [p for p in fitting if p.name.split(" @")[0].strip().lower() == lowered]
            if len(base) > 1:  # e.g. a user's "... - Copy" next to the vendor preset
                base = [p for p in base if p.source == "system"]
            if len(base) == 1:
                return base[0]
            matches = fitting or matches
        names = ", ".join(sorted(p.name for p in matches)[:15])
        raise LookupError(f"'{name}' is ambiguous for {kind}; candidates: {names}")

    def resolve(self, kind: str, name: str) -> dict:
        """Merge the inheritance chain root-first into one flat config."""
        chain: list[Preset] = []
        seen: set[int] = set()
        preset: Preset | None = self.get(kind, name)
        while preset is not None:
            if id(preset) in seen:
                raise ValueError(f"Inheritance cycle at '{preset.name}'")
            seen.add(id(preset))
            chain.append(preset)
            parent = preset.raw.get("inherits") or ""
            preset = self._parent(kind, preset)
            if parent and preset is None:
                raise LookupError(f"{kind} preset '{chain[-1].name}' inherits missing '{parent}'")
        merged: dict = {}
        for p in reversed(chain):
            merged.update(p.raw)
        top = chain[0]
        merged["name"] = top.name
        merged["type"] = kind
        merged["inherits"] = ""
        merged["from"] = "system" if top.source == "system" else "User"
        return merged

    def system_root_name(self, kind: str, name: str) -> str:
        """Nearest selectable system ancestor; used for compatibility checks."""
        seen: set[int] = set()
        preset: Preset | None = self.get(kind, name)
        while preset is not None and id(preset) not in seen:
            if preset.source == "system" and preset.visible:
                return preset.name
            seen.add(id(preset))
            preset = self._parent(kind, preset)
        return name

    def is_compatible(self, kind: str, name: str, printer: str) -> bool:
        try:
            cfg = self.resolve(kind, name)
        except (LookupError, ValueError):
            return False
        compatible = cfg.get("compatible_printers") or []
        if not compatible:
            return True
        return printer in compatible or self.system_root_name("machine", printer) in compatible

    def list(self, kind: str, printer: str | None = None, search: str | None = None,
             include_incompatible: bool = False) -> list[dict]:
        out = []
        for p in self.presets[kind].values():
            if not p.visible:
                continue
            if search and search.lower() not in p.name.lower():
                continue
            compatible = True
            if printer and kind != "machine":
                compatible = self.is_compatible(kind, p.name, printer)
                if not compatible and not include_incompatible:
                    continue
            item = {"name": p.name, "source": p.source}
            if p.source == "user":
                item["inherits"] = p.raw.get("inherits", "")
            if printer and kind != "machine" and not compatible:
                item["compatible"] = False
            out.append(item)
        out.sort(key=lambda d: (d["source"] != "user", d["name"]))
        return out

    # ---- GUI selections ----------------------------------------------
    def current_selection(self) -> dict:
        """Printer selected in the GUI plus its remembered process/filament."""
        machine = (self.conf.get("presets") or {}).get("machine")
        return {"machine": machine, **self.remembered_for(machine)}

    def remembered_for(self, machine: str | None) -> dict:
        for entry in self.conf.get("anycubic_presets") or []:
            if isinstance(entry, dict) and entry.get("machine") == machine:
                slots = sorted((int(k.split("_")[1]), v) for k, v in entry.items()
                               if re.fullmatch(r"filament_\d+", k))
                filaments = [entry.get("filament")] + [v for _, v in slots]
                return {"process": entry.get("process"), "filaments": [f for f in filaments if f],
                        "bed_type": entry.get("curr_bed_type")}
        return {}

    def defaults_for(self, machine: str) -> dict:
        """Remembered choices, falling back to the printer's own defaults."""
        remembered = self.remembered_for(machine)
        cfg = self.resolve("machine", machine)
        process = remembered.get("process") or cfg.get("default_print_profile")
        filaments = remembered.get("filaments") or cfg.get("default_filament_profile") or []
        if isinstance(filaments, str):
            filaments = [filaments]
        if not process or not filaments:
            raise ValueError(f"No default process/filament known for '{machine}'; pass them explicitly")
        return {"process": process, "filaments": filaments[:1]}

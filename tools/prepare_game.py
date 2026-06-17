#!/usr/bin/env python3
"""Prepare a PS1 game image for a first PSXRecomp pass.

The tool accepts a .cue/.bin/.iso image or a .7z/.rar/.zip archive containing
one. It extracts the data track when needed, reads SYSTEM.CNF, pulls the boot
PS-X EXE from the disc filesystem, parses the EXE header, and emits:

  games/<slug>/game.toml
  games/<slug>/seeds.txt

It intentionally stops before recompilation. Game-specific seeds, stack-word
patches, overlays, and runtime build wiring still need evidence from testing.
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


RAW_SECTOR_SIZE = 2352
COOKED_SECTOR_SIZE = 2048
RAW_USER_OFFSET = 24
PSX_EXE_HEADER_SIZE = 0x800


ARCHIVE_EXTS = {".7z", ".rar", ".zip"}
IMAGE_EXTS = {".cue", ".bin", ".iso"}


@dataclass
class CueTrack:
    file_name: str
    number: int
    mode: str
    index01_frames: int


@dataclass
class DiscSource:
    disc_path: Path
    data_path: Path
    data_track: CueTrack | None


@dataclass
class ISOEntry:
    name: str
    lba: int
    size: int
    is_dir: bool


@dataclass
class ExeHeader:
    initial_pc: int
    initial_gp: int
    load_address: int
    file_size: int
    memfill_start: int
    memfill_size: int
    initial_sp: int
    initial_fp: int
    stack_base: int
    stack_offset: int


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def as_posix_rel(path: Path, root: Path) -> str:
    path = path.resolve()
    root = root.resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return os.path.relpath(path, root).replace("\\", "/")


def toml_string(value: str) -> str:
    return json.dumps(value)


def hex32(value: int) -> str:
    return f"0x{value:08X}"


def derive_out_stem(exe_name: str) -> str:
    lower = exe_name.lower()
    if lower.endswith(".bin") or lower.endswith(".exe"):
        return exe_name[:-4]
    return exe_name


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "game"


def split_archive_path(path: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+", path) if part not in ("", ".")]


def archive_member_to_path(out_dir: Path, member: str) -> Path:
    return out_dir.joinpath(*split_archive_path(member))


def archive_member_basename(member: str) -> str:
    parts = split_archive_path(member)
    return parts[-1] if parts else member


def norm_member(member: str) -> str:
    return "/".join(split_archive_path(member)).lower()


def find_7z(explicit: str | None) -> str:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("PSX_7Z"):
        candidates.append(os.environ["PSX_7Z"])
    for exe in ("7z", "7zz", "7za"):
        found = shutil.which(exe)
        if found:
            candidates.append(found)
    candidates.append(r"C:\Program Files (x86)\Lua\5.1\7z.exe")

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    fail("7-Zip not found. Pass --seven-zip or set PSX_7Z.")


def run_checked(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        fail(f"command failed: {' '.join(cmd)}\n{detail}")
    return proc


def list_archive(archive: Path, seven_zip: str) -> list[dict[str, str]]:
    proc = run_checked([seven_zip, "l", "-slt", str(archive)])
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key] = value
    if current:
        records.append(current)

    archive_text = str(archive).lower()
    members: list[dict[str, str]] = []
    for rec in records:
        path = rec.get("Path", "")
        if not path:
            continue
        if path.lower() == archive_text:
            continue
        if rec.get("Folder") == "+" or rec.get("Attributes", "").startswith("D"):
            continue
        members.append(rec)
    return members


def choose_archive_cue(members: Iterable[dict[str, str]], archive: Path) -> str:
    cues = [m["Path"] for m in members if m.get("Path", "").lower().endswith(".cue")]
    if not cues:
        fail(f"{archive.name} does not contain a .cue file")
    if len(cues) == 1:
        return cues[0]

    stem_words = set(re.findall(r"[a-z0-9]+", archive.stem.lower()))
    scored: list[tuple[int, str]] = []
    for cue in cues:
        cue_words = set(re.findall(r"[a-z0-9]+", archive_member_basename(cue).lower()))
        scored.append((len(stem_words & cue_words), cue))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return scored[0][1]


def extract_archive_member(
    archive: Path,
    member: str,
    out_dir: Path,
    seven_zip: str,
    force: bool,
) -> Path:
    target = archive_member_to_path(out_dir, member)
    if target.exists() and not force:
        return target
    out_dir.mkdir(parents=True, exist_ok=True)
    run_checked([seven_zip, "x", str(archive), f"-o{out_dir}", "-y", member])
    if not target.exists():
        fail(f"7-Zip reported success but {target} was not created")
    return target


def parse_cue(cue_path: Path) -> list[CueTrack]:
    tracks: list[CueTrack] = []
    current_file: str | None = None
    text = cue_path.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        file_match = re.match(r'FILE\s+"([^"]+)"\s+\S+', line, re.IGNORECASE)
        if file_match:
            current_file = file_match.group(1)
            continue
        track_match = re.match(r"TRACK\s+(\d+)\s+(\S+)", line, re.IGNORECASE)
        if track_match:
            if not current_file:
                fail(f"{cue_path}: TRACK appears before FILE")
            tracks.append(
                CueTrack(
                    file_name=current_file,
                    number=int(track_match.group(1)),
                    mode=track_match.group(2).upper(),
                    index01_frames=0,
                )
            )
            continue
        index_match = re.match(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", line, re.IGNORECASE)
        if index_match and tracks:
            minutes, seconds, frames = (int(index_match.group(i)) for i in range(1, 4))
            tracks[-1].index01_frames = ((minutes * 60) + seconds) * 75 + frames
    return tracks


def choose_data_track(tracks: list[CueTrack], cue_path: Path) -> CueTrack:
    data_tracks = [track for track in tracks if "AUDIO" not in track.mode]
    if not data_tracks:
        fail(f"{cue_path} does not describe a data track")
    preferred = [
        track
        for track in data_tracks
        if track.number == 1 or "MODE2" in track.mode or "MODE1" in track.mode
    ]
    return preferred[0] if preferred else data_tracks[0]


def resolve_cue_file(cue_path: Path, file_name: str) -> Path:
    file_path = Path(file_name.replace("\\", os.sep))
    if file_path.is_absolute():
        return file_path
    return cue_path.parent / file_path


def find_archive_member_for_cue_file(
    members: Iterable[dict[str, str]],
    cue_member: str,
    cue_file_name: str,
) -> str:
    cue_parent_parts = split_archive_path(cue_member)[:-1]
    file_parts = split_archive_path(cue_file_name)
    expected = "/".join(cue_parent_parts + file_parts).lower()
    by_norm = {norm_member(m["Path"]): m["Path"] for m in members if "Path" in m}
    if expected in by_norm:
        return by_norm[expected]

    basename = file_parts[-1].lower()
    basename_hits = [m["Path"] for m in members if archive_member_basename(m["Path"]).lower() == basename]
    if len(basename_hits) == 1:
        return basename_hits[0]
    if basename_hits:
        basename_hits.sort(key=lambda item: item.lower())
        return basename_hits[0]
    fail(f"archive does not contain cue-referenced file {cue_file_name!r}")


class DiscImage:
    def __init__(self, data_path: Path, track: CueTrack | None):
        self.data_path = data_path
        self.track = track
        if not data_path.exists():
            fail(f"data track not found: {data_path}")

        size = data_path.stat().st_size
        mode = track.mode if track else ""
        if "2048" in mode:
            self.stride = COOKED_SECTOR_SIZE
            self.user_offset = 0
        elif "2352" in mode or size % RAW_SECTOR_SIZE == 0:
            self.stride = RAW_SECTOR_SIZE
            self.user_offset = RAW_USER_OFFSET
        else:
            self.stride = COOKED_SECTOR_SIZE
            self.user_offset = 0

        self.base_offset = (track.index01_frames * self.stride) if track else 0

    def read_sector(self, lba: int) -> bytes:
        offset = self.base_offset + (lba * self.stride) + self.user_offset
        with self.data_path.open("rb") as f:
            f.seek(offset)
            data = f.read(COOKED_SECTOR_SIZE)
        if len(data) != COOKED_SECTOR_SIZE:
            fail(f"short read from {self.data_path} at LBA {lba}")
        return data

    def read_extent(self, lba: int, size: int) -> bytes:
        sectors = (size + COOKED_SECTOR_SIZE - 1) // COOKED_SECTOR_SIZE
        data = b"".join(self.read_sector(lba + i) for i in range(sectors))
        return data[:size]


class ISO9660:
    def __init__(self, disc: DiscImage):
        self.disc = disc
        pvd = disc.read_sector(16)
        if pvd[0] != 1 or pvd[1:6] != b"CD001":
            fail(f"{disc.data_path} does not look like an ISO9660 data track")
        root = pvd[156:]
        self.root = self._parse_record(root)
        if not self.root or not self.root.is_dir:
            fail("ISO9660 root directory record is missing or invalid")

    @staticmethod
    def _clean_name(raw: bytes) -> str:
        if raw == b"\x00":
            return "."
        if raw == b"\x01":
            return ".."
        text = raw.decode("ascii", errors="replace")
        return text.split(";", 1)[0]

    def _parse_record(self, data: bytes) -> ISOEntry | None:
        if not data or data[0] < 34:
            return None
        length = data[0]
        record = data[:length]
        name_len = record[32]
        name = self._clean_name(record[33 : 33 + name_len])
        lba = struct.unpack_from("<I", record, 2)[0]
        size = struct.unpack_from("<I", record, 10)[0]
        flags = record[25]
        return ISOEntry(name=name, lba=lba, size=size, is_dir=bool(flags & 0x02))

    def list_dir(self, directory: ISOEntry) -> list[ISOEntry]:
        data = self.disc.read_extent(directory.lba, directory.size)
        entries: list[ISOEntry] = []
        pos = 0
        while pos < len(data):
            length = data[pos]
            if length == 0:
                pos = ((pos // COOKED_SECTOR_SIZE) + 1) * COOKED_SECTOR_SIZE
                continue
            record = self._parse_record(data[pos : pos + length])
            pos += length
            if not record or record.name in (".", ".."):
                continue
            entries.append(record)
        return entries

    def find(self, path: str) -> ISOEntry:
        parts = [part for part in re.split(r"[\\/]+", path.strip("\\/")) if part]
        current = self.root
        for i, part in enumerate(parts):
            needle = part.split(";", 1)[0].upper()
            matches = [entry for entry in self.list_dir(current) if entry.name.upper() == needle]
            if not matches:
                fail(f"disc file not found: {path}")
            current = matches[0]
            if i + 1 < len(parts) and not current.is_dir:
                fail(f"disc path component is not a directory: {part}")
        return current

    def read_file(self, path: str) -> bytes:
        entry = self.find(path)
        if entry.is_dir:
            fail(f"disc path is a directory: {path}")
        return self.disc.read_extent(entry.lba, entry.size)


def parse_system_cnf(data: bytes) -> str:
    text = data.decode("ascii", errors="ignore")
    match = re.search(r"^\s*BOOT\s*=\s*cdrom:\\?([^;\r\n]+)", text, re.IGNORECASE | re.MULTILINE)
    if not match:
        fail("SYSTEM.CNF does not contain a BOOT = cdrom:... line")
    boot_path = match.group(1).strip().replace("\\", "/").lstrip("/")
    if not boot_path:
        fail("SYSTEM.CNF boot path is empty")
    return boot_path


def parse_exe_header(data: bytes) -> ExeHeader:
    if len(data) < PSX_EXE_HEADER_SIZE:
        fail("boot EXE is smaller than a PS-X EXE header")
    if data[:8] != b"PS-X EXE":
        fail(f"boot file magic is {data[:8]!r}, expected b'PS-X EXE'")
    fields = struct.unpack_from("<14I", data, 0x08)
    return ExeHeader(
        initial_pc=fields[2],
        initial_gp=fields[3],
        load_address=fields[4],
        file_size=fields[5],
        memfill_start=fields[8],
        memfill_size=fields[9],
        initial_sp=fields[10],
        initial_fp=fields[11],
        stack_base=fields[12],
        stack_offset=fields[13],
    )


def validate_exe_header(header: ExeHeader, data_len: int) -> list[str]:
    warnings: list[str] = []
    if header.load_address < 0x80000000 or header.load_address >= 0xA0000000:
        fail(f"invalid PS-X EXE load address: {hex32(header.load_address)}")
    if header.initial_pc < 0x80000000 or header.initial_pc >= 0xA0000000:
        fail(f"invalid PS-X EXE entry PC: {hex32(header.initial_pc)}")
    if header.file_size == 0:
        fail("PS-X EXE file_size is zero")
    if header.file_size > 2 * 1024 * 1024:
        fail(f"PS-X EXE file_size is larger than PS1 RAM: {hex32(header.file_size)}")
    end_address = header.load_address + header.file_size
    if end_address > 0x80200000:
        fail(
            "PS-X EXE load region exceeds RAM: "
            f"{hex32(header.load_address)}-{hex32(end_address)}"
        )
    if not (header.load_address <= header.initial_pc < end_address):
        warnings.append(
            f"entry PC {hex32(header.initial_pc)} is outside loaded EXE range "
            f"{hex32(header.load_address)}-{hex32(end_address)}"
        )
    expected_min = PSX_EXE_HEADER_SIZE + header.file_size
    if data_len < expected_min:
        fail(
            f"boot EXE is shorter than header file_size: "
            f"{data_len} bytes < {expected_min} bytes"
        )
    if data_len > expected_min:
        warnings.append(
            f"boot EXE has {data_len - expected_min} trailing byte(s) after "
            "the declared PS-X payload"
        )
    return warnings


def canonical_serial(boot_path: str) -> str:
    name = Path(boot_path).name.upper()
    match = re.match(r"([A-Z]{4})[_-](\d{3})[.](\d{2})", name)
    if match:
        return f"{match.group(1)}-{match.group(2)}{match.group(3)}"
    return name.replace("_", "-").replace(".", "")


def write_bytes_if_needed(path: Path, data: bytes, force: bool) -> str:
    if path.exists():
        old = path.read_bytes()
        if old == data:
            return "unchanged"
        if not force:
            fail(f"{path} already exists and differs; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return "written"


def write_text_if_needed(path: Path, text: str, force: bool) -> str:
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        if old == text:
            return "unchanged"
        if not force:
            fail(f"{path} already exists and differs; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return "written"


def planned_bytes_status(path: Path, data: bytes) -> str:
    if not path.exists():
        return "would write"
    return "unchanged" if path.read_bytes() == data else "would update"


def planned_text_status(path: Path, text: str) -> str:
    if not path.exists():
        return "would write"
    old = path.read_text(encoding="utf-8", errors="replace")
    return "unchanged" if old == text else "would update"


def disc_hashes(path: Path) -> tuple[int, str]:
    crc = 0
    sha1 = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            crc = binascii.crc32(chunk, crc)
            sha1.update(chunk)
    return crc & 0xFFFFFFFF, sha1.hexdigest()


def used_debug_ports(games_dir: Path) -> set[int]:
    ports: set[int] = set()
    if not games_dir.exists():
        return ports
    for config in games_dir.glob("*/game.toml"):
        text = config.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"^\s*debug_port\s*=\s*(\d+)\s*$", text, re.MULTILINE):
            ports.add(int(match.group(1)))
    return ports


def existing_debug_port(game_dir: Path) -> int | None:
    config = game_dir / "game.toml"
    if not config.exists():
        return None
    text = config.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^\s*debug_port\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def choose_debug_port(project_root: Path, game_dir: Path, requested: int | None) -> int:
    if requested is not None:
        return requested
    existing = existing_debug_port(game_dir)
    if existing is not None:
        return existing
    used = used_debug_ports(project_root / "games")
    for port in range(4373, 4500):
        if port not in used:
            return port
    return 4373


def prepare_disc_source(args: argparse.Namespace, game_name: str, project_root: Path) -> DiscSource:
    input_path = Path(args.input).resolve()
    if not input_path.exists():
        fail(f"input does not exist: {input_path}")

    suffix = input_path.suffix.lower()
    if suffix in ARCHIVE_EXTS:
        extract_dir = Path(args.extract_dir).resolve() if args.extract_dir else project_root.parent / "extracted" / game_name
        seven_zip = find_7z(args.seven_zip)
        members = list_archive(input_path, seven_zip)
        cue_member = choose_archive_cue(members, input_path)
        cue_path = extract_archive_member(input_path, cue_member, extract_dir, seven_zip, args.force)

        tracks = parse_cue(cue_path)
        data_track = choose_data_track(tracks, cue_path)
        data_member = find_archive_member_for_cue_file(members, cue_member, data_track.file_name)
        data_path = extract_archive_member(input_path, data_member, extract_dir, seven_zip, args.force)
        return DiscSource(disc_path=cue_path, data_path=data_path, data_track=data_track)

    if suffix == ".cue":
        tracks = parse_cue(input_path)
        data_track = choose_data_track(tracks, input_path)
        data_path = resolve_cue_file(input_path, data_track.file_name)
        return DiscSource(disc_path=input_path, data_path=data_path, data_track=data_track)

    if suffix in {".bin", ".iso"}:
        return DiscSource(disc_path=input_path, data_path=input_path, data_track=None)

    fail(f"unsupported input extension {suffix!r}; expected one of {sorted(ARCHIVE_EXTS | IMAGE_EXTS)}")


def build_game_toml(
    *,
    name: str,
    game_id: str,
    exe_path: Path,
    disc_path: Path,
    data_path: Path,
    header: ExeHeader,
    project_root: Path,
    game_dir: Path,
    debug_port: int,
    no_disc_hash: bool,
) -> str:
    stack_base = header.initial_sp or header.stack_base or 0x801FFFF0
    crc_line = ""
    sha1_line = ""
    if not no_disc_hash:
        crc, sha1 = disc_hashes(data_path)
        crc_line = f"disc_crc = {toml_string(hex32(crc))}\n"
        sha1_line = f"disc_sha1 = {toml_string(sha1)}\n"

    seeds_rel = as_posix_rel(game_dir / "seeds.txt", project_root)
    exe_rel = as_posix_rel(exe_path, project_root)
    disc_rel = as_posix_rel(disc_path, project_root)
    slug = game_dir.name
    out_stem = derive_out_stem(exe_path.name)

    return (
        "[game]\n"
        f"name = {toml_string(name)}\n"
        f"id = {toml_string(game_id)}\n"
        f"exe = {toml_string(exe_rel)}\n"
        f"load_address = {toml_string(hex32(header.load_address))}\n"
        f"entry_pc = {toml_string(hex32(header.initial_pc))}\n"
        f"text_size = {toml_string(hex32(header.file_size))}\n"
        f"stack_base = {toml_string(hex32(stack_base))}\n"
        f"disc = {toml_string(disc_rel)}\n"
        f"{crc_line}"
        f"{sha1_line}"
        "\n"
        "[recompiler]\n"
        f"seeds = {toml_string(seeds_rel)}\n"
        "out_dir = \"generated\"\n"
        "strict = true\n"
        f"out_stem = {toml_string(out_stem)}\n"
        "\n"
        "[runtime]\n"
        f"debug_port = {debug_port}\n"
        f"window_title = {toml_string(name)}\n"
        f"memcard_dir = {toml_string('saves/' + slug)}\n"
        "disc_speed = \"1x\"\n"
        "overlay_cache = true\n"
        "overlay_backend = \"sljit\"\n"
        "\n"
        "[video]\n"
        "renderer = \"software\"\n"
        "supersampling = 1\n"
        "antialiasing = true\n"
        "texture_filtering = \"nearest\"\n"
        "crt_filter = \"raw\"\n"
        "aspect_ratio = \"4:3\"\n"
        "\n"
        "[controller]\n"
        "p1_device = \"auto\"\n"
        "p1_analog = false\n"
        "p2_analog = false\n"
    )


def build_seeds_txt(name: str, game_id: str) -> str:
    return (
        f"# Extra static-discovery seeds for {name} ({game_id}).\n"
        "#\n"
        "# Add one hex address per line for confirmed function entries inside the\n"
        "# main PS-X EXE. Use these prefixes when runtime evidence calls for them:\n"
        "#   interior 0xXXXXXXXX\n"
        "#   dispatch_root 0xXXXXXXXX\n"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="PS1 .cue/.bin/.iso, or .7z/.rar/.zip containing a cue/bin image")
    parser.add_argument("--name", help="Display name for game.toml")
    parser.add_argument("--slug", help="games/<slug> directory name")
    parser.add_argument("--game-dir", help="Override output game directory")
    parser.add_argument("--extract-dir", help="Override archive extraction directory")
    parser.add_argument("--project-root", help="PSXRecomp project root; defaults to this script's parent")
    parser.add_argument("--debug-port", type=int, help="Runtime debug port; defaults to first free 4373..4499")
    parser.add_argument("--seven-zip", help="Path to 7z/7zz/7za for archive inputs")
    parser.add_argument("--force", action="store_true", help="Overwrite differing generated files")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and report, but do not write boot EXE, game.toml, or seeds.txt",
    )
    parser.add_argument("--no-disc-hash", action="store_true", help="Do not compute disc_crc/disc_sha1")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    project_root = Path(args.project_root).resolve() if args.project_root else project_root_from_script()
    if not project_root.exists():
        fail(f"project root does not exist: {project_root}")

    input_path = Path(args.input)
    game_name = args.name or input_path.stem
    game_slug = args.slug or slugify(game_name)
    game_dir = Path(args.game_dir).resolve() if args.game_dir else project_root / "games" / game_slug
    debug_port = choose_debug_port(project_root, game_dir, args.debug_port)

    source = prepare_disc_source(args, game_name, project_root)
    iso = ISO9660(DiscImage(source.data_path, source.data_track))
    system_cnf = iso.read_file("SYSTEM.CNF")
    boot_path = parse_system_cnf(system_cnf)
    exe_data = iso.read_file(boot_path)
    header = parse_exe_header(exe_data)
    warnings = validate_exe_header(header, len(exe_data))
    game_id = canonical_serial(boot_path)

    exe_name = Path(boot_path).name
    exe_out = (Path(args.extract_dir).resolve() if args.extract_dir else project_root.parent / "extracted" / game_name) / exe_name

    game_toml = build_game_toml(
        name=game_name,
        game_id=game_id,
        exe_path=exe_out,
        disc_path=source.disc_path,
        data_path=source.data_path,
        header=header,
        project_root=project_root,
        game_dir=game_dir,
        debug_port=debug_port,
        no_disc_hash=args.no_disc_hash,
    )
    seeds_txt = build_seeds_txt(game_name, game_id)
    if args.dry_run:
        exe_status = planned_bytes_status(exe_out, exe_data)
        toml_status = planned_text_status(game_dir / "game.toml", game_toml)
        seeds_status = planned_text_status(game_dir / "seeds.txt", seeds_txt)
    else:
        exe_status = write_bytes_if_needed(exe_out, exe_data, args.force)
        toml_status = write_text_if_needed(game_dir / "game.toml", game_toml, args.force)
        seeds_status = write_text_if_needed(game_dir / "seeds.txt", seeds_txt, args.force)

    action = "Inspected" if args.dry_run else "Prepared"
    print(f"{action} {game_name} ({game_id})")
    print(f"  disc:       {source.disc_path}")
    print(f"  data track: {source.data_path}")
    print(f"  boot exe:   {exe_out} [{exe_status}]")
    print(f"  entry_pc:   {hex32(header.initial_pc)}")
    print(f"  load_addr:  {hex32(header.load_address)}")
    print(f"  text_size:  {hex32(header.file_size)}")
    for warning in warnings:
        print(f"  warning:    {warning}")
    print(f"  game.toml:  {game_dir / 'game.toml'} [{toml_status}]")
    print(f"  seeds:      {game_dir / 'seeds.txt'} [{seeds_status}]")
    print()
    print("Next step:")
    print(f"  recompiler/build/psxrecomp-game.exe --config {as_posix_rel(game_dir / 'game.toml', project_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

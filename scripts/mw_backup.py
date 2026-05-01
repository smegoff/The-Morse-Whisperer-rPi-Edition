#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path


APP_DIR = Path("/opt/morse-whisperer-pi")
BACKUP_DIR = APP_DIR / "backups"

FILES_TO_BACKUP = [
    APP_DIR / "morse_whisperer_pi.py",
    APP_DIR / "morse_whisperer_splash.py",
    APP_DIR / "config.json",
    APP_DIR / "mw_health.py",
    APP_DIR / "start_morse_whisperer.sh",
    Path("/usr/local/bin/mw-health"),
    Path("/etc/systemd/system/morse-whisperer.service"),
    Path("/boot/config.txt"),
    Path("/boot/firmware/config.txt"),
]


def run_cmd(cmd: list[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip()
    except Exception as exc:
        return 999, str(exc)


def safe_rel(path: Path) -> Path:
    """
    Preserve useful path context inside the tarball without using absolute paths.
    """
    path = Path(path)

    if path.is_absolute():
        return Path("rootfs") / Path(str(path).lstrip("/"))

    return path


def copy_if_exists(src: Path, dest_root: Path, manifest_lines: list[str]) -> None:
    src = Path(src)

    if not src.exists():
        manifest_lines.append(f"MISSING {src}")
        return

    rel = safe_rel(src)
    dst = dest_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        manifest_lines.append(f"DIR     {src} -> {rel}")
    else:
        shutil.copy2(src, dst)
        manifest_lines.append(f"FILE    {src} -> {rel}")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="replace")


def collect_command_outputs(dest_root: Path, manifest_lines: list[str]) -> None:
    commands = {
        "systemctl-status.txt": ["systemctl", "status", "morse-whisperer.service", "--no-pager"],
        "systemctl-is-enabled.txt": ["systemctl", "is-enabled", "morse-whisperer.service"],
        "systemctl-is-active.txt": ["systemctl", "is-active", "morse-whisperer.service"],
        "journalctl-recent.txt": ["journalctl", "-u", "morse-whisperer.service", "-n", "180", "--no-pager"],
        "mw-health.txt": ["mw-health"],
        "ls-dev-fb-input.txt": ["bash", "-lc", "ls -l /dev/fb* /dev/input/* 2>/dev/null || true"],
        "arecord-list.txt": ["arecord", "-l"],
        "dmesg-lcd-audio-tail.txt": [
            "bash",
            "-lc",
            "dmesg | grep -Ei 'ads7846|xpt2046|touch|stmpe|spi0|ili934|fbtft|fb1|audio|usb' | tail -220 || true",
        ],
        "boot-overlay-lines.txt": [
            "bash",
            "-lc",
            "grep -nEi 'dtoverlay=(pitft28|tft9341|ads7846)|Morse Whisperer|dtparam=spi' /boot/config.txt /boot/firmware/config.txt 2>/dev/null || true",
        ],
    }

    out_dir = dest_root / "command-output"
    out_dir.mkdir(parents=True, exist_ok=True)

    for filename, cmd in commands.items():
        rc, out = run_cmd(cmd)
        write_text(out_dir / filename, f"$ {' '.join(cmd)}\nRC={rc}\n\n{out}\n")
        manifest_lines.append(f"CMD     {filename} rc={rc}")

    log = Path("/var/log/morse-whisperer/service.log")
    if log.exists():
        rc, out = run_cmd(["tail", "-300", str(log)])
        write_text(out_dir / "service-log-tail.txt", f"$ tail -300 {log}\nRC={rc}\n\n{out}\n")
        manifest_lines.append("CMD     service-log-tail.txt")

    err = Path("/var/log/morse-whisperer/service.err")
    if err.exists():
        rc, out = run_cmd(["tail", "-200", str(err)])
        write_text(out_dir / "service-err-tail.txt", f"$ tail -200 {err}\nRC={rc}\n\n{out}\n")
        manifest_lines.append("CMD     service-err-tail.txt")


def collect_metadata(dest_root: Path, manifest_lines: list[str]) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    meta = [
        "The Morse Whisperer Appliance Backup",
        "====================================",
        f"Created: {now}",
        f"Hostname: {socket.gethostname()}",
        f"User: uid={os.getuid()} euid={os.geteuid()}",
        f"Platform: {platform.platform()}",
        f"Python: {platform.python_version()}",
        f"App dir: {APP_DIR}",
        "",
    ]

    for cmd_name, cmd in [
        ("uname", ["uname", "-a"]),
        ("os-release", ["bash", "-lc", "cat /etc/os-release 2>/dev/null || true"]),
        ("disk", ["df", "-h", "/opt", "/boot", "/boot/firmware"]),
        ("git-status", ["bash", "-lc", f"cd {APP_DIR} && git status --short 2>/dev/null || true"]),
        ("git-rev", ["bash", "-lc", f"cd {APP_DIR} && git rev-parse --short HEAD 2>/dev/null || true"]),
    ]:
        rc, out = run_cmd(cmd)
        meta.append(f"--- {cmd_name} rc={rc} ---")
        meta.append(out)
        meta.append("")

    write_text(dest_root / "metadata.txt", "\n".join(meta))
    manifest_lines.append("META    metadata.txt")


def make_tarball(source_dir: Path, output_path: Path) -> None:
    with tarfile.open(output_path, "w:gz") as tar:
        tar.add(source_dir, arcname=output_path.stem)


def prune_old_backups(keep: int) -> None:
    if keep <= 0:
        return

    backups = sorted(BACKUP_DIR.glob("mw-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)

    for old in backups[keep:]:
        try:
            old.unlink()
            print(f"Pruned old backup: {old}")
        except Exception as exc:
            print(f"WARNING: could not prune {old}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a The Morse Whisperer appliance backup")
    parser.add_argument("--keep", type=int, default=20, help="Keep the newest N backups, default 20. Use 0 to disable pruning.")
    parser.add_argument("--name", default="", help="Optional label to include in backup filename")
    args = parser.parse_args()

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    label = ""

    if args.name:
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in args.name.strip())
        safe = "-".join(part for part in safe.split("-") if part)
        if safe:
            label = f"-{safe}"

    output = BACKUP_DIR / f"mw-backup-{stamp}{label}.tar.gz"

    manifest_lines: list[str] = []

    with tempfile.TemporaryDirectory(prefix="mw-backup-") as tmp:
        tmp_root = Path(tmp) / f"mw-backup-{stamp}{label}"
        tmp_root.mkdir(parents=True, exist_ok=True)

        collect_metadata(tmp_root, manifest_lines)

        for src in FILES_TO_BACKUP:
            copy_if_exists(src, tmp_root, manifest_lines)

        # Capture a compact list of existing known-good snapshots, but do not
        # archive every historical backup file into this backup.
        known_good = sorted(APP_DIR.glob("*known-good*"))
        known_good_text = "\n".join(str(p) for p in known_good) + ("\n" if known_good else "")
        write_text(tmp_root / "known-good-files.txt", known_good_text)
        manifest_lines.append(f"META    known-good-files.txt count={len(known_good)}")

        collect_command_outputs(tmp_root, manifest_lines)

        write_text(tmp_root / "MANIFEST.txt", "\n".join(manifest_lines) + "\n")

        make_tarball(tmp_root, output)

    print("============================================================")
    print("The Morse Whisperer backup complete")
    print("============================================================")
    print(f"Backup: {output}")
    print(f"Size:   {output.stat().st_size / 1024.0:.1f} KiB")
    print("============================================================")

    prune_old_backups(args.keep)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

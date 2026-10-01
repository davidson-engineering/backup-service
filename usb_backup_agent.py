#!/usr/bin/env python3
"""Snapshot the configured source directories onto the mounted backup drive.

Each run copies every source into a new timestamped directory under
<dest>/<name>/. Files unchanged since the previous snapshot are hard-linked to
it (rsync --link-dest), so every snapshot is a complete, browsable copy while
only changed files take up new space. After a snapshot completes, the oldest
ones beyond `keep` are pruned.

Exits non-zero if any source failed, so systemd marks the run as failed.
"""
import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().with_name("backup_config.yaml")
DEFAULT_KEEP = 30
TOP_LEVEL_KEYS = {"keep", "sources"}
SOURCE_KEYS = {"name", "path", "exclude", "require_mount"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")
PARTIAL_SUFFIX = ".partial"

# rsync(1) exit values that still produce a usable snapshot
RSYNC_PARTIAL = 23   # some files could not be transferred
RSYNC_VANISHED = 24  # some source files vanished during the transfer
RSYNC_STATS_RE = re.compile(
    r"^(Number of regular files transferred|Total file size|Total transferred file size): ([\d,]+)", re.M
)

log = logging.getLogger("usb_backup_agent")


class ConfigError(Exception):
    pass


class LevelPrefixFormatter(logging.Formatter):
    """Prefix warnings and errors with their level; leave plain progress messages bare."""

    def format(self, record):
        message = super().format(record)
        return message if record.levelno <= logging.INFO else f"{record.levelname}: {message}"


def load_config(path):
    """Return (keep, sources) from the YAML config, rejecting anything unexpected."""
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ConfigError("top level must be a mapping")
    unknown = set(config) - TOP_LEVEL_KEYS
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(sorted(unknown))}")

    keep = config.get("keep", DEFAULT_KEEP)
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        raise ConfigError("keep must be a positive integer")

    raw_sources = config.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("sources must be a non-empty list")

    sources = []
    for i, src in enumerate(raw_sources):
        where = f"sources[{i}]"
        if not isinstance(src, dict):
            raise ConfigError(f"{where} must be a mapping")
        unknown = set(src) - SOURCE_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown keys: {', '.join(sorted(unknown))}")
        name = src.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError(f"{where}: name must be a plain folder name (letters, digits, . _ -)")
        if any(s["name"] == name for s in sources):
            raise ConfigError(f"{where}: duplicate name {name!r}")
        path = src.get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ConfigError(f"{where}: path must be an absolute path")
        exclude = src.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
            raise ConfigError(f"{where}: exclude must be a list of strings")
        require_mount = src.get("require_mount", True)
        if not isinstance(require_mount, bool):
            raise ConfigError(f"{where}: require_mount must be true or false")
        sources.append({"name": name, "path": path, "exclude": exclude, "require_mount": require_mount})
    return keep, sources


def source_problem(src):
    """Return why a source must not be backed up right now, or None if it is fine.

    An unmounted network share leaves behind an empty mount point directory;
    snapshotting that would record the share as empty.
    """
    path = src["path"]
    if not os.path.isdir(path):
        return f"{path} does not exist or is not a directory"
    if src["require_mount"] and not os.path.ismount(path):
        return (f"{path} is not a mount point - is the share mounted? "
                "(set require_mount: false for a plain directory)")
    return None


def snapshot_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def completed_snapshots(base):
    return sorted(p for p in base.iterdir() if p.is_dir() and SNAPSHOT_RE.match(p.name))


def human_size(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def summarize(rsync_stats):
    """Condense rsync --stats output into one line, or return it as is if the format is unexpected."""
    stats = {key: int(value.replace(",", "")) for key, value in RSYNC_STATS_RE.findall(rsync_stats)}
    try:
        changed = stats["Number of regular files transferred"]
        copied = stats["Total transferred file size"]
        total = stats["Total file size"]
    except KeyError:
        return rsync_stats.strip() or "snapshot complete"
    files = "file" if changed == 1 else "files"
    return f"{changed} changed {files} copied ({human_size(copied)}), snapshot total {human_size(total)}"


def snapshot(src, dest, stamp, keep):
    """Take one snapshot of src under dest/<name>/<stamp>. Return True on full success."""
    name = src["name"]
    base = dest / name
    base.mkdir(exist_ok=True)
    for stale in base.glob("*" + PARTIAL_SUFFIX):
        log.info("Removing incomplete snapshot %s", stale)
        shutil.rmtree(stale)

    previous = completed_snapshots(base)
    final = base / stamp
    if final.exists():
        raise FileExistsError(f"snapshot {final} already exists")
    work = base / (stamp + PARTIAL_SUFFIX)

    cmd = ["rsync", "-aAX", "--stats"]
    if previous:
        cmd.append(f"--link-dest={previous[-1]}")
    cmd += [f"--exclude={pattern}" for pattern in src["exclude"]]
    cmd += [f"{src['path']}/", f"{work}/"]

    log.info("Backing up %s -> %s", src["path"], final)
    # stdout only carries the --stats summary; rsync's errors go straight to stderr.
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=False)
    rc = result.returncode
    if rc not in (0, RSYNC_PARTIAL, RSYNC_VANISHED) or not work.is_dir():
        log.error("%s: rsync failed with exit code %d; discarding the incomplete snapshot", name, rc)
        shutil.rmtree(work, ignore_errors=True)
        return False

    work.rename(final)
    log.info("%s: %s", name, summarize(result.stdout))
    if rc == RSYNC_VANISHED:
        log.warning("%s: some files vanished during the transfer", name)
    elif rc == RSYNC_PARTIAL:
        log.error("%s: some files could not be transferred (see rsync errors above); "
                  "kept the snapshot without them", name)

    for old in completed_snapshots(base)[:-keep]:
        log.info("Pruning old snapshot %s", old)
        shutil.rmtree(old)
    return rc != RSYNC_PARTIAL


def main(argv=None):
    parser = argparse.ArgumentParser(description="Snapshot the configured sources onto the backup drive.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help=f"default: {DEFAULT_CONFIG}")
    parser.add_argument("--dest", type=Path, required=True, help="mount point of the backup drive")
    args = parser.parse_args(argv)

    handler = logging.StreamHandler()
    handler.setFormatter(LevelPrefixFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    try:
        keep, sources = load_config(args.config)
    except (OSError, yaml.YAMLError, ConfigError) as e:
        log.error("Invalid config %s: %s", args.config, e)
        return 1
    if not os.path.ismount(args.dest):
        log.error("%s is not a mount point - is the backup drive mounted?", args.dest)
        return 1

    stamp = snapshot_stamp()
    failed = []
    for src in sources:
        problem = source_problem(src)
        if problem:
            log.error("Skipping %s: %s", src["name"], problem)
            failed.append(src["name"])
            continue
        try:
            if not snapshot(src, args.dest, stamp, keep):
                failed.append(src["name"])
        except Exception:  # one broken source must not stop the others
            log.exception("Backup of %s failed", src["name"])
            failed.append(src["name"])

    if failed:
        log.error("Backup finished with failures: %s", ", ".join(failed))
        return 1
    log.info("All %d sources backed up (snapshot %s)", len(sources), stamp)
    return 0


if __name__ == "__main__":
    sys.exit(main())

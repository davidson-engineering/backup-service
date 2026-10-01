"""Tests for usb_backup_agent, run against real rsync in temporary directories.

Mount points are simulated by patching os.path.ismount, so the tests run
unprivileged. They need GNU rsync (the agent uses -A/-X), so on macOS run them
in a Linux container.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usb_backup_agent as agent  # noqa: E402


def gnu_rsync():
    try:
        out = subprocess.run(["rsync", "--version"], capture_output=True, text=True, check=False).stdout
    except FileNotFoundError:
        return False
    return "rsync  version" in out


pytestmark = pytest.mark.skipif(not gnu_rsync(), reason="needs GNU rsync")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A share with a few files, a drive to back up to, and helpers to run the agent."""

    class Env:
        share = tmp_path / "share"
        drive = tmp_path / "drive"
        config = tmp_path / "backup_config.yaml"
        mounted = set()
        stamps = iter(f"2026-10-{day:02d}T020000Z" for day in range(1, 32))

        def write_config(self, text=None, keep=30, **source_opts):
            if text is None:
                opts = "".join(f"\n    {k}: {v}" for k, v in source_opts.items())
                text = f"keep: {keep}\nsources:\n  - name: share\n    path: {self.share}{opts}\n"
            self.config.write_text(text)

        def run(self):
            return agent.main(["--config", str(self.config), "--dest", str(self.drive)])

        def snapshots(self, name="share"):
            return [p.name for p in agent.completed_snapshots(self.drive / name)]

    e = Env()
    e.share.mkdir()
    e.drive.mkdir()
    for name in ("a.txt", "b.txt"):
        (e.share / name).write_text(f"contents of {name}")
    e.mounted.update({str(e.share), str(e.drive)})
    e.write_config()
    monkeypatch.setattr(agent.os.path, "ismount", lambda p: str(p) in e.mounted)
    monkeypatch.setattr(agent, "snapshot_stamp", lambda: next(e.stamps))
    return e


def fake_rsync(tmp_path, monkeypatch, exit_code):
    """Put an rsync on PATH that writes one file into the target, then exits with exit_code."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "rsync"
    script.write_text(f'#!/bin/sh\nfor t; do :; done\nmkdir -p "$t" && echo x > "$t/copied"\nexit {exit_code}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{agent.os.environ['PATH']}")


def test_first_run_creates_a_complete_snapshot(env):
    assert env.run() == 0
    assert env.snapshots() == ["2026-10-01T020000Z"]
    snap = env.drive / "share" / "2026-10-01T020000Z"
    assert (snap / "a.txt").read_text() == "contents of a.txt"
    assert not list((env.drive / "share").glob("*.partial"))


def test_unchanged_files_are_hard_linked_to_the_previous_snapshot(env):
    env.run()
    (env.share / "b.txt").write_text("changed")
    assert env.run() == 0
    first, second = (env.drive / "share" / s for s in env.snapshots())
    assert (first / "a.txt").stat().st_ino == (second / "a.txt").stat().st_ino
    assert (first / "b.txt").read_text() == "contents of b.txt"
    assert (second / "b.txt").read_text() == "changed"


def test_each_snapshot_logs_a_one_line_summary(env, caplog):
    caplog.set_level("INFO")
    env.run()
    (env.share / "b.txt").write_text("changed!")
    env.run()
    assert "share: 1 changed file copied (8 B), snapshot total 25 B" in caplog.text


def test_file_deleted_from_share_survives_in_older_snapshot(env):
    env.run()
    (env.share / "a.txt").unlink()
    env.run()
    first, second = (env.drive / "share" / s for s in env.snapshots())
    assert (first / "a.txt").exists()
    assert not (second / "a.txt").exists()


def test_unmounted_share_is_refused_and_existing_snapshots_are_kept(env):
    env.run()
    # The share drops: its mount point is still there, but empty.
    env.mounted.discard(str(env.share))
    for f in env.share.iterdir():
        f.unlink()
    assert env.run() == 1
    assert env.snapshots() == ["2026-10-01T020000Z"]
    assert (env.drive / "share" / "2026-10-01T020000Z" / "a.txt").exists()


def test_missing_source_fails_the_run_but_other_sources_are_backed_up(env):
    env.write_config(
        f"sources:\n  - name: gone\n    path: {env.share}-missing\n  - name: share\n    path: {env.share}\n"
    )
    assert env.run() == 1
    assert env.snapshots("share") == ["2026-10-01T020000Z"]
    assert not (env.drive / "gone").exists()


def test_require_mount_false_allows_a_plain_directory(env):
    env.mounted.discard(str(env.share))
    env.write_config(require_mount="false")
    assert env.run() == 0
    assert env.snapshots() == ["2026-10-01T020000Z"]


def test_unmounted_drive_is_refused_without_writing_anything(env):
    env.mounted.discard(str(env.drive))
    assert env.run() == 1
    assert list(env.drive.iterdir()) == []


def test_exclude_patterns_are_applied(env):
    (env.share / "scratch.tmp").write_text("junk")
    env.write_config(exclude='["*.tmp"]')
    env.run()
    snap = env.drive / "share" / "2026-10-01T020000Z"
    assert (snap / "a.txt").exists()
    assert not (snap / "scratch.tmp").exists()


def test_only_the_newest_snapshots_are_kept(env):
    env.write_config(keep=2)
    for _ in range(4):
        assert env.run() == 0
    assert env.snapshots() == ["2026-10-03T020000Z", "2026-10-04T020000Z"]


def test_failed_rsync_discards_the_incomplete_snapshot(env, tmp_path, monkeypatch):
    env.run()
    fake_rsync(tmp_path, monkeypatch, exit_code=11)  # 11 = error in file I/O, e.g. drive full
    assert env.run() == 1
    assert env.snapshots() == ["2026-10-01T020000Z"]
    assert not list((env.drive / "share").glob("*.partial"))


def test_partial_transfer_keeps_the_snapshot_but_fails_the_run(env, tmp_path, monkeypatch):
    fake_rsync(tmp_path, monkeypatch, exit_code=agent.RSYNC_PARTIAL)
    assert env.run() == 1
    assert env.snapshots() == ["2026-10-01T020000Z"]


def test_vanished_files_count_as_success(env, tmp_path, monkeypatch):
    fake_rsync(tmp_path, monkeypatch, exit_code=agent.RSYNC_VANISHED)
    assert env.run() == 0
    assert env.snapshots() == ["2026-10-01T020000Z"]


def test_leftover_partial_snapshot_from_a_crash_is_removed(env):
    leftover = env.drive / "share" / "2026-09-30T020000Z.partial"
    leftover.mkdir(parents=True)
    (leftover / "half-copied").write_text("x")
    assert env.run() == 0
    assert not leftover.exists()
    assert env.snapshots() == ["2026-10-01T020000Z"]


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("logfile: /var/log/x.log\nsources: []\n", "unknown keys: logfile"),
        ("sources:\n  - name: ../etc\n    path: /srv\n", "name must be a plain folder name"),
        ("sources:\n  - name: /etc\n    path: /srv\n", "name must be a plain folder name"),
        ("sources:\n  - name: a\n    path: /srv\n  - name: a\n    path: /opt\n", "duplicate name"),
        ("sources:\n  - name: a\n    path: relative/dir\n", "path must be an absolute path"),
        ("sources:\n  - name: a\n    path: /srv\n    excludes: ['*.tmp']\n", "unknown keys: excludes"),
        ("keep: 0\nsources:\n  - name: a\n    path: /srv\n", "keep must be a positive integer"),
        ("sources: []\n", "sources must be a non-empty list"),
    ],
)
def test_invalid_config_is_rejected_before_touching_the_drive(env, caplog, config, error):
    env.write_config(config)
    assert env.run() == 1
    assert error in caplog.text
    assert list(env.drive.iterdir()) == []

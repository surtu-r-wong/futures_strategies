from __future__ import annotations

from pathlib import Path
import os
import shlex
import stat
import subprocess
import sys

import pytest

import cta_carry.provenance as carry_provenance
from cta_carry.provenance import GitState, capture_git_state


@pytest.fixture(autouse=True)
def _isolated_git_environment(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "git-template"
    template.mkdir()
    _git(repo, "init", f"--template={template}")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    return repo


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def test_split_git_path_rejects_unsafe_paths():
    for encoded_path in (
        b"",
        b"/absolute",
        b".",
        b"..",
        b"nested//payload.txt",
        b"nested/./payload.txt",
        b"nested/../payload.txt",
        b"bad\0path",
    ):
        with pytest.raises(OSError, match="unsafe Git path"):
            carry_provenance._split_git_path(encoded_path)


def test_capture_git_state_reports_clean_commit(tmp_path):
    repo = _repo(tmp_path)

    state = capture_git_state(repo)

    assert state == GitState(
        version=_git(repo, "rev-parse", "HEAD"),
        dirty=False,
        diff_sha256="",
    )


def test_git_test_helper_ignores_fake_global_config(monkeypatch, tmp_path):
    fake_global = tmp_path / "global.gitconfig"
    fake_global.write_text(
        "[commit]\n\tgpgsign = true\n"
        "[custom]\n\tprobe = leaked\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_global))

    repo = _repo(tmp_path)

    assert _git(repo, "log", "-1", "--format=%an") == "Test User"
    with pytest.raises(subprocess.CalledProcessError):
        _git(repo, "config", "--get", "custom.probe")


def test_git_command_forces_locale_and_preserves_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CARRY_PROVENANCE_SENTINEL", "kept")
    captured = {}

    class Completed:
        stdout = b"ok"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(carry_provenance.subprocess, "run", fake_run)

    assert carry_provenance._git(tmp_path, "status") == b"ok"
    assert captured["command"] == ["git", "status"]
    env = captured["env"]
    assert env["LC_ALL"] == "C"
    assert env["LANG"] == "C"
    assert env["CARRY_PROVENANCE_SENTINEL"] == "kept"


def test_capture_git_state_ignores_ignored_untracked_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("output/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore output")
    clean = capture_git_state(repo)
    output = repo / "output"
    output.mkdir()
    (output / "report.xlsx").write_bytes(b"ignored")

    assert capture_git_state(repo) == clean


@pytest.mark.parametrize("staged", [False, True])
def test_capture_git_state_hashes_tracked_changes(tmp_path, staged):
    repo = _repo(tmp_path)
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    if staged:
        _git(repo, "add", "tracked.txt")

    state = capture_git_state(repo)

    assert state.dirty is True
    assert len(state.diff_sha256) == 64
    assert capture_git_state(repo) == state

    (repo / "tracked.txt").write_text("three\n", encoding="utf-8")
    changed = capture_git_state(repo)

    assert state.diff_sha256 != changed.diff_sha256


@pytest.mark.skipif(os.name == "nt", reason="executable mode unsupported")
def test_capture_git_state_hashes_unstaged_executable_mode(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "config", "core.fileMode", "true")
    path = repo / "tracked.txt"
    path.write_text("dirty\n", encoding="utf-8")
    original_mode = path.stat().st_mode
    path.chmod(original_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    nonexecutable = capture_git_state(repo)
    assert nonexecutable.dirty is True

    path.chmod(original_mode | stat.S_IXUSR)
    if not path.stat().st_mode & stat.S_IXUSR:
        pytest.skip("filesystem does not preserve executable mode")
    executable = capture_git_state(repo)

    assert executable.dirty is True
    assert executable.diff_sha256 != nonexecutable.diff_sha256


def test_capture_git_state_hashes_staged_and_unstaged_binary_changes(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "tracked.txt"
    path.write_bytes(b"\x00staged")
    _git(repo, "add", "tracked.txt")

    staged_state = capture_git_state(repo)
    assert capture_git_state(repo) == staged_state

    path.write_bytes(b"\x00worktree")
    mixed_state = capture_git_state(repo)

    assert staged_state.dirty is True
    assert mixed_state.dirty is True
    assert staged_state.diff_sha256 != mixed_state.diff_sha256


def test_capture_git_state_ignores_git_presentation_config(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "mv", "tracked.txt", "renamed.txt")
    (repo / "renamed.txt").write_text("two\n", encoding="utf-8")

    baseline = capture_git_state(repo)

    assert baseline.dirty is True
    assert capture_git_state(repo) == baseline

    for key, value in (
        ("diff.noprefix", "true"),
        ("core.abbrev", "4"),
        ("diff.renames", "false"),
        ("status.renames", "false"),
        ("diff.algorithm", "patience"),
    ):
        _git(repo, "config", key, value)

    configured = capture_git_state(repo)

    assert configured == baseline


def test_capture_git_state_ignores_diff_context_presentation_config(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "tracked.txt"
    original = [f"line-{index}" for index in range(30)]
    original[3] = ""
    path.write_text("\n".join(original) + "\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "add multi-hunk content")

    changed = original.copy()
    changed[1] = "changed-first"
    changed[20] = "changed-second"
    path.write_text("\n".join(changed) + "\n", encoding="utf-8")
    baseline = capture_git_state(repo)
    assert baseline.dirty is True
    assert capture_git_state(repo) == baseline

    _git(repo, "config", "diff.interHunkContext", "20")
    _git(repo, "config", "diff.suppressBlankEmpty", "true")

    assert capture_git_state(repo) == baseline


def test_capture_git_state_ignores_core_quote_path_config(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "café.txt"
    path.write_text("first\n", encoding="utf-8")
    _git(repo, "add", path.name)
    _git(repo, "commit", "-m", "add non-ascii path")

    path.write_text("second\n", encoding="utf-8")
    baseline = capture_git_state(repo)
    assert baseline.dirty is True

    _git(repo, "config", "core.quotePath", "false")

    assert capture_git_state(repo) == baseline


def test_capture_git_state_ignores_diff_order_file_config(tmp_path):
    repo = _repo(tmp_path)
    for name in ("alpha.txt", "omega.txt"):
        (repo / name).write_text("first\n", encoding="utf-8")
    _git(repo, "add", "alpha.txt", "omega.txt")
    _git(repo, "commit", "-m", "add ordered paths")

    for name in ("alpha.txt", "omega.txt"):
        (repo / name).write_text("second\n", encoding="utf-8")
    baseline = capture_git_state(repo)
    assert baseline.dirty is True

    order_file = tmp_path / "diff-order"
    order_file.write_text("omega.txt\nalpha.txt\n", encoding="utf-8")
    _git(repo, "config", "diff.orderFile", str(order_file))

    assert capture_git_state(repo) == baseline


def test_capture_git_state_distinguishes_staged_from_worktree_content(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "tracked.txt"
    path.write_text("staged-a\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    path.write_text("final\n", encoding="utf-8")

    first = capture_git_state(repo)
    assert capture_git_state(repo) == first

    path.write_text("staged-b\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    path.write_text("final\n", encoding="utf-8")

    second = capture_git_state(repo)

    assert first.dirty is True
    assert second.dirty is True
    assert first.diff_sha256 != second.diff_sha256


def test_capture_git_state_ignores_custom_diff_driver_presentation(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "tracked.txt"
    attributes = repo / ".gitattributes"
    attributes.write_text("*.txt diff=custom\n", encoding="utf-8")
    original = ["section alpha", *(f"line-{index}" for index in range(20))]
    path.write_text("\n".join(original) + "\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes", "tracked.txt")
    _git(repo, "commit", "-m", "configure custom diff driver")

    changed = original.copy()
    changed[15] = "changed"
    path.write_text("\n".join(changed) + "\n", encoding="utf-8")
    baseline = capture_git_state(repo)
    assert baseline.dirty is True

    _git(repo, "config", "diff.custom.xfuncname", "^section")
    configured = capture_git_state(repo)

    assert configured == baseline


def test_capture_git_state_does_not_run_textconv(tmp_path):
    repo = _repo(tmp_path)
    attributes = repo / ".gitattributes"
    attributes.write_text("*.txt diff=constant\n", encoding="utf-8")
    textconv = repo / "fail_textconv.py"
    textconv.write_text("raise SystemExit(1)\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes", textconv.name)
    _git(repo, "commit", "-m", "configure textconv")
    _git(
        repo,
        "config",
        "diff.constant.textconv",
        shlex.join([sys.executable, str(textconv)]),
    )
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")

    state = capture_git_state(repo)

    assert state.dirty is True
    assert len(state.diff_sha256) == 64


def test_capture_git_state_hashes_untracked_names_and_contents(tmp_path):
    repo = _repo(tmp_path)
    original = repo / "new.txt"
    original.write_text("first\n", encoding="utf-8")
    original_state = capture_git_state(repo)
    assert capture_git_state(repo) == original_state

    renamed = repo / "renamed.txt"
    original.rename(renamed)
    renamed_state = capture_git_state(repo)
    assert capture_git_state(repo) == renamed_state

    renamed.write_text("second\n", encoding="utf-8")
    changed_content_state = capture_git_state(repo)

    assert original_state.dirty is True
    assert original_state.diff_sha256 != renamed_state.diff_sha256
    assert renamed_state.diff_sha256 != changed_content_state.diff_sha256


def test_capture_git_state_hashes_untracked_symlink_itself(tmp_path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("first\n", encoding="utf-8")
    link = repo / "link.txt"
    _symlink_or_skip(link, outside)
    first = capture_git_state(repo)
    outside.write_text("second\n", encoding="utf-8")
    second = capture_git_state(repo)

    assert first.dirty is True
    assert first.diff_sha256 == second.diff_sha256


def test_capture_git_state_rejects_untracked_parent_symlink_swap(
    monkeypatch,
    tmp_path,
):
    repo = _repo(tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    (nested / "payload.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_payload = outside / "payload.txt"
    outside_payload.write_text("outside sentinel\n", encoding="utf-8")
    outside_directory_stat = outside.stat()
    outside_payload_stat = outside_payload.stat()
    probe = tmp_path / "probe"
    _symlink_or_skip(probe, outside)
    probe.unlink()
    displaced = tmp_path / "displaced"
    swapped = False

    real_git = carry_provenance._git

    def swap_after_untracked_enumeration(repo_root, *args):
        nonlocal swapped
        output = real_git(repo_root, *args)
        if not swapped and args[:2] == ("ls-files", "--others"):
            nested.rename(displaced)
            nested.symlink_to(outside, target_is_directory=True)
            swapped = True
        return output

    real_open = carry_provenance.os.open
    real_read = carry_provenance.os.read
    outside_entry_opens = []
    outside_reads = []

    def audited_open(candidate, flags, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        outside_relative_open = False
        if dir_fd is not None and os.fsdecode(candidate) == "payload.txt":
            parent_stat = os.fstat(dir_fd)
            outside_relative_open = (
                parent_stat.st_dev == outside_directory_stat.st_dev
                and parent_stat.st_ino == outside_directory_stat.st_ino
            )
        if (
            dir_fd is None
            and os.fsdecode(candidate) == os.fspath(nested / "payload.txt")
        ) or outside_relative_open:
            outside_entry_opens.append(candidate)
        return real_open(candidate, flags, *args, **kwargs)

    def audited_read(descriptor, size):
        opened = os.fstat(descriptor)
        if (
            opened.st_dev == outside_payload_stat.st_dev
            and opened.st_ino == outside_payload_stat.st_ino
        ):
            outside_reads.append(descriptor)
        return real_read(descriptor, size)

    monkeypatch.setattr(carry_provenance, "_git", swap_after_untracked_enumeration)
    monkeypatch.setattr(carry_provenance.os, "open", audited_open)
    monkeypatch.setattr(carry_provenance.os, "read", audited_read)

    state = capture_git_state(repo)

    assert swapped is True
    assert outside_entry_opens == []
    assert outside_reads == []
    assert state == GitState(
        version="unknown",
        dirty="unknown",
        diff_sha256="unknown",
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_hash_worktree_path_rejects_fifo_without_opening(monkeypatch, tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    root_fd = carry_provenance._open_repo_root(tmp_path)
    try:
        def unexpected_open(*args, **kwargs):
            pytest.fail("special entry must be rejected before opening")

        monkeypatch.setattr(carry_provenance.os, "open", unexpected_open)

        with pytest.raises(OSError, match="unsupported worktree entry"):
            carry_provenance._hash_worktree_path(root_fd, b"pipe")
    finally:
        os.close(root_fd)


def test_capture_git_state_reports_unknown_for_untracked_hash_error(
    monkeypatch,
    tmp_path,
):
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("content\n", encoding="utf-8")

    def changed_entry(_root_fd, _encoded_path):
        raise OSError("untracked entry changed")

    monkeypatch.setattr(carry_provenance, "_hash_worktree_path", changed_entry)

    assert capture_git_state(repo) == GitState(
        version="unknown",
        dirty="unknown",
        diff_sha256="unknown",
    )


def test_hash_worktree_path_does_not_read_symlink_swapped_for_file(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "entry.txt"
    outside = tmp_path / "outside.txt"
    path.write_text("inside\n", encoding="utf-8")
    outside.write_text("outside\n", encoding="utf-8")
    probe = tmp_path / "probe"
    _symlink_or_skip(probe, outside)
    probe.unlink()
    root_fd = carry_provenance._open_repo_root(tmp_path)

    real_open = carry_provenance.os.open

    def swap_then_open(candidate, flags, *args, **kwargs):
        path.unlink()
        path.symlink_to(outside)
        return real_open(candidate, flags, *args, **kwargs)

    def unexpected_read(*args, **kwargs):
        pytest.fail("replacement target contents must not be read")

    monkeypatch.setattr(carry_provenance.os, "open", swap_then_open)
    monkeypatch.setattr(carry_provenance.os, "read", unexpected_read)

    try:
        with pytest.raises(OSError):
            carry_provenance._hash_worktree_path(root_fd, b"entry.txt")
    finally:
        os.close(root_fd)


def test_hash_worktree_path_rejects_platform_without_nofollow(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "entry.txt"
    path.write_text("inside\n", encoding="utf-8")
    root_fd = carry_provenance._open_repo_root(tmp_path)
    monkeypatch.delattr(carry_provenance.os, "O_NOFOLLOW", raising=False)

    def unexpected_open(*args, **kwargs):
        pytest.fail("file must not be opened without no-follow support")

    monkeypatch.setattr(carry_provenance.os, "open", unexpected_open)

    try:
        with pytest.raises(OSError, match="no-follow"):
            carry_provenance._hash_worktree_path(root_fd, b"entry.txt")
    finally:
        os.close(root_fd)


def test_capture_git_state_is_explicit_when_git_is_unavailable(
    monkeypatch,
    tmp_path,
):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing_git)

    assert capture_git_state(tmp_path) == GitState(
        version="unknown",
        dirty="unknown",
        diff_sha256="unknown",
    )

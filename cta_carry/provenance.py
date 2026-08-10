from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess


_GIT_TIMEOUT_SECONDS = 2
_SAFE_TRAVERSAL_SUPPORTED = all(
    function in getattr(os, "supports_dir_fd", ())
    for function in (os.open, os.stat, os.readlink)
) and os.stat in getattr(os, "supports_follow_symlinks", ())


def _required_open_flag(name: str) -> int:
    flag = getattr(os, name, None)
    if flag is None:
        raise OSError(f"safe no-follow traversal flag unavailable: {name}")
    return flag


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | _required_open_flag("O_DIRECTORY")
        | _required_open_flag("O_NOFOLLOW")
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _regular_open_flags() -> int:
    return (
        os.O_RDONLY
        | _required_open_flag("O_NOFOLLOW")
        | _required_open_flag("O_NONBLOCK")
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )


def _split_git_path(encoded_path: bytes) -> tuple[bytes, ...]:
    if (
        not encoded_path
        or b"\0" in encoded_path
        or os.path.isabs(encoded_path)
    ):
        raise OSError(f"unsafe Git path: {encoded_path!r}")
    components = tuple(encoded_path.split(b"/"))
    if any(component in (b"", b".", b"..") for component in components):
        raise OSError(f"unsafe Git path: {encoded_path!r}")
    return components


def _lstat_at(parent_fd: int, name: bytes) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _open_repo_root(repo_root: Path) -> int:
    if not _SAFE_TRAVERSAL_SUPPORTED:
        raise OSError("safe descriptor-relative traversal unsupported")
    descriptor = os.open(repo_root, _directory_open_flags())
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError(f"repository root is not a directory: {repo_root}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_parent_directories(
    root_fd: int,
    components: tuple[bytes, ...],
) -> tuple[int, list[int]]:
    parent_fd = root_fd
    opened_descriptors: list[int] = []
    try:
        for component in components[:-1]:
            descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=parent_fd,
            )
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISDIR(opened.st_mode):
                    raise OSError(
                        f"worktree parent is not a directory: {component!r}"
                    )
            except BaseException:
                os.close(descriptor)
                raise
            opened_descriptors.append(descriptor)
            parent_fd = descriptor
        return parent_fd, opened_descriptors
    except BaseException:
        for descriptor in reversed(opened_descriptors):
            os.close(descriptor)
        raise




@dataclass(frozen=True)
class GitState:
    version: str
    dirty: bool | str
    diff_sha256: str


def _git(repo_root: Path, *args: str) -> bytes:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=env,
    ).stdout


def _same_entry_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
    )


def _same_entry_snapshot(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return (
        _same_entry_identity(first, second)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _hash_worktree_path(
    root_fd: int,
    encoded_path: bytes,
    *,
    missing_ok: bool = False,
) -> bytes | None:
    components = _split_git_path(encoded_path)
    display_path = os.fsdecode(encoded_path)
    try:
        parent_fd, opened_descriptors = _open_parent_directories(
            root_fd,
            components,
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise

    name = components[-1]
    try:
        try:
            before = _lstat_at(parent_fd, name)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        digest = hashlib.sha256()

        if stat.S_ISLNK(before.st_mode):
            target = os.fsencode(os.readlink(name, dir_fd=parent_fd))
            after = _lstat_at(parent_fd, name)
            if not _same_entry_snapshot(before, after):
                raise OSError(
                    f"worktree symlink changed while hashing: {display_path}"
                )
            digest.update(b"symlink\0")
            digest.update(target)
            return digest.digest()

        if not stat.S_ISREG(before.st_mode):
            raise OSError(f"unsupported worktree entry type: {display_path}")

        descriptor = os.open(
            name,
            _regular_open_flags(),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not _same_entry_identity(before, opened)
            ):
                raise OSError(
                    f"worktree entry changed before hashing: {display_path}"
                )
            digest.update(b"file\0")
            digest.update(
                b"100755\0"
                if before.st_mode & stat.S_IXUSR
                else b"100644\0"
            )
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after_open = os.fstat(descriptor)
            after_path = _lstat_at(parent_fd, name)
            if (
                not _same_entry_snapshot(before, after_open)
                or not _same_entry_snapshot(before, after_path)
            ):
                raise OSError(
                    f"worktree entry changed while hashing: {display_path}"
                )
            return digest.digest()
        finally:
            os.close(descriptor)
    finally:
        for descriptor in reversed(opened_descriptors):
            os.close(descriptor)


def _fingerprint_dirty_state(
    repo_root: Path,
    root_fd: int,
    status: bytes,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"status\0")
    digest.update(status)
    # Fingerprint stable Git/index/worktree state, not human-readable patch
    # presentation, which arbitrary diff drivers and config can change.
    digest.update(b"staged-index\0")
    index_entries = _git(
        repo_root,
        "ls-files",
        "--stage",
        "--full-name",
        "-z",
        "--",
    ).split(b"\0")
    for entry in sorted(entry for entry in index_entries if entry):
        digest.update(b"index-entry\0")
        digest.update(entry)
        digest.update(b"\0")

    digest.update(b"unstaged-worktree\0")
    unstaged = _git(
        repo_root,
        "ls-files",
        "--modified",
        "--deleted",
        "--full-name",
        "-z",
        "--",
    ).split(b"\0")
    for encoded_path in sorted({path for path in unstaged if path}):
        digest.update(b"tracked-path\0")
        digest.update(encoded_path)
        digest.update(b"\0")
        entry_hash = _hash_worktree_path(
            root_fd,
            encoded_path,
            missing_ok=True,
        )
        if entry_hash is None:
            digest.update(b"deleted\0")
        else:
            digest.update(b"present\0")
            digest.update(entry_hash)

    untracked = _git(
        repo_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    ).split(b"\0")
    for encoded_path in sorted(path for path in untracked if path):
        digest.update(b"untracked\0")
        digest.update(encoded_path)
        digest.update(b"\0")
        entry_hash = _hash_worktree_path(root_fd, encoded_path)
        if entry_hash is None:
            raise OSError(f"untracked path disappeared: {encoded_path!r}")
        digest.update(entry_hash)
    return digest.hexdigest()


def capture_git_state(repo_root: Path) -> GitState:
    try:
        version = _git(repo_root, "rev-parse", "HEAD").decode().strip()
        status = _git(
            repo_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
        )
        if not status:
            return GitState(version=version, dirty=False, diff_sha256="")

        root_fd = _open_repo_root(repo_root)
        try:
            diff_sha256 = _fingerprint_dirty_state(repo_root, root_fd, status)
        finally:
            os.close(root_fd)
        return GitState(
            version=version,
            dirty=True,
            diff_sha256=diff_sha256,
        )
    except (
        OSError,
        UnicodeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return GitState(
            version="unknown",
            dirty="unknown",
            diff_sha256="unknown",
        )

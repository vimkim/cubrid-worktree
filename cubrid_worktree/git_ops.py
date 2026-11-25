"""Git worktree, branch, and metadata operations."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import CliError
from .naming import extract_single_ticket

LEGACY_UNTICKETED_REVIEW_RE = re.compile(r"^review-noticket-pr-([1-9][0-9]*)$", re.IGNORECASE)
PR_NUMBER_URL_RE = re.compile(r"/pull/([1-9][0-9]*)/?$")
PROC_DIR = Path("/proc")


@dataclass(frozen=True)
class WorktreeRecord:
    path: Path
    head: str
    branch: str | None
    detached: bool
    pr_url: str | None
    role: str | None
    ticket: str | None
    is_main: bool = False
    locked: bool = False
    lock_reason: str | None = None
    prunable: bool = False
    prunable_reason: str | None = None

    @property
    def name(self) -> str:
        return self.path.name

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = os.fspath(self.path)
        payload["name"] = self.name
        return payload


def run_git(
    args: list[str],
    *,
    cwd: Path,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CliError("MISSING_COMMAND", "Required command is not installed: git.", fix="Install git and ensure it is on PATH.") from exc

    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout).strip()
        raise CliError(
            "GIT_FAILED",
            f"Git command failed ({' '.join(command)}): {detail or f'exit {result.returncode}'}",
            fix="Inspect the repository and branch state in the control checkout.",
        )
    return result


def ensure_control_worktree(control_dir: Path, parent_dir: Path) -> Path:
    if not control_dir.is_dir():
        raise CliError(
            "MISSING_CONTROL_DIR",
            f"Control worktree does not exist: {control_dir}",
            fix="Pass --control-dir or set CUBRID_WORKTREE_CONTROL_DIR to your develop checkout.",
        )
    if not parent_dir.is_dir():
        raise CliError(
            "MISSING_PARENT_DIR",
            f"Worktree parent directory does not exist: {parent_dir}",
            fix="Create the parent directory or pass --parent-dir.",
        )
    root = Path(run_git(["rev-parse", "--show-toplevel"], cwd=control_dir).stdout.strip()).resolve()
    if root != control_dir.resolve():
        raise CliError(
            "INVALID_CONTROL_DIR",
            f"Control path is not a Git worktree root: {control_dir} (root is {root})",
            fix="Point --control-dir at the root of the control checkout.",
        )
    return root


def _branch_metadata(control_dir: Path) -> dict[str, dict[str, str]]:
    result = run_git(
        ["config", "--local", "--get-regexp", r"^branch\..*\.(pr-url|cubrid-role)$"],
        cwd=control_dir,
        allowed=(0, 1),
    )
    metadata: dict[str, dict[str, str]] = {}
    if result.returncode != 0 or not result.stdout:
        return metadata
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(" ")
        if not key.startswith("branch."):
            continue
        remainder = key.removeprefix("branch.")
        if "." not in remainder:
            continue
        branch_name, attr = remainder.rsplit(".", 1)
        metadata.setdefault(branch_name, {})[attr] = value.strip()
    return metadata


def list_worktrees(control_dir: Path) -> list[WorktreeRecord]:
    result = run_git(["worktree", "list", "--porcelain", "-z"], cwd=control_dir)
    meta = _branch_metadata(control_dir)
    records: list[WorktreeRecord] = []
    current: dict[str, Any] = {}

    def flush() -> None:
        if "path" not in current:
            return
        path = Path(current["path"]).resolve()
        branch = current.get("branch")
        branch_meta = meta.get(branch, {}) if branch else {}
        ticket = extract_single_ticket(path.name) or (extract_single_ticket(branch) if branch else None)
        records.append(
            WorktreeRecord(
                path=path,
                head=current.get("head", ""),
                branch=branch,
                detached=bool(current.get("detached", False)),
                pr_url=branch_meta.get("pr-url"),
                role=branch_meta.get("cubrid-role"),
                ticket=ticket,
                is_main=(len(records) == 0),
                locked=bool(current.get("locked", False)),
                lock_reason=current.get("lock_reason"),
                prunable=bool(current.get("prunable", False)),
                prunable_reason=current.get("prunable_reason"),
            )
        )

    for token in result.stdout.split("\0"):
        if token == "":
            flush()
            current = {}
            continue
        if token.startswith("worktree "):
            current["path"] = token.removeprefix("worktree ")
        elif token.startswith("HEAD "):
            current["head"] = token.removeprefix("HEAD ")
        elif token.startswith("branch refs/heads/"):
            current["branch"] = token.removeprefix("branch refs/heads/")
        elif token == "detached":
            current["detached"] = True
        elif token == "locked":
            current["locked"] = True
        elif token.startswith("locked "):
            current["locked"] = True
            current["lock_reason"] = token.removeprefix("locked ").strip() or None
        elif token == "prunable":
            current["prunable"] = True
        elif token.startswith("prunable "):
            current["prunable"] = True
            current["prunable_reason"] = token.removeprefix("prunable ").strip() or None

    flush()
    return records


def local_branch_exists(control_dir: Path, branch: str) -> bool:
    result = run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=control_dir,
        allowed=(0, 1),
    )
    return result.returncode == 0


def validate_branch_name(control_dir: Path, branch: str) -> None:
    result = run_git(
        ["check-ref-format", "--branch", branch],
        cwd=control_dir,
        allowed=(0, 1, 128),
    )
    if result.returncode != 0:
        raise CliError(
            "INVALID_BRANCH",
            f"Invalid Git branch name: {branch}",
            fix="Choose a valid branch name without spaces or special Git ref syntax.",
            exit_code=2,
        )


def is_target_occupied(
    control_dir: Path,
    parent_dir: Path,
    name: str,
    worktrees: list[WorktreeRecord] | None = None,
) -> bool:
    target_path = parent_dir / name
    if os.path.lexists(target_path):
        return True
    resolved_target = target_path.resolve()
    records = list_worktrees(control_dir) if worktrees is None else worktrees
    if any(record.path == resolved_target for record in records):
        return True
    return local_branch_exists(control_dir, name)


def exact_conflict_error(
    control_dir: Path,
    target_path: Path,
    branch: str,
    worktrees: list[WorktreeRecord] | None = None,
) -> str | None:
    if os.path.lexists(target_path):
        return f"worktree path already exists: {target_path}"
    resolved_target = target_path.resolve()
    records = list_worktrees(control_dir) if worktrees is None else worktrees
    if any(record.path == resolved_target for record in records):
        return f"worktree path is already registered: {target_path}"
    if local_branch_exists(control_dir, branch):
        return f"local branch already exists: {branch}"
    return None


def find_ticket_siblings(
    worktrees: list[WorktreeRecord],
    parent_dir: Path,
    ticket: str | None,
    *,
    exclude_path: Path | None = None,
) -> list[dict[str, str]]:
    if not ticket:
        return []
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(ticket)}(?![A-Za-z0-9])", re.IGNORECASE)
    excluded = exclude_path.resolve() if exclude_path is not None else None
    seen_paths: set[Path] = set()
    siblings: list[dict[str, str]] = []

    for record in worktrees:
        if excluded is not None and record.path == excluded:
            continue
        if pattern.search(record.path.name) or (record.branch and pattern.search(record.branch)):
            seen_paths.add(record.path)
            siblings.append(
                {
                    "source": "registered worktree",
                    "name": record.path.name,
                    "path": os.fspath(record.path),
                    "branch": record.branch or "(detached)",
                    "role": record.role or "",
                }
            )

    if parent_dir.is_dir():
        for child in sorted(parent_dir.iterdir(), key=lambda entry: entry.name.casefold()):
            if not child.is_dir():
                continue
            resolved = child.resolve()
            if excluded is not None and resolved == excluded:
                continue
            if resolved in seen_paths:
                continue
            if pattern.search(child.name):
                siblings.append(
                    {
                        "source": "sibling directory",
                        "name": child.name,
                        "path": os.fspath(child),
                        "branch": "",
                        "role": "",
                    }
                )

    return siblings


def find_existing_pr_worktree(
    worktrees: list[WorktreeRecord],
    pr_number: int,
    pr_url: str,
    ticket: str | None,
) -> WorktreeRecord | None:
    canonical = pr_url.rstrip("/").casefold()
    for record in worktrees:
        if record.pr_url and record.pr_url.rstrip("/").casefold() == canonical:
            return record

    expected_names = {f"PR-{pr_number}".casefold(), f"review-noticket-pr-{pr_number}".casefold()}
    if ticket:
        expected_names.add(f"{ticket}-pr-{pr_number}".casefold())
        expected_names.add(f"review-{ticket}-pr-{pr_number}".casefold())

    for record in worktrees:
        if record.path.name.casefold() in expected_names:
            return record
        if record.branch and record.branch.casefold() in expected_names:
            return record
    return None


def configured_remotes(control_dir: Path) -> set[str]:
    result = run_git(["remote"], cwd=control_dir)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def fetch_base_if_remote(control_dir: Path, base_ref: str) -> dict[str, Any]:
    if "/" not in base_ref:
        return {"status": "skipped", "reason": "local ref", "ref": base_ref}
    remote, branch = base_ref.split("/", 1)
    remotes = configured_remotes(control_dir)
    if remote not in remotes:
        return {"status": "skipped", "reason": f"'{remote}' is not a configured remote", "ref": base_ref}
    run_git(["fetch", "--", remote, branch], cwd=control_dir)
    return {"status": "fetched", "remote": remote, "branch": branch, "ref": base_ref}


def set_branch_role(control_dir: Path, branch: str, role: str) -> None:
    run_git(["config", "--local", "--replace-all", f"branch.{branch}.cubrid-role", role], cwd=control_dir)


def set_branch_pr_url(control_dir: Path, branch: str, pr_url: str) -> None:
    run_git(["config", "--local", "--replace-all", f"branch.{branch}.pr-url", pr_url], cwd=control_dir)


def add_worktree(control_dir: Path, target_path: Path, branch: str, base_ref: str) -> None:
    run_git(
        ["worktree", "add", "-b", branch, os.fspath(target_path), base_ref],
        cwd=control_dir,
    )
    set_branch_role(control_dir, branch, "implement")


def _is_strictly_inside(path: Path, root: Path) -> bool:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    return resolved_path != resolved_root and resolved_root in resolved_path.parents


def _has_symlink_component(root: Path, rel: PurePosixPath) -> bool:
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if os.path.islink(cursor):
            return True
    return False


def _make_writable_tree(root: Path) -> None:
    if not os.path.lexists(root) or os.path.islink(root):
        return
    root_stat = os.lstat(root)
    if stat.S_ISDIR(root_stat.st_mode):
        os.chmod(root, stat.S_IMODE(root_stat.st_mode) | stat.S_IRWXU)
    elif stat.S_ISREG(root_stat.st_mode):
        os.chmod(root, stat.S_IMODE(root_stat.st_mode) | stat.S_IRUSR | stat.S_IWUSR)
        return
    else:
        return

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(dirpath)
        for dirname in dirnames:
            entry = base / dirname
            st = os.lstat(entry)
            if not stat.S_ISLNK(st.st_mode):
                os.chmod(entry, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
        for filename in filenames:
            entry = base / filename
            st = os.lstat(entry)
            if stat.S_ISREG(st.st_mode):
                os.chmod(entry, stat.S_IMODE(st.st_mode) | stat.S_IRUSR | stat.S_IWUSR)


def _rmtree_writable(root: Path) -> None:
    _make_writable_tree(root)
    shutil.rmtree(root)


def _empty_directory(directory: Path) -> None:
    if not os.path.lexists(directory) or os.path.islink(directory):
        return
    _make_writable_tree(directory)
    for child in list(directory.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def git_status_porcelain(repo_dir: Path) -> list[str]:
    result = run_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=repo_dir)
    return [line for line in result.stdout.splitlines() if line]


def _normalize_submodule_relpath(raw: str, repo_dir: Path) -> str | None:
    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        return None
    rel = PurePosixPath(cleaned)
    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..", ".git") for part in rel.parts):
        return None
    if _has_symlink_component(repo_dir, rel):
        return None
    candidate = repo_dir.joinpath(*rel.parts)
    if os.path.lexists(candidate) and not _is_strictly_inside(candidate, repo_dir):
        return None
    return rel.as_posix()


def _discover_direct_submodule_paths(repo_dir: Path) -> list[str]:
    paths: set[str] = set()
    gitmodules = repo_dir / ".gitmodules"
    if gitmodules.is_file() and not gitmodules.is_symlink():
        cfg = run_git(
            [
                "config",
                "--file",
                os.fspath(gitmodules),
                "--null",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ],
            cwd=repo_dir,
            allowed=(0, 1),
        )
        if cfg.returncode == 0 and cfg.stdout:
            for chunk in cfg.stdout.split("\0"):
                if "\n" not in chunk:
                    continue
                _, _, value = chunk.partition("\n")
                norm = _normalize_submodule_relpath(value, repo_dir)
                if norm:
                    paths.add(norm)

    stage = run_git(["ls-files", "--stage", "-z"], cwd=repo_dir, allowed=(0, 1))
    if stage.returncode == 0 and stage.stdout:
        for entry in stage.stdout.split("\0"):
            if entry.startswith("160000 ") and "\t" in entry:
                _, _, raw_path = entry.partition("\t")
                norm = _normalize_submodule_relpath(raw_path, repo_dir)
                if norm:
                    paths.add(norm)

    return sorted(paths)


def inspect_worktree_submodules(worktree_path: Path) -> dict[str, Any]:
    top_level = _discover_direct_submodule_paths(worktree_path)
    queue: list[tuple[PurePosixPath, Path]] = [
        (PurePosixPath(rel), worktree_path.joinpath(*PurePosixPath(rel).parts)) for rel in top_level
    ]
    visited: set[Path] = set()
    populated: list[str] = []
    dirty: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []

    while queue:
        rel_posix, sub_dir = queue.pop(0)
        if _has_symlink_component(worktree_path, rel_posix):
            continue
        if not sub_dir.is_dir() or os.path.islink(sub_dir):
            continue
        if not _is_strictly_inside(sub_dir, worktree_path):
            continue
        if not os.path.lexists(sub_dir / ".git"):
            continue
        resolved_sub = sub_dir.resolve()
        if resolved_sub in visited:
            continue
        visited.add(resolved_sub)

        rel_str = rel_posix.as_posix()
        populated.append(rel_str)

        try:
            status_lines = git_status_porcelain(sub_dir)
        except CliError as exc:
            broken.append({"path": rel_str, "error": exc.message})
            continue

        if status_lines:
            dirty.append({"path": rel_str, "status": status_lines[:20], "total_changes": len(status_lines)})

        try:
            nested_paths = _discover_direct_submodule_paths(sub_dir)
        except CliError:
            nested_paths = []
        for nested_rel in nested_paths:
            nested_posix = PurePosixPath(nested_rel)
            queue.append((rel_posix / nested_posix, sub_dir.joinpath(*nested_posix.parts)))

    populated.sort()
    dirty.sort(key=lambda item: item["path"])
    broken.sort(key=lambda item: item["path"])
    return {
        "top_level": top_level,
        "populated": populated,
        "dirty": dirty,
        "broken": broken,
    }


def _running_cub_processes(worktree: Path, install: Path) -> list[dict[str, str]]:
    roots = {os.fspath(path) for path in (worktree, install, install.resolve())}
    try:
        state = json.loads((worktree / ".cub-workenv/state.json").read_text())
        if state.get("worktree") == str(worktree) and Path(state["install"]).is_absolute():
            roots.add(state["install"])
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass

    def references_target(value: str) -> bool:
        path = value.removesuffix(" (deleted)")
        return any(path == root or path.startswith(root + "/") for root in roots)

    def link_target(path: Path) -> str:
        try:
            return os.readlink(path)
        except OSError:
            return ""

    def process_fields(path: Path) -> list[str]:
        try:
            return os.fsdecode(path.read_bytes()).split("\0")
        except OSError:
            return []

    matches = []
    for process in PROC_DIR.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            name = (process / "comm").read_text(errors="replace").rstrip("\n")
        except OSError:
            continue
        exe = link_target(process / "exe")
        if not (name.startswith("cub_") or Path(exe).name.startswith("cub_")):
            continue
        paths = [exe, link_target(process / "cwd")]
        for arg in process_fields(process / "cmdline"):
            paths.append(arg)
            if arg.startswith("-") and "=" in arg:
                paths.append(arg.split("=", 1)[1])
        for field in process_fields(process / "environ"):
            key, _, value = field.partition("=")
            if key in ("CUBRID", "CUBRID_DATABASES", "TMPDIR"):
                paths.append(value)
        matched = any(map(references_target, paths))
        if not matched:
            try:
                matched = any(references_target(link_target(fd)) for fd in (process / "fd").iterdir())
            except OSError:
                pass
        if matched:
            matches.append({"pid": process.name, "name": name})
    return matches


def _reset_workenv(worktree: Path, env: Mapping[str, str]) -> str | None:
    command = shutil.which("cub-workenv", path=env.get("PATH", os.defpath))
    if command is None:
        return "cub-workenv is unavailable on PATH. Work environment reset was skipped."
    try:
        result = subprocess.run(
            [command, "reset", "--worktree", os.fspath(worktree), "--apply"],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cub-workenv reset failed for {worktree}: {exc}"
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return f"cub-workenv reset failed for {worktree} (exit {result.returncode}): {detail}"
    return None


def delete_worktree(
    control_dir: Path,
    record: WorktreeRecord,
    *,
    force: bool = False,
    delete_branch: bool = False,
    dry_run: bool = False,
    keep_install: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    resolved_control = control_dir.resolve()
    if record.is_main or record.path == resolved_control:
        raise CliError(
            "PROTECTED_WORKTREE",
            f"Cannot delete protected control/main worktree: {record.path}",
            fix="Choose a linked worktree from `cubrid-worktree list`.",
        )

    try:
        cwd: Path | None = Path.cwd().resolve()
    except OSError:
        cwd = None
    if cwd is not None and (cwd == record.path or record.path in cwd.parents):
        raise CliError(
            "ACTIVE_CWD",
            f"Current working directory ({cwd}) is inside target worktree '{record.path}'.",
            fix="Change directory outside the worktree and re-run.",
        )

    if record.locked:
        reason_part = f" ({record.lock_reason})" if record.lock_reason else ""
        raise CliError(
            "WORKTREE_LOCKED",
            f"Worktree '{record.path}' is locked{reason_part}.",
            fix=f"Unlock it first with: git -C {control_dir} worktree unlock {record.path}",
        )

    if not record.path.exists():
        raise CliError(
            "WORKTREE_MISSING_DIR",
            f"Registered worktree directory is missing on disk: {record.path}",
            fix=f"Prune stale worktree metadata with: git -C {control_dir} worktree prune",
        )

    wt_gitdir = Path(
        run_git(["rev-parse", "--path-format=absolute", "--git-dir"], cwd=record.path).stdout.strip()
    ).resolve()
    common_gitdir = Path(
        run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=record.path).stdout.strip()
    ).resolve()
    if wt_gitdir == common_gitdir or wt_gitdir.parent != (common_gitdir / "worktrees").resolve():
        raise CliError(
            "PROTECTED_WORKTREE",
            f"Refusing to delete non-linked worktree gitdir: {record.path}",
            fix="Only linked worktrees under `<common-git-dir>/worktrees/` can be deleted.",
        )

    active_env = os.environ if env is None else env
    home = Path(active_env.get("HOME") or Path.home()).resolve()
    install = home / ".cub" / "install" / record.name
    warnings = []
    install_status = "kept" if keep_install else "absent"
    if not keep_install and os.path.lexists(install):
        if _has_symlink_component(home, PurePosixPath(install.relative_to(home))) or not install.is_dir():
            warnings.append(f"Skipped install cleanup for {install}. Expected a directory without symlinks in its path.")
            install_status = "skipped"
        else:
            install_status = "would_remove" if dry_run else "remove"

    if not force:
        try:
            processes = _running_cub_processes(record.path, install)
        except OSError as exc:
            raise CliError(
                "PROCESS_CHECK_FAILED", f"Cannot inspect running CUBRID processes: {exc}",
                fix="Make /proc available or use --force after checking for running CUBRID processes.",
            ) from exc
        if processes:
            names = ", ".join(f"{p['name']} (PID {p['pid']})" for p in processes)
            raise CliError(
                "ACTIVE_CUBRID_PROCESSES", f"CUBRID processes reference {record.path} or its install: {names}",
                fix="Stop the listed processes or pass --force to override the check.",
                details={"processes": processes},
            )

    wt_modules_dir = wt_gitdir / "modules"
    modules_gitdir_present = wt_modules_dir.exists()

    sub_info = inspect_worktree_submodules(record.path)
    super_status = git_status_porcelain(record.path)

    if not force:
        if sub_info["broken"]:
            broken_names = ", ".join(item["path"] for item in sub_info["broken"])
            raise CliError(
                "SUBMODULE_STATUS_FAILED",
                f"Cannot inspect submodule state in '{record.path}': {broken_names}",
                fix="Repair the submodule checkout or re-run with --force.",
                details={"broken_submodules": sub_info["broken"]},
            )
        if sub_info["dirty"]:
            dirty_names = ", ".join(item["path"] for item in sub_info["dirty"])
            raise CliError(
                "DIRTY_SUBMODULE",
                f"Refusing to delete worktree '{record.path}': dirty submodule(s): {dirty_names}.",
                fix="Commit, stash, or clean the listed submodule(s), or re-run with --force.",
                details={
                    "dirty_submodules": sub_info["dirty"],
                    "worktree_status": super_status[:20],
                },
            )
        if super_status:
            raise CliError(
                "DIRTY_WORKTREE",
                f"Refusing to delete dirty worktree '{record.path}' ({len(super_status)} uncommitted/untracked change(s)).",
                fix="Commit, stash, or remove local changes, or re-run with --force.",
                details={"status": super_status[:20], "total_changes": len(super_status)},
            )

    if dry_run:
        if delete_branch and record.branch:
            branch_preview: dict[str, Any] = {
                "requested": True,
                "deleted": False,
                "branch": record.branch,
                "status": "dry_run",
            }
        elif delete_branch:
            branch_preview = {
                "requested": True,
                "deleted": False,
                "branch": None,
                "status": "skipped",
                "reason": "detached HEAD",
            }
        else:
            branch_preview = {
                "requested": False,
                "deleted": False,
                "branch": record.branch,
                "status": "skipped",
                "reason": "not requested",
            }
        return {
            "action": "delete",
            "deleted": False,
            "dry_run": True,
            "name": record.name,
            "path": os.fspath(record.path),
            "branch": record.branch,
            "role": record.role,
            "ticket": record.ticket,
            "pr_url": record.pr_url,
            "force": force,
            "installation": {"path": str(install), "status": install_status},
            "warnings": warnings,
            "submodules": {
                "discovered": sub_info["top_level"],
                "populated": sub_info["populated"],
                "deinitialized": False,
                "cleaned_paths": [],
                "modules_gitdir_present": modules_gitdir_present,
                "modules_gitdir_removed": False,
            },
            "branch_deletion": branch_preview,
        }

    deinitialized = False
    cleaned_paths: list[str] = []
    modules_gitdir_removed = False

    if sub_info["top_level"] or sub_info["populated"] or modules_gitdir_present:
        gitmodules = record.path / ".gitmodules"
        if gitmodules.is_file() and not gitmodules.is_symlink():
            deinit_cmd = ["submodule", "deinit", "--force", "--all"] if force else ["submodule", "deinit", "--all"]
            run_git(deinit_cmd, cwd=record.path)
            deinitialized = True

        for rel in sub_info["top_level"]:
            rel_posix = PurePosixPath(rel)
            sub_dir = record.path.joinpath(*rel_posix.parts)
            if (
                not _has_symlink_component(record.path, rel_posix)
                and sub_dir.is_dir()
                and not os.path.islink(sub_dir)
                and _is_strictly_inside(sub_dir, record.path)
                and os.path.lexists(sub_dir / ".git")
            ):
                _empty_directory(sub_dir)
                cleaned_paths.append(rel)

        if wt_modules_dir.exists():
            _rmtree_writable(wt_modules_dir)
            modules_gitdir_removed = True

        if not force:
            post_status = git_status_porcelain(record.path)
            if post_status:
                raise CliError(
                    "DIRTY_WORKTREE_AFTER_SUBMODULE_CLEANUP",
                    f"Worktree '{record.path}' became non-clean after submodule cleanup ({len(post_status)} entry/entries).",
                    fix="Inspect `git status` in the worktree or re-run with --force.",
                    details={"status": post_status[:20]},
                )

    remove_cmd = ["worktree", "remove"]
    if force:
        remove_cmd.append("--force")
    remove_cmd.append(os.fspath(record.path))
    warning = _reset_workenv(record.path, active_env)
    if warning:
        warnings.append(warning)
    run_git(remove_cmd, cwd=control_dir)
    if install_status == "remove":
        try:
            _rmtree_writable(install)
            install_status = "removed"
        except OSError as exc:
            install_status = "failed"
            warnings.append(f"Could not remove install {install}: {exc}")

    if not delete_branch:
        branch_deletion: dict[str, Any] = {
            "requested": False,
            "deleted": False,
            "branch": record.branch,
            "status": "skipped",
            "reason": "not requested",
        }
    elif not record.branch:
        branch_deletion = {
            "requested": True,
            "deleted": False,
            "branch": None,
            "status": "skipped",
            "reason": "detached HEAD",
        }
    else:
        flag = "-D" if force else "-d"
        branch_result = run_git(["branch", flag, record.branch], cwd=control_dir, allowed=(0, 1))
        if branch_result.returncode != 0:
            detail = (branch_result.stderr or branch_result.stdout).strip()
            raise CliError(
                "BRANCH_DELETE_FAILED",
                f"Worktree '{record.path}' was deleted, but deleting local branch '{record.branch}' failed: {detail}",
                fix=f"Force-delete the branch if intended: git -C {control_dir} branch -D {record.branch}",
                details={
                    "worktree_deleted": True,
                    "path": os.fspath(record.path),
                    "branch": record.branch,
                    "warnings": warnings,
                },
            )
        branch_deletion = {
            "requested": True,
            "deleted": True,
            "branch": record.branch,
            "status": "deleted",
            "force": force,
        }

    return {
        "action": "delete",
        "deleted": True,
        "dry_run": False,
        "name": record.name,
        "path": os.fspath(record.path),
        "branch": record.branch,
        "role": record.role,
        "ticket": record.ticket,
        "pr_url": record.pr_url,
        "force": force,
        "installation": {"path": str(install), "status": install_status},
        "warnings": warnings,
        "submodules": {
            "discovered": sub_info["top_level"],
            "populated": sub_info["populated"],
            "deinitialized": deinitialized,
            "cleaned_paths": cleaned_paths,
            "modules_gitdir_present": modules_gitdir_present,
            "modules_gitdir_removed": modules_gitdir_removed,
        },
        "branch_deletion": branch_deletion,
    }

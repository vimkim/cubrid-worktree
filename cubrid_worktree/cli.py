"""CLI entrypoint, command handlers, and output formatting for cubrid-worktree."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, resolve_config
from .errors import CliError, SelectionCancelled
from .git_ops import (
    WorktreeRecord,
    add_worktree,
    delete_worktree,
    ensure_control_worktree,
    exact_conflict_error,
    fetch_base_if_remote,
    find_ticket_siblings,
    list_worktrees,
    local_branch_exists,
    validate_branch_name,
)
from .herdr import open_in_herdr
from .naming import extract_single_ticket, normalize_ticket, parse_target
from .prepare import is_worktree_prepared, run_prepare
from .review import (
    existing_candidate_numbers,
    load_review_candidates,
    review_all,
    review_pr,
    select_candidate_interactive,
)

PR_NUM_RE = re.compile(r"PR-([1-9][0-9]*)$", re.IGNORECASE)
LEGACY_NOTICKET_RE = re.compile(r"review-noticket-pr-([1-9][0-9]*)$", re.IGNORECASE)


def _emit_json(payload: dict[str, Any], *, ok: bool = True, stream: Any = None) -> None:
    target = sys.stdout if stream is None else stream
    envelope = {"ok": True, "data": payload} if ok else {"ok": False, "error": payload}
    print(json.dumps(envelope, ensure_ascii=False), file=target)


def _note_siblings(siblings: list[dict[str, str]], *, quiet: bool) -> None:
    if quiet or not siblings:
        return
    print("Existing worktrees/directories for this ticket:", file=sys.stderr)
    for item in siblings:
        branch_part = f" [{item['branch']}]" if item.get("branch") else ""
        role_part = f" ({item['role']})" if item.get("role") else ""
        print(f"  - [{item['source']}] {item['path']}{branch_part}{role_part}", file=sys.stderr)


def _resolve_existing_for_reuse(
    worktrees: list[WorktreeRecord],
    target_path: Path,
    branch: str,
) -> WorktreeRecord:
    resolved = target_path.resolve()
    by_path = next((wt for wt in worktrees if wt.path == resolved), None)
    if by_path is None:
        raise CliError(
            "REUSE_MISMATCH",
            f"Cannot reuse '{target_path}': branch '{branch}' exists or path is not a registered worktree.",
            fix="Inspect `cubrid-worktree list` and `git status`, or choose another summary.",
        )
    if by_path.branch and by_path.branch != branch:
        raise CliError(
            "REUSE_MISMATCH",
            f"Worktree at '{target_path}' is on branch '{by_path.branch}', expected '{branch}'.",
            fix="Pass --branch matching that worktree or run `cubrid-worktree open` directly.",
        )
    return by_path


def handle_create(args: argparse.Namespace, config: Config, env: Mapping[str, str]) -> tuple[dict[str, Any], int]:
    control_dir = ensure_control_worktree(config.control_dir, config.parent_dir)
    target = parse_target(args.target, summary=args.summary)
    branch = args.branch or target.name
    validate_branch_name(control_dir, branch)

    target_path = config.parent_dir / target.name
    worktrees = list_worktrees(control_dir)
    siblings = find_ticket_siblings(worktrees, config.parent_dir, target.ticket, exclude_path=target_path)

    conflict = exact_conflict_error(control_dir, target_path, branch, worktrees)
    if conflict is not None:
        if args.reuse:
            existing = _resolve_existing_for_reuse(worktrees, target_path, branch)
            _note_siblings(siblings, quiet=args.quiet or args.json)
            prep_result = (
                {"status": "skipped", "reason": "already prepared"}
                if (args.no_prepare or is_worktree_prepared(existing.path))
                else run_prepare(config, existing.path, dry_run=args.dry_run)
            )
            herdr_result = (
                open_in_herdr(
                    existing.path,
                    target.space_label,
                    tab_label=target.tab_label,
                    focus=not args.no_focus,
                    new_tab=False,
                    force=bool(args.herdr),
                    dry_run=args.dry_run,
                    env=env,
                )
                if not args.no_herdr
                else {"status": "skipped", "reason": "disabled"}
            )
            return {
                "action": "create",
                "created": False,
                "reused": True,
                "name": target.name,
                "ticket": target.ticket,
                "summary": target.summary,
                "branch": existing.branch or branch,
                "base": args.base,
                "role": existing.role or "implement",
                "path": os.fspath(existing.path),
                "siblings": siblings,
                "fetch": {"status": "skipped", "reason": "reused"},
                "prepare": prep_result,
                "herdr": herdr_result,
                "dry_run": bool(args.dry_run),
            }, 0
        raise CliError(
            "ALREADY_EXISTS",
            f"Cannot create worktree '{target.name}': {conflict}",
            fix=f"Pass --reuse or open it with: cubrid-worktree open {target.name}",
        )

    _note_siblings(siblings, quiet=args.quiet or args.json)

    if args.dry_run:
        fetch_result = (
            {"status": "skipped", "reason": "disabled"}
            if args.no_fetch
            else {"status": "dry_run", "ref": args.base}
        )
        prep_result = (
            {"status": "skipped", "reason": "disabled"}
            if args.no_prepare
            else run_prepare(config, target_path, dry_run=True)
        )
        herdr_result = (
            {"status": "skipped", "reason": "disabled"}
            if args.no_herdr
            else open_in_herdr(
                target_path,
                target.space_label,
                tab_label=target.tab_label,
                focus=not args.no_focus,
                new_tab=True,
                force=bool(args.herdr),
                dry_run=True,
                env=env,
            )
        )
        return {
            "action": "create",
            "created": False,
            "reused": False,
            "name": target.name,
            "ticket": target.ticket,
            "summary": target.summary,
            "branch": branch,
            "base": args.base,
            "role": "implement",
            "path": os.fspath(target_path),
            "siblings": siblings,
            "fetch": fetch_result,
            "prepare": prep_result,
            "herdr": herdr_result,
            "dry_run": True,
        }, 0

    fetch_result = (
        {"status": "skipped", "reason": "disabled"}
        if args.no_fetch
        else fetch_base_if_remote(control_dir, args.base)
    )
    add_worktree(control_dir, target_path, branch, args.base)
    prep_result = (
        {"status": "skipped", "reason": "disabled"}
        if args.no_prepare
        else run_prepare(config, target_path, dry_run=False)
    )
    herdr_result = (
        {"status": "skipped", "reason": "disabled"}
        if args.no_herdr
        else open_in_herdr(
            target_path,
            target.space_label,
            tab_label=target.tab_label,
            focus=not args.no_focus,
            new_tab=True,
            force=bool(args.herdr),
            dry_run=False,
            env=env,
        )
    )
    return {
        "action": "create",
        "created": True,
        "reused": False,
        "name": target.name,
        "ticket": target.ticket,
        "summary": target.summary,
        "branch": branch,
        "base": args.base,
        "role": "implement",
        "path": os.fspath(target_path),
        "siblings": siblings,
        "fetch": fetch_result,
        "prepare": prep_result,
        "herdr": herdr_result,
        "dry_run": False,
    }, 0


def handle_review(args: argparse.Namespace, config: Config, env: Mapping[str, str]) -> tuple[dict[str, Any], int]:
    control_dir = ensure_control_worktree(config.control_dir, config.parent_dir)

    if args.all:
        herdr_enabled = bool(args.herdr) and not args.no_herdr
        return review_all(
            config,
            control_dir,
            prepare=not args.no_prepare,
            herdr=herdr_enabled,
            force_herdr=bool(args.herdr),
            focus=not args.no_focus,
            dry_run=bool(args.dry_run),
            env=env,
        )

    if args.interactive:
        candidates = load_review_candidates(config, control_dir)
        if not candidates:
            return {
                "action": "review",
                "created": False,
                "reused": False,
                "message": "No pull requests currently request your review.",
            }, 0
        worktrees = list_worktrees(control_dir)
        existing_nums = existing_candidate_numbers(candidates, worktrees)
        selected = select_candidate_interactive(candidates, existing_nums, control_dir)
        if selected is None:
            return {
                "action": "review",
                "created": False,
                "reused": False,
                "message": "No pull requests currently request your review.",
            }, 0
        selector = selected.url
    else:
        selector = args.pr

    herdr_enabled = not args.no_herdr
    result = review_pr(
        selector,
        config,
        control_dir,
        prepare=not args.no_prepare,
        herdr=herdr_enabled,
        force_herdr=bool(args.herdr),
        focus=not args.no_focus,
        reuse=bool(args.reuse),
        dry_run=bool(args.dry_run),
        env=env,
    )
    _note_siblings(result.get("siblings", []), quiet=args.quiet or args.json)
    return result, 0


def _resolve_worktree_record(raw_target: str, config: Config, control_dir: Path) -> WorktreeRecord:
    stripped = raw_target.strip()
    if not stripped:
        raise CliError(
            "INVALID_USAGE",
            "Worktree target must not be empty.",
            fix="Pass a worktree directory name, path, or branch.",
            exit_code=2,
        )

    worktrees = list_worktrees(control_dir)
    resolved_direct = (
        Path(stripped).expanduser().resolve()
        if (os.path.sep in stripped or stripped.startswith(".") or stripped.startswith("~"))
        else None
    )
    resolved_under_parent = (config.parent_dir / stripped).resolve()

    matched: WorktreeRecord | None = None
    if resolved_direct is not None:
        matched = next((record for record in worktrees if record.path == resolved_direct), None)
    if matched is None:
        matched = next((record for record in worktrees if record.path == resolved_under_parent), None)
    if matched is None:
        matched = next(
            (record for record in worktrees if record.path.name.casefold() == stripped.casefold()),
            None,
        )
    if matched is None:
        matched = next(
            (record for record in worktrees if record.branch and record.branch.casefold() == stripped.casefold()),
            None,
        )

    if matched is None:
        raise CliError(
            "WORKTREE_NOT_FOUND",
            f"No registered CUBRID worktree matches '{raw_target}'.",
            fix="List available worktrees with `cubrid-worktree list` or create one with `cubrid-worktree create`.",
        )
    return matched


def _resolve_open_target(raw_target: str, config: Config, control_dir: Path) -> tuple[WorktreeRecord, str, str | None]:
    matched = _resolve_worktree_record(raw_target, config, control_dir)

    ticket = matched.ticket or extract_single_ticket(matched.path.name) or (
        extract_single_ticket(matched.branch) if matched.branch else None
    )
    if ticket:
        space_label = ticket
        if matched.path.name.casefold() == ticket.casefold():
            default_tab_label = None
        elif matched.path.name.casefold().startswith(f"{ticket.casefold()}-"):
            default_tab_label = matched.path.name[len(ticket) + 1 :]
        else:
            default_tab_label = matched.path.name
    else:
        pr_match = PR_NUM_RE.match(matched.path.name) or LEGACY_NOTICKET_RE.match(matched.path.name)
        if pr_match:
            space_label = f"PR-{int(pr_match.group(1))}"
            default_tab_label = None if matched.path.name == space_label else matched.path.name
        else:
            space_label = matched.path.name
            default_tab_label = None

    return matched, space_label, default_tab_label


def handle_open(args: argparse.Namespace, config: Config, env: Mapping[str, str]) -> tuple[dict[str, Any], int]:
    control_dir = ensure_control_worktree(config.control_dir, config.parent_dir)
    record, space_label, default_tab_label = _resolve_open_target(args.target, config, control_dir)
    tab_label = args.tab_label if args.tab_label is not None else default_tab_label
    herdr_result = open_in_herdr(
        record.path,
        space_label,
        tab_label=tab_label,
        focus=not args.no_focus,
        new_tab=bool(args.new_tab),
        force=not args.dry_run,
        dry_run=bool(args.dry_run),
        env=env,
    )
    return {
        "action": "open",
        "name": record.path.name,
        "path": os.fspath(record.path),
        "branch": record.branch,
        "ticket": record.ticket,
        "role": record.role,
        "herdr": herdr_result,
        "dry_run": bool(args.dry_run),
    }, 0


def handle_list(args: argparse.Namespace, config: Config) -> tuple[dict[str, Any], int]:
    control_dir = ensure_control_worktree(config.control_dir, config.parent_dir)
    worktrees = list_worktrees(control_dir)
    ticket_filter = normalize_ticket(args.ticket) if args.ticket else None

    if ticket_filter:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(ticket_filter)}(?![A-Za-z0-9])", re.IGNORECASE)
        filtered = [
            record
            for record in worktrees
            if record.ticket == ticket_filter
            or pattern.search(record.path.name)
            or (record.branch and pattern.search(record.branch))
        ]
    else:
        filtered = worktrees

    return {
        "action": "list",
        "control_dir": os.fspath(control_dir),
        "parent_dir": os.fspath(config.parent_dir),
        "ticket_filter": ticket_filter,
        "worktrees": [record.to_dict() for record in filtered],
    }, 0


def handle_delete(args: argparse.Namespace, config: Config, env: Mapping[str, str]) -> tuple[dict[str, Any], int]:
    control_dir = ensure_control_worktree(config.control_dir, config.parent_dir)
    record = _resolve_worktree_record(args.target, config, control_dir)
    result = delete_worktree(
        control_dir,
        record,
        force=bool(args.force),
        delete_branch=bool(args.delete_branch),
        dry_run=bool(args.dry_run),
        keep_install=bool(args.keep_install),
        env=env,
    )
    return result, 0


def _format_human(data: dict[str, Any]) -> str:
    action = data.get("action")
    if action == "list":
        rows = data.get("worktrees", [])
        if not rows:
            suffix = f" for {data['ticket_filter']}" if data.get("ticket_filter") else ""
            return f"No worktrees found{suffix}."
        lines = []
        for row in rows:
            branch = row["branch"] or "(detached)"
            extras = [x for x in (row.get("role"), row.get("pr_url")) if x]
            suffix = f" ({', '.join(extras)})" if extras else ""
            lines.append(f"{row['name']}\t{branch}\t{row['path']}{suffix}")
        return "\n".join(lines)

    if action == "review-all":
        lines = []
        skipped = data.get("skipped", [])
        created = data.get("created", [])
        failed = data.get("failed", [])
        if created:
            lines.append("Review worktrees created:")
            for item in created:
                lines.append(f"  #{item['pr_number']} -> {item['path']} [{item['branch']}]")
        if skipped:
            lines.append("Review worktrees skipped [existing]:")
            for item in skipped:
                lines.append(f"  #{item['pr_number']} {item['title']}")
        if failed:
            lines.append("Review worktrees failed:")
            for item in failed:
                lines.append(f"  #{item['pr_number']}: {item['error']['message']}")
        if not lines:
            return "No pull requests currently request your review."
        return "\n".join(lines)

    if action == "delete":
        prefix = "[dry-run] " if data.get("dry_run") else ""
        lines = [f"{prefix}Deleted worktree: {data['path']}"]
        if data.get("branch"):
            branch_del = data.get("branch_deletion") or {}
            if branch_del.get("deleted"):
                branch_state = "deleted"
            elif branch_del.get("status") == "dry_run":
                branch_state = "would delete"
            else:
                branch_state = "kept"
            lines.append(f"Branch: {data['branch']} ({branch_state})")
        if data.get("role"):
            lines.append(f"Role: {data['role']}")
        sub = data.get("submodules") or {}
        populated = sub.get("populated") or []
        cleaned = sub.get("cleaned_paths") or []
        if populated or cleaned or sub.get("modules_gitdir_present"):
            names = ", ".join(populated or cleaned or sub.get("discovered") or [])
            if data.get("dry_run"):
                mod_note = "; modules gitdir present" if sub.get("modules_gitdir_present") else ""
                lines.append(f"Submodules: {names or 'present'}{mod_note}")
            else:
                mod_note = "; modules gitdir removed" if sub.get("modules_gitdir_removed") else ""
                lines.append(f"Submodules: cleaned ({names or 'none'}){mod_note}")
        installation = data["installation"]
        if installation["status"] == "would_remove":
            lines.append(f"Would remove install: {installation['path']}")
        elif installation["status"] == "removed":
            lines.append(f"Removed install: {installation['path']}")
        return "\n".join(lines)

    if data.get("message") and not data.get("path"):
        return str(data["message"])

    lines = []
    prefix = "[dry-run] " if data.get("dry_run") else ""
    if action == "open":
        lines.append(f"{prefix}Opened worktree: {data['path']}")
    elif data.get("reused"):
        lines.append(f"{prefix}Reused worktree: {data['path']}")
    else:
        lines.append(f"{prefix}Created worktree: {data['path']}")
    if data.get("branch"):
        lines.append(f"Branch: {data['branch']}")
    if data.get("role"):
        lines.append(f"Role: {data['role']}")
    if data.get("pr_url"):
        lines.append(f"PR: {data['pr_url']}")
    prep = data.get("prepare") or {}
    if prep.get("status"):
        lines.append(f"Prepare: {prep['status']}")
    herdr = data.get("herdr") or {}
    if herdr.get("status") and herdr["status"] != "skipped":
        details = [herdr["status"]]
        if herdr.get("space_label"):
            details.append(f"space={herdr['space_label']}")
        if herdr.get("tab_label"):
            details.append(f"tab={herdr['tab_label']}")
        if herdr.get("tab_id"):
            details.append(f"tab_id={herdr['tab_id']}")
        if herdr.get("pane_id"):
            details.append(f"pane_id={herdr['pane_id']}")
        lines.append(f"Herdr: {' '.join(details)}")
    elif herdr.get("reason"):
        lines.append(f"Herdr: skipped ({herdr['reason']})")
    return "\n".join(lines)


def _format_plain(data: dict[str, Any]) -> str:
    action = data.get("action")
    if action == "list":
        return "\n".join(
            "\t".join(
                [
                    row["name"],
                    row.get("ticket") or "-",
                    row.get("role") or "-",
                    row.get("branch") or "detached",
                    row["path"],
                    row.get("pr_url") or "-",
                ]
            )
            for row in data.get("worktrees", [])
        )
    if action == "review-all":
        return "\n".join(
            f"PR-{item['pr_number']}\t{item['branch']}\t{item['path']}"
            for item in data.get("created", [])
        )
    if data.get("path"):
        return "\t".join(
            [
                str(data.get("name", "")),
                str(data.get("branch") or "-"),
                str(data.get("role") or "-"),
                str(data["path"]),
            ]
        )
    return ""


class _UsageArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError("INVALID_USAGE", message, fix="Run `cubrid-worktree --help` for usage.", exit_code=2)


def _add_common_path_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--control-dir", help="Path to control Git checkout (default: ~/gh/cb/develop)")
    parser.add_argument("--parent-dir", help="Parent directory for sibling worktrees (default: <control-dir>/..)")


def _add_output_flags(parser: argparse.ArgumentParser) -> None:
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Emit structured JSON envelope on stdout")
    fmt.add_argument("--plain", action="store_true", help="Emit stable tab-separated lines on stdout")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress non-essential diagnostics")


def _add_herdr_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--herdr", action="store_true", help="Require Herdr placement (fail if HERDR_ENV!=1)")
    group.add_argument("--no-herdr", action="store_true", help="Do not create/open Herdr Spaces or tabs")
    parser.add_argument("--no-focus", action="store_true", help="Do not switch Herdr UI focus")


def build_parser() -> argparse.ArgumentParser:
    root = _UsageArgumentParser(
        prog="cubrid-worktree",
        description="Manage ticket-first CUBRID Git worktrees and Herdr workspaces from any directory.",
        epilog=(
            "Examples:\n"
            "  cubrid-worktree create CBRD-28099\n"
            "  cubrid-worktree create CBRD-28099-bench --base origin/develop\n"
            "  cubrid-worktree review 7887\n"
            "  cubrid-worktree open CBRD-28099-bench\n"
            "  cubrid-worktree list CBRD-28099 --json\n"
            "  cubrid-worktree delete CBRD-28099-bench"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = root.add_subparsers(dest="subcommand", required=True)

    create_p = subparsers.add_parser("create", help="Create a primary or summary-scoped CUBRID worktree")
    create_p.add_argument("target", help="Ticket (CBRD-28099) or summary name (CBRD-28099-bench)")
    create_p.add_argument("--summary", help="Optional summary suffix when target is a bare ticket")
    create_p.add_argument("--base", default="origin/develop", help="Base ref for the new branch (default: origin/develop)")
    create_p.add_argument("--branch", help="Explicit branch name (default: worktree name)")
    create_p.add_argument("--no-fetch", action="store_true", help="Skip fetching remote base ref before branch creation")
    create_p.add_argument("--no-prepare", action="store_true", help="Skip running prepare-build")
    create_p.add_argument("--prepare-justfile", help="Path to justfile containing prepare-build")
    create_p.add_argument("--reuse", action="store_true", help="Reuse an existing matching worktree instead of failing")
    create_p.add_argument("-n", "--dry-run", action="store_true", help="Preview actions without creating worktrees or tabs")
    _add_herdr_flags(create_p)
    _add_common_path_flags(create_p)
    _add_output_flags(create_p)

    review_p = subparsers.add_parser("review", help="Check out a CUBRID PR into a ticket-first review worktree")
    review_p.add_argument("pr", nargs="?", help="PR number or GitHub URL")
    review_p.add_argument("--interactive", action="store_true", help="Interactively choose a requested PR with fzf")
    review_p.add_argument("--all", action="store_true", help="Check out every missing requested review PR")
    review_p.add_argument("--repo", help="GitHub repository OWNER/REPO (default: CUBRID/cubrid)")
    review_p.add_argument("--no-prepare", action="store_true", help="Skip running prepare-build")
    review_p.add_argument("--prepare-justfile", help="Path to justfile containing prepare-build")
    review_p.add_argument("--reuse", action="store_true", help="Reuse an existing review worktree for the PR")
    review_p.add_argument("-n", "--dry-run", action="store_true", help="Preview actions without modifying Git or Herdr")
    _add_herdr_flags(review_p)
    _add_common_path_flags(review_p)
    _add_output_flags(review_p)

    open_p = subparsers.add_parser("open", help="Open an existing worktree in its ticket Herdr Space/Tab")
    open_p.add_argument("target", help="Worktree directory name, path, or branch")
    open_p.add_argument("--new-tab", action="store_true", help="Always create a new tab instead of focusing a matching cwd tab")
    open_p.add_argument("--tab-label", help="Override Herdr tab label")
    open_p.add_argument("--no-focus", action="store_true", help="Do not switch Herdr UI focus")
    open_p.add_argument("-n", "--dry-run", action="store_true", help="Preview Herdr placement without modifying Herdr")
    _add_common_path_flags(open_p)
    _add_output_flags(open_p)

    list_p = subparsers.add_parser("list", help="List registered CUBRID worktrees")
    list_p.add_argument("ticket", nargs="?", help="Optional JIRA ticket key filter (e.g. CBRD-28099)")
    _add_common_path_flags(list_p)
    _add_output_flags(list_p)

    delete_p = subparsers.add_parser(
        "delete",
        help="Safely delete a linked CUBRID worktree (including submodules)",
    )
    delete_p.add_argument("target", help="Worktree directory name, path, or branch")
    delete_p.add_argument(
        "--delete-branch",
        action="store_true",
        help="Also delete the worktree's local branch (git branch -d, or -D with --force)",
    )
    delete_p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Remove even if the worktree is dirty or referenced by running CUBRID processes",
    )
    delete_p.add_argument(
        "--keep-install",
        action="store_true",
        help="Keep the installation under ~/.cub/install/<worktree-name>",
    )
    delete_p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Preview checks and actions without modifying Git or the filesystem",
    )
    _add_common_path_flags(delete_p)
    _add_output_flags(delete_p)

    return root


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    active_argv = sys.argv[1:] if argv is None else argv
    active_env = os.environ if env is None else env
    json_mode = "--json" in active_argv

    try:
        parser = build_parser()
        args = parser.parse_args(active_argv)

        if args.subcommand == "review":
            mode_count = sum((args.pr is not None, bool(args.interactive), bool(args.all)))
            if mode_count != 1:
                raise CliError(
                    "INVALID_USAGE",
                    "Provide exactly one of PR, --interactive, or --all.",
                    fix="Run `cubrid-worktree review --help` for examples.",
                    exit_code=2,
                )
            if args.all and args.reuse:
                raise CliError(
                    "INVALID_USAGE",
                    "--reuse cannot be combined with --all.",
                    fix="Use --reuse with a single PR or --interactive.",
                    exit_code=2,
                )

        config = resolve_config(
            control_dir=getattr(args, "control_dir", None),
            parent_dir=getattr(args, "parent_dir", None),
            repository=getattr(args, "repo", None),
            prepare_justfile=getattr(args, "prepare_justfile", None),
            env=active_env,
        )

        if args.subcommand == "create":
            data, code = handle_create(args, config, active_env)
        elif args.subcommand == "review":
            data, code = handle_review(args, config, active_env)
        elif args.subcommand == "open":
            data, code = handle_open(args, config, active_env)
        elif args.subcommand == "list":
            data, code = handle_list(args, config)
        elif args.subcommand == "delete":
            data, code = handle_delete(args, config, active_env)
            for warning in data["warnings"]:
                print(f"warning: {warning}", file=sys.stderr)
        else:
            raise CliError("INVALID_USAGE", f"Unknown subcommand: {args.subcommand}", exit_code=2)

        if args.json:
            if code != 0:
                failed = data.get("failed", [])
                _emit_json(
                    {
                        "code": "PARTIAL_FAILURE",
                        "message": f"Operation completed with {len(failed)} failure(s).",
                        "fix": "Inspect error.details.failed and retry the failed items.",
                        "details": data,
                    },
                    ok=False,
                    stream=sys.stderr,
                )
            else:
                _emit_json(data, ok=True)
        elif args.plain:
            text = _format_plain(data)
            if text:
                print(text)
            if code != 0 and data.get("failed"):
                print(
                    f"error: PARTIAL_FAILURE — {len(data['failed'])} item(s) failed.",
                    file=sys.stderr,
                )
        elif not args.quiet:
            print(_format_human(data))
            if code != 0 and data.get("failed"):
                print(
                    f"error: PARTIAL_FAILURE — {len(data['failed'])} item(s) failed.",
                    file=sys.stderr,
                )
        return code

    except SelectionCancelled as exc:
        if json_mode:
            _emit_json(
                {"code": "CANCELLED", "message": str(exc), "fix": "Re-run to select a PR."},
                ok=False,
                stream=sys.stderr,
            )
        else:
            print(str(exc), file=sys.stderr)
        return SelectionCancelled.exit_code
    except CliError as exc:
        if json_mode:
            err_payload: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.fix:
                err_payload["fix"] = exc.fix
            if exc.details is not None:
                err_payload["details"] = exc.details
            _emit_json(err_payload, ok=False, stream=sys.stderr)
        else:
            for warning in (exc.details or {}).get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
            print(f"error: {exc.code} — {exc.message}", file=sys.stderr)
            if exc.fix:
                print(f"fix: {exc.fix}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        if json_mode:
            _emit_json(
                {"code": "INTERRUPTED", "message": "Interrupted by user."},
                ok=False,
                stream=sys.stderr,
            )
        else:
            print("Interrupted.", file=sys.stderr)
        return 130

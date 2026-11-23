"""Pull-request discovery, interactive selection, and review worktree creation."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import CliError, SelectionCancelled
from .git_ops import (
    WorktreeRecord,
    exact_conflict_error,
    find_existing_pr_worktree,
    find_ticket_siblings,
    is_target_occupied,
    list_worktrees,
    set_branch_pr_url,
    set_branch_role,
)
from .herdr import open_in_herdr
from .naming import choose_review_target, extract_ticket_from_title, parse_pr_selector
from .prepare import is_worktree_prepared, run_prepare


@dataclass(frozen=True)
class ReviewCandidate:
    number: int
    title: str
    url: str
    author_login: str
    created_at: str
    approved_count: int
    reviewer_pool_count: int
    review_state: str


def _run_capture(command: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CliError(
            "MISSING_COMMAND",
            f"Required command is not installed: {command[0]}",
            fix=f"Install {command[0]} and ensure it is on PATH.",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise CliError(
            "EXTERNAL_COMMAND_FAILED",
            f"Command failed ({shlex.join(command)}){suffix}",
        )
    return result.stdout.strip()


def load_pr_details(pr_selector: str, config: Config, control_dir: Path) -> tuple[int, str, str]:
    requested_number, canonical_url = parse_pr_selector(pr_selector, config.repository)
    output = _run_capture(
        [
            "gh",
            "pr",
            "view",
            canonical_url,
            "--repo",
            config.repository,
            "--json",
            "number,title,url",
        ],
        cwd=control_dir,
    )
    try:
        details = json.loads(output)
        number = details["number"]
        title = details["title"]
        resolved_url = details["url"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CliError("GH_METADATA_ERROR", "Could not read PR number, title, and URL from gh output.") from exc

    if not isinstance(number, int) or number <= 0 or not isinstance(title, str) or not title.strip() or not isinstance(resolved_url, str):
        raise CliError("GH_METADATA_ERROR", "gh returned incomplete PR metadata.")
    resolved_number, normalized_url = parse_pr_selector(resolved_url, config.repository)
    if resolved_number != requested_number or resolved_number != number:
        raise CliError(
            "GH_METADATA_ERROR",
            f"Requested PR #{requested_number}, but gh resolved PR #{number} ({resolved_url}).",
        )
    return number, title.strip(), normalized_url


def _required_string(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CliError("CANDIDATE_PARSE_ERROR", f"Candidate row has an invalid {field!r}.")
    return value


def _required_count(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CliError("CANDIDATE_PARSE_ERROR", f"Candidate row has an invalid {field!r}.")
    return value


def parse_review_candidates(output: str, repository: str) -> list[ReviewCandidate]:
    try:
        rows = json.loads(output)
    except json.JSONDecodeError as exc:
        raise CliError("CANDIDATE_PARSE_ERROR", "Could not parse review candidates JSON output.") from exc
    if not isinstance(rows, list):
        raise CliError("CANDIDATE_PARSE_ERROR", "Review candidates JSON output must be an array.")

    candidates: list[ReviewCandidate] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise CliError("CANDIDATE_PARSE_ERROR", "Review candidates array contains a non-object row.")
        number = _required_count(raw_row, "number")
        if number == 0:
            raise CliError("CANDIDATE_PARSE_ERROR", "Candidate row has an invalid 'number'.")
        url = _required_string(raw_row, "url")
        resolved_number, canonical_url = parse_pr_selector(url, repository)
        if resolved_number != number:
            raise CliError(
                "CANDIDATE_PARSE_ERROR",
                f"Candidate metadata mismatch: number {number}, URL {url}",
            )
        if "requested" in raw_row and raw_row.get("requested") is not True:
            raise CliError("CANDIDATE_PARSE_ERROR", f"Candidate PR #{number} is not requested.")

        if isinstance(raw_row.get("author"), dict):
            author_login = str(raw_row["author"].get("login") or "unknown")
        else:
            author_login = _required_string(raw_row, "author_login")

        created_at = str(raw_row.get("created_at") or raw_row.get("createdAt") or "")
        if not created_at:
            raise CliError("CANDIDATE_PARSE_ERROR", "Candidate row is missing creation timestamp.")

        approved_count = (
            _required_count(raw_row, "approved_count") if "approved_count" in raw_row else 0
        )
        reviewer_pool_count = (
            _required_count(raw_row, "reviewer_pool_count") if "reviewer_pool_count" in raw_row else 0
        )
        review_state = str(raw_row.get("review_state") or ("draft" if raw_row.get("isDraft") else "requested"))

        candidates.append(
            ReviewCandidate(
                number=number,
                title=_required_string(raw_row, "title"),
                url=canonical_url,
                author_login=author_login,
                created_at=created_at,
                approved_count=approved_count,
                reviewer_pool_count=reviewer_pool_count,
                review_state=review_state,
            )
        )
    return candidates


def load_review_candidates(config: Config, control_dir: Path) -> list[ReviewCandidate]:
    if config.review_candidates_cmd:
        cmd = shlex.split(config.review_candidates_cmd)
    elif config.repository.casefold() == "cubrid/cubrid" and shutil.which("cubrid-dev2-pr"):
        cmd = ["cubrid-dev2-pr", "--json", "--requested-only"]
    else:
        cmd = [
            "gh",
            "pr",
            "list",
            "--repo",
            config.repository,
            "--state",
            "open",
            "--search",
            "review-requested:@me",
            "--json",
            "number,title,url,author,createdAt,isDraft",
        ]
    output = _run_capture(cmd, cwd=control_dir)
    return parse_review_candidates(output, config.repository)


def _single_line(value: str) -> str:
    return " ".join(value.split())


def candidate_display(candidate: ReviewCandidate, existing: bool = False) -> str:
    marker = "[existing] " if existing else ""
    counts = (
        f"  {candidate.approved_count}/{candidate.reviewer_pool_count}"
        if candidate.reviewer_pool_count > 0
        else ""
    )
    return (
        f"{marker}#{candidate.number}  {candidate.author_login}  "
        f"{candidate.created_at[:10]}{counts}  "
        f"{_single_line(candidate.review_state)}  {_single_line(candidate.title)}"
    )


def select_candidate_interactive(
    candidates: list[ReviewCandidate],
    existing_numbers: set[int],
    control_dir: Path,
) -> ReviewCandidate | None:
    if not candidates:
        return None
    by_url = {candidate.url: candidate for candidate in candidates}
    rows = [
        f"{candidate.url}\t{candidate_display(candidate, existing=candidate.number in existing_numbers)}"
        for candidate in candidates
    ]
    command = [
        "fzf",
        "--height=40%",
        "--layout=reverse",
        "--border",
        "--no-multi",
        "--no-preview",
        "--prompt=Review PR> ",
        "--delimiter=\t",
        "--with-nth=2",
        "--accept-nth=1",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=control_dir,
            check=False,
            capture_output=True,
            input="\n".join(rows) + "\n",
            text=True,
        )
    except FileNotFoundError as exc:
        raise CliError("MISSING_COMMAND", "Interactive selection requires fzf on PATH.", fix="Install fzf or pass a PR number/URL.") from exc

    if result.returncode in (1, 130):
        raise SelectionCancelled("Selection cancelled.")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CliError("FZF_FAILED", f"fzf failed with exit status {result.returncode}: {detail}")

    selected_url = result.stdout.strip()
    if selected_url not in by_url:
        raise CliError("FZF_FAILED", "fzf returned an unknown or empty pull-request URL.")
    return by_url[selected_url]


def review_pr(
    pr_selector: str,
    config: Config,
    control_dir: Path,
    *,
    prepare: bool = True,
    herdr: bool = True,
    force_herdr: bool = False,
    focus: bool = True,
    reuse: bool = False,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    number, title, canonical_url = load_pr_details(pr_selector, config, control_dir)
    worktrees = list_worktrees(control_dir)
    ticket = extract_ticket_from_title(title)

    existing = find_existing_pr_worktree(worktrees, number, canonical_url, ticket)
    if existing is not None:
        if not reuse:
            raise CliError(
                "ALREADY_EXISTS",
                f"Review worktree for PR #{number} already exists at {existing.path} (branch: {existing.branch or 'detached'}).",
                fix=f"Pass --reuse or run: cubrid-worktree open {existing.path.name}",
            )
        siblings = find_ticket_siblings(worktrees, config.parent_dir, existing.ticket, exclude_path=existing.path)
        prep_result = (
            {"status": "skipped", "reason": "already prepared"}
            if (not prepare or is_worktree_prepared(existing.path))
            else run_prepare(config, existing.path, dry_run=dry_run)
        )
        if existing.ticket and existing.path.name.casefold().startswith(f"{existing.ticket.casefold()}-"):
            tab_label = existing.path.name[len(existing.ticket) + 1 :]
        elif existing.ticket and existing.path.name.casefold() == existing.ticket.casefold():
            tab_label = None
        else:
            tab_label = existing.path.name
        herdr_result = (
            open_in_herdr(
                existing.path,
                existing.ticket or f"PR-{number}",
                tab_label=tab_label,
                focus=focus,
                new_tab=False,
                force=force_herdr,
                dry_run=dry_run,
                env=env,
            )
            if herdr
            else {"status": "skipped", "reason": "disabled"}
        )
        return {
            "action": "review",
            "created": False,
            "reused": True,
            "pr_number": number,
            "pr_url": canonical_url,
            "title": title,
            "ticket": existing.ticket,
            "name": existing.path.name,
            "branch": existing.branch,
            "role": existing.role or "review",
            "path": os.fspath(existing.path),
            "siblings": siblings,
            "prepare": prep_result,
            "herdr": herdr_result,
            "dry_run": dry_run,
        }

    target = choose_review_target(
        number,
        title,
        lambda name: is_target_occupied(control_dir, config.parent_dir, name, worktrees),
    )
    target_path = config.parent_dir / target.name
    conflict = exact_conflict_error(control_dir, target_path, target.name, worktrees)
    if conflict is not None:
        raise CliError(
            "ALREADY_EXISTS",
            f"Cannot create review worktree '{target.name}': {conflict}",
            fix="Inspect existing worktrees with `cubrid-worktree list`.",
        )

    siblings = find_ticket_siblings(worktrees, config.parent_dir, target.ticket, exclude_path=target_path)
    if dry_run:
        prep_result = (
            run_prepare(config, target_path, dry_run=True)
            if prepare
            else {"status": "skipped", "reason": "disabled"}
        )
        herdr_result = (
            open_in_herdr(
                target_path,
                target.space_label,
                tab_label=target.tab_label,
                focus=focus,
                new_tab=True,
                force=force_herdr,
                dry_run=True,
                env=env,
            )
            if herdr
            else {"status": "skipped", "reason": "disabled"}
        )
        return {
            "action": "review",
            "created": False,
            "reused": False,
            "pr_number": number,
            "pr_url": canonical_url,
            "title": title,
            "ticket": target.ticket,
            "name": target.name,
            "branch": target.name,
            "role": "review",
            "path": os.fspath(target_path),
            "siblings": siblings,
            "prepare": prep_result,
            "herdr": herdr_result,
            "dry_run": True,
        }

    _run_capture(
        [
            "gh",
            "pr",
            "checkout",
            str(number),
            "--repo",
            config.repository,
            "--branch",
            target.name,
            "--worktree",
            os.fspath(target_path),
        ],
        cwd=control_dir,
    )
    try:
        set_branch_pr_url(target_path, target.name, canonical_url)
        set_branch_role(target_path, target.name, "review")
    except CliError as exc:
        raise CliError(
            "METADATA_FAILED",
            f"{exc.message}\nThe review worktree remains at: {target_path}",
            fix=f"Record PR association with: gh-pr-associate {canonical_url}",
        ) from exc

    prep_result = (
        run_prepare(config, target_path, dry_run=False)
        if prepare
        else {"status": "skipped", "reason": "disabled"}
    )
    herdr_result = (
        open_in_herdr(
            target_path,
            target.space_label,
            tab_label=target.tab_label,
            focus=focus,
            new_tab=True,
            force=force_herdr,
            dry_run=False,
            env=env,
        )
        if herdr
        else {"status": "skipped", "reason": "disabled"}
    )
    return {
        "action": "review",
        "created": True,
        "reused": False,
        "pr_number": number,
        "pr_url": canonical_url,
        "title": title,
        "ticket": target.ticket,
        "name": target.name,
        "branch": target.name,
        "role": "review",
        "path": os.fspath(target_path),
        "siblings": siblings,
        "prepare": prep_result,
        "herdr": herdr_result,
        "dry_run": False,
    }


def existing_candidate_numbers(
    candidates: list[ReviewCandidate],
    worktrees: list[WorktreeRecord],
) -> set[int]:
    existing: set[int] = set()
    for candidate in candidates:
        ticket = extract_ticket_from_title(candidate.title)
        if find_existing_pr_worktree(worktrees, candidate.number, candidate.url, ticket) is not None:
            existing.add(candidate.number)
    return existing


def review_all(
    config: Config,
    control_dir: Path,
    *,
    prepare: bool = True,
    herdr: bool = False,
    force_herdr: bool = False,
    focus: bool = True,
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    candidates = load_review_candidates(config, control_dir)
    worktrees = list_worktrees(control_dir)
    existing_nums = existing_candidate_numbers(candidates, worktrees)

    to_create: list[ReviewCandidate] = []
    skipped: list[ReviewCandidate] = []
    for candidate in candidates:
        if candidate.number in existing_nums:
            skipped.append(candidate)
        else:
            to_create.append(candidate)

    created_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    for candidate in to_create:
        try:
            item = review_pr(
                candidate.url,
                config,
                control_dir,
                prepare=prepare,
                herdr=herdr,
                force_herdr=force_herdr,
                focus=focus,
                reuse=False,
                dry_run=dry_run,
                env=env,
            )
            created_items.append(item)
        except CliError as exc:
            failed_items.append(
                {
                    "pr_number": candidate.number,
                    "pr_url": candidate.url,
                    "title": candidate.title,
                    "error": {"code": exc.code, "message": exc.message, "fix": exc.fix},
                }
            )

    payload = {
        "action": "review-all",
        "requested_count": len(candidates),
        "skipped": [
            {"pr_number": c.number, "pr_url": c.url, "title": c.title} for c in skipped
        ],
        "created": created_items,
        "failed": failed_items,
        "dry_run": dry_run,
    }
    return payload, (1 if failed_items else 0)

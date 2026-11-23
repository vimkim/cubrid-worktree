"""Ticket-first worktree/branch naming, validation, and PR selector parsing."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import CliError

JIRA_PREFIX_RE = r"(?:CBRD|APIS|CUBRIDQA|CUBRIDMAN|TOOLS)"
TICKET_KEY_RE = re.compile(rf"^({JIRA_PREFIX_RE}-[1-9][0-9]*)(?:-(.+))?$", re.IGNORECASE)
TICKET_ANY_RE = re.compile(
    rf"(?<![A-Za-z0-9])({JIRA_PREFIX_RE}-[1-9][0-9]*)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
PR_TITLE_TICKET_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9]*-[1-9][0-9]*)\]")
PR_NAME_RE = re.compile(r"^[Pp][Rr]-([1-9][0-9]*)$")
SUMMARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ParsedTarget:
    name: str
    ticket: str | None
    summary: str | None
    space_label: str
    tab_label: str | None


def _validate_segment(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise CliError(
            "INVALID_NAME",
            f"{field} must not be empty.",
            fix=f"Provide a non-empty {field}.",
            exit_code=2,
        )
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise CliError(
            "INVALID_NAME",
            f"{field} '{cleaned}' must not contain '/', '\\\\', or '..' (slashes collide with refs/heads/<ticket>).",
            fix="Use hyphens instead of slashes, e.g. CBRD-28099-bench.",
            exit_code=2,
        )
    if not SUMMARY_RE.match(cleaned) or cleaned.endswith(("-", ".")):
        raise CliError(
            "INVALID_NAME",
            f"{field} '{cleaned}' must match [A-Za-z0-9][A-Za-z0-9._-]* and not end with '-' or '.'.",
            fix="Use short kebab-case ASCII characters such as bench, 11.4, or repro.",
            exit_code=2,
        )
    return cleaned


def _reject_extra_tickets(value: str, *, allow_ticket: str | None = None) -> None:
    found = {match.upper() for match in TICKET_ANY_RE.findall(value)}
    if allow_ticket is not None:
        found.discard(allow_ticket.upper())
    if found:
        joined = ", ".join(sorted(found))
        raise CliError(
            "MULTIPLE_TICKETS",
            f"Suffix '{value}' contains another ticket key ({joined}), which makes ticket detection ambiguous.",
            fix="Keep only one ticket key at the start and use a descriptive summary.",
            exit_code=2,
        )


def normalize_ticket(raw_ticket: str) -> str:
    cleaned = _validate_segment(raw_ticket, field="ticket")
    match = TICKET_KEY_RE.match(cleaned)
    if match is None or match.group(2) is not None or PR_NAME_RE.match(cleaned):
        raise CliError(
            "INVALID_TICKET",
            f"Expected a JIRA ticket key like CBRD-28099, got '{raw_ticket}'.",
            fix="Pass a ticket key such as CBRD-28099.",
            exit_code=2,
        )
    return match.group(1).upper()


def parse_target(raw_name: str, summary: str | None = None) -> ParsedTarget:
    cleaned_name = _validate_segment(raw_name, field="worktree name")
    cleaned_summary = _validate_segment(summary, field="summary") if summary is not None else None
    if cleaned_summary is not None:
        _reject_extra_tickets(cleaned_summary)

    pr_match = PR_NAME_RE.match(cleaned_name)
    if pr_match is not None:
        if cleaned_summary is not None:
            raise CliError(
                "INVALID_NAME",
                f"Unticketed PR name '{cleaned_name}' does not accept --summary.",
                fix="Use PR-<N> directly or specify a ticketed name like CBRD-28099-summary.",
                exit_code=2,
            )
        canonical = f"PR-{int(pr_match.group(1))}"
        return ParsedTarget(
            name=canonical,
            ticket=None,
            summary=None,
            space_label=canonical,
            tab_label=None,
        )

    ticket_match = TICKET_KEY_RE.match(cleaned_name)
    if ticket_match is not None:
        ticket = ticket_match.group(1).upper()
        inline_summary = ticket_match.group(2)
        if inline_summary is not None:
            _validate_segment(inline_summary, field="summary")
            _reject_extra_tickets(inline_summary, allow_ticket=ticket)
            if cleaned_summary is not None and cleaned_summary != inline_summary:
                raise CliError(
                    "CONFLICTING_SUMMARY",
                    f"Target '{cleaned_name}' already has summary '{inline_summary}', conflicting with --summary '{cleaned_summary}'.",
                    fix="Pass either CBRD-xxxxx-summary or CBRD-xxxxx --summary <summary>.",
                    exit_code=2,
                )
            effective_summary = inline_summary
        else:
            effective_summary = cleaned_summary

        final_name = f"{ticket}-{effective_summary}" if effective_summary else ticket
        return ParsedTarget(
            name=final_name,
            ticket=ticket,
            summary=effective_summary,
            space_label=ticket,
            tab_label=effective_summary,
        )

    distinct_embedded = {m.upper() for m in TICKET_ANY_RE.findall(cleaned_name)}
    if len(distinct_embedded) > 1:
        raise CliError(
            "MULTIPLE_TICKETS",
            f"Name '{cleaned_name}' contains multiple distinct ticket keys: {', '.join(sorted(distinct_embedded))}.",
            fix="Use CBRD-xxxxx or CBRD-xxxxx-<summary>.",
            exit_code=2,
        )
    embedded_ticket = next(iter(distinct_embedded), None)
    final_name = f"{cleaned_name}-{cleaned_summary}" if cleaned_summary else cleaned_name
    space_label = embedded_ticket or final_name
    tab_label = cleaned_summary if embedded_ticket is None else (final_name if final_name != embedded_ticket else None)
    return ParsedTarget(
        name=final_name,
        ticket=embedded_ticket,
        summary=cleaned_summary,
        space_label=space_label,
        tab_label=tab_label,
    )


def parse_pr_selector(value: str, repository: str = "CUBRID/cubrid") -> tuple[int, str]:
    cleaned = value.strip()
    if not cleaned:
        raise CliError(
            "INVALID_PR",
            "Pull request selector must not be empty.",
            fix=f"Pass a PR number (e.g. 7887) or URL (https://github.com/{repository}/pull/7887).",
            exit_code=2,
        )
    if "/" not in repository:
        raise CliError(
            "INVALID_REPO",
            f"Repository '{repository}' must use OWNER/REPO format.",
            fix="Pass --repo CUBRID/cubrid.",
            exit_code=2,
        )
    repo_owner, repo_name = repository.split("/", 1)

    if cleaned.isdigit() and int(cleaned) > 0:
        number = int(cleaned)
        return number, f"https://github.com/{repository}/pull/{number}"

    parsed = urlsplit(cleaned)
    parts = [part for part in parsed.path.split("/") if part]
    valid_path = (
        len(parts) == 4
        and parts[0].casefold() == repo_owner.casefold()
        and parts[1].casefold() == repo_name.casefold()
        and parts[2] == "pull"
        and parts[3].isdigit()
        and int(parts[3]) > 0
    )
    if parsed.scheme != "https" or parsed.netloc.casefold() != "github.com" or not valid_path:
        raise CliError(
            "INVALID_PR",
            f"Expected a {repository} PR number or URL, got '{value}'.",
            fix=f"Example: https://github.com/{repository}/pull/7887 or 7887",
            exit_code=2,
        )
    number = int(parts[3])
    return number, f"https://github.com/{repository}/pull/{number}"


def extract_ticket_from_title(title: str) -> str | None:
    match = PR_TITLE_TICKET_RE.search(title)
    if match is None:
        return None
    return match.group(1).upper()


def extract_single_ticket(text: str) -> str | None:
    found = {m.upper() for m in TICKET_ANY_RE.findall(text)}
    if len(found) == 1:
        return next(iter(found))
    return None


def choose_review_target(
    pr_number: int,
    title: str,
    is_occupied: Callable[[str], bool],
) -> ParsedTarget:
    ticket = extract_ticket_from_title(title)
    if ticket is None:
        return parse_target(f"PR-{pr_number}")
    if not is_occupied(ticket):
        return parse_target(ticket)
    return parse_target(f"{ticket}-pr-{pr_number}")

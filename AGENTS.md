# cubrid-worktree

## Overview

`cubrid-worktree` is a standalone, Python standard-library CLI for managing ticket-first CUBRID Git worktrees, build preparation, PR review checkouts, and Herdr workspace/tab placement from any working directory.

## Public repository hygiene

This repository is public. Do not hardcode user-specific home paths, private/internal hostnames or URLs, or credentials. Keep defaults portable (`~` / environment overrides) and treat external tools (`just`, `gh`, `fzf`, `herdr`, `herdr-next-to-current`, `cubrid-dev2-pr`) as configurable or optional integrations.

## Development and testing

- Keep runtime dependencies empty (`dependencies = []` in `pyproject.toml`; Python 3.10+ stdlib only).
- Run `just test` (or `just check`) before committing.
- Never invoke live `herdr` or `herdr-next-to-current` mutations in tests; always mock external UI/network commands and use temporary Git repositories for Git tests.
- Install locally with `just install` (aliased to `just sync` for `daily-update-cubrid` compatibility).

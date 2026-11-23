# Show available commands.
default:
    @just --list

alias install := sync

# Install or refresh cubrid-worktree via uv for local use and daily-update-cubrid.
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_TOOL_BIN_DIR="${CUBRID_WORKTREE_BIN_DIR:-$(uv tool dir --bin)}"
    destination="$UV_TOOL_BIN_DIR/cubrid-worktree"
    uv tool install --reinstall .
    "$destination" --help >/dev/null
    printf 'Installed %s\n' "$destination"

# Run the unit and integration test suite.
test *args:
    python3 -m unittest discover -s tests -v {{args}}

# Run tests and smoke-check the module CLI entrypoint.
check: test
    python3 -m cubrid_worktree --help >/dev/null

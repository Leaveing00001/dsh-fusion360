#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Register the Fusion 360 MCP server with DSH.

Appends the ``mcp-fusion360`` patch entry to a DSH profile's patch layer, with
the three machine-specific paths (interpreter, server script, cwd) already
filled in, so nothing has to be hand-edited.

    python tools\\install_dsh_mcp.py                 # normal
    python tools\\install_dsh_mcp.py --dry-run       # show the plan, write nothing
    python tools\\install_dsh_mcp.py --profile other # a non-default profile
    python tools\\install_dsh_mcp.py --force         # replace an existing entry

Exit codes:
    0 = registered (or already registered and left alone)
    1 = could not register - see the printed reason

The patch file is a top-level YAML array, so appending a ``- insert:`` block
needs no YAML parser and no rewriting of what is already there.  A timestamped
backup is written before anything is modified.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

ENTRY_ID = "mcp-fusion360"
SERVER_NAME = "fusion360"
DEFAULT_PORT = 27182

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_SCRIPT = os.path.join(REPO_ROOT, "mcp_server", "fusion_mcp_server.py")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def patch_path(profile: str) -> str:
    return os.path.join(
        os.environ.get("USERPROFILE", ""), ".dsh", "profiles", profile,
        "cordis.patch.yml")


def find_python(explicit: str | None) -> str:
    """Pick the interpreter DSH should spawn.

    ``sys.executable`` is right when this script was run with a real Python.
    It is wrong when it is the Microsoft Store stub, which cannot run a server
    script, so fall back to whatever ``python`` resolves to on PATH.
    """
    if explicit:
        return explicit

    candidate = sys.executable or ""
    if candidate and "WindowsApps" not in candidate:
        return candidate

    found = shutil.which("python") or shutil.which("python3")
    return found or candidate


def render_entry(python_exe: str, port: int) -> str:
    # YAML single-quoted strings take backslashes literally, which is exactly
    # what Windows paths need.
    return f"""
# Fusion 360 bridge for DSH - added by tools/install_dsh_mcp.py
#
# `insert` appends rows to the profile's plugin tree; `name` names the DSH
# package to instantiate and `config` is handed to it.  The mcp-client package
# exposes each server tool as mcp__<serverName>__<tool>.
- insert:
    - id: {ENTRY_ID}
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: {SERVER_NAME}
        transport: stdio
        command: '{python_exe}'
        args:
          - '{SERVER_SCRIPT}'
        cwd: '{REPO_ROOT}'
        env:
          FUSION_DSH_BRIDGE_HOST: '127.0.0.1'
          FUSION_DSH_BRIDGE_PORT: '{port}'
        # Modelling calls are slow, and the first call after a cold start has
        # been measured at ~24 s.  The default would time out.
        toolCallTimeoutMs: 120000
        # Leave false so a Fusion-less start still boots DSH; the tools simply
        # report that the bridge is unreachable until Fusion and the add-in run.
        failOnStartupError: false
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register the Fusion 360 MCP server with DSH.")
    parser.add_argument("--profile", default="desktop",
                        help="DSH profile name (default: desktop)")
    parser.add_argument("--python", default=None,
                        help="interpreter for DSH to spawn (default: this one)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"bridge port (default: {DEFAULT_PORT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="replace an existing entry instead of stopping")
    args = parser.parse_args()

    say()
    say("  Register the Fusion 360 MCP server with DSH")
    say("  " + "=" * 56)

    if not os.path.isfile(SERVER_SCRIPT):
        say(f"  FAIL  server script not found: {SERVER_SCRIPT}")
        say("        Run this from a checkout of dsh-fusion360.")
        return 1

    python_exe = find_python(args.python)
    if not python_exe or not os.path.isfile(python_exe):
        say(f"  FAIL  no usable Python interpreter found (got: {python_exe!r})")
        say("        Pass one explicitly with --python <path>.")
        return 1

    target = patch_path(args.profile)
    say(f"  python : {python_exe}")
    say(f"  server : {SERVER_SCRIPT}")
    say(f"  cwd    : {REPO_ROOT}")
    say(f"  patch  : {target}")

    existing = ""
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as handle:
            existing = handle.read()
    else:
        say()
        say(f"  note: {target} does not exist yet; it will be created.")

    if ENTRY_ID in existing and not args.force:
        say()
        say(f"  OK    '{ENTRY_ID}' is already present - nothing to do.")
        say("        Re-run with --force to replace it (useful after moving")
        say("        this checkout or switching interpreter).")
        return 0

    if ENTRY_ID in existing and args.force:
        say()
        say(f"  note: --force given; the existing '{ENTRY_ID}' block will be")
        say("        left in place and a second one appended.  DSH applies")
        say("        patch rows in order, so the later entry wins - but if you")
        say("        want the file tidy, remove the old block by hand first.")
        say("        (This script does not edit YAML it did not write.)")

    entry = render_entry(python_exe, args.port)

    if args.dry_run:
        say()
        say("  DRY RUN - nothing written.  This is what would be appended:")
        say()
        say(entry)
        return 0

    if existing:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{target}.bak-{stamp}"
        shutil.copy2(target, backup)
        say(f"  backup : {backup}")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(entry)

    say()
    say("  OK    registered.  Next:")
    say()
    say("        1. Restart DSH - a newly added row is NOT hot-loaded.")
    say("             python tools\\restart_dsh.py")
    say("        2. Install the add-in, if you have not already:")
    say("             powershell -ExecutionPolicy Bypass -File tools\\install_addin.ps1")
    say("        3. Start Fusion 360, then ask DSH for fusion_status.")
    say()
    return 0


if __name__ == "__main__":
    sys.exit(main())

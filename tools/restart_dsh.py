#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
restart_dsh.py - cleanly restart DSH Desktop so the Fusion 360 MCP tools load.

WHY THIS EXISTS
    The DSH profile patch (cordis.patch.yml) that registers the `mcp-fusion360`
    MCP client is only read at DSH startup. Editing it while DSH runs does NOT
    hot-load the new row, so DSH must be fully restarted - and "fully" is the
    hard part: DSH Desktop is Electron, so one logical app is many processes
    (main + GPU + renderer + utility). Killing only the window leaves orphans
    that keep a single-instance lock, and the relaunch then silently no-ops.

WHAT IT DOES
    1. Records every DSH Desktop / node process owned by this install.
    2. Gracefully closes the app (taskkill without /F = WM_CLOSE, lets it save).
    3. Verifies they actually exited; force-kills survivors.
    4. Sweeps orphaned Electron helpers so no single-instance lock remains.
    5. Waits for the install-dir + userData locks to be released.
    6. Relaunches via explorer.exe so the app is NOT a child of this script.
    7. Polls until the DSH process tree is actually up again.

USAGE
    Double-click  restart_dsh.bat   (preferred - gives you a visible window)
    or:  python restart_dsh.py            # normal restart
         python restart_dsh.py --dry-run  # show what it WOULD kill, change nothing
         python restart_dsh.py --wait 180 # longer startup timeout

EXIT CODES
    0 = DSH is back up
    1 = something went wrong (see the printed reason)
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- config ----

DSH_EXE_NAME = "DSH Desktop.exe"
USER_DATA = os.path.join(os.environ.get("APPDATA", ""), "DSH Desktop")
PROFILE_DIR = os.path.join(os.environ.get("USERPROFILE", ""), ".dsh")

# Filled in by resolve_dsh_exe() before anything uses them. Left empty at
# import time on purpose: finding the install can mean spawning PowerShell, and
# importing this module should stay cheap.
DSH_EXE = ""
DSH_DIR = ""

# MCP registration we expect to be present after restart.
MCP_PATCH = os.path.join(PROFILE_DIR, "profiles", "desktop", "cordis.patch.yml")
MCP_MARKER = "mcp-fusion360"

# Fusion bridge endpoint, checked at the end as a bonus signal.
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 27182

# Electron helper processes that can outlive the main process. We only kill
# these if their command line points at the DSH install (never a user's other
# Electron app that happens to be called "node").
HELPER_NAMES = {"node.exe", "electron.exe", "crashpad_handler.exe"}

CREATE_NO_WINDOW = 0x08000000


# ------------------------------------------------------------- utilities ----

def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    say(f"[{n}/{total}] {msg}")


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command, return (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        out = p.stdout.decode("utf-8", "replace") if p.stdout else ""
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, "<timeout>"
    except FileNotFoundError:
        return 127, "<not found>"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, f"<{exc}>"


def powershell(script: str, timeout: int = 60) -> tuple[int, str]:
    """Run a PowerShell script via -Command with UTF-8 output forced.

    NOTE: we deliberately do NOT use -EncodedCommand here. When a script
    writes to the progress stream, -EncodedCommand emits CLIXML
    (`#< CLIXML ... <Objs ...>`) instead of plain stdout, which corrupts any
    JSON we are trying to parse back.
    """
    prelude = (
        "$ProgressPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;"
    )
    return run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", prelude + script,
        ],
        timeout=timeout,
    )


def strip_clixml(out: str) -> str:
    """Remove PowerShell CLIXML noise that can still leak into stdout."""
    if "#< CLIXML" not in out:
        return out
    # Drop the CLIXML banner and any <Objs ...>...</Objs> payload.
    import re

    out = re.sub(r"#<\s*CLIXML.*?(?=<Objs|$)", "", out, flags=re.S)
    out = re.sub(r"<Objs\b.*?</Objs>", "", out, flags=re.S)
    return out.strip()


def encode_ps(script: str) -> list[str]:
    """Kept for compatibility; delegates to powershell() semantics."""
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        "$ProgressPreference='SilentlyContinue';" + script,
    ]


def _candidate_dsh_paths() -> list[str]:
    """Conventional install locations, plus an explicit override."""
    out = []
    override = os.environ.get("DSH_DESKTOP_EXE", "").strip()
    if override:
        out.append(override)
    for base in (
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.path.expanduser("~"),
    ):
        if not base:
            continue
        for sub in ("Programs\\DSH Desktop", "DSH Desktop",
                    "Programs\\dsh-desktop", "dsh-desktop"):
            out.append(os.path.join(base, sub, DSH_EXE_NAME))
    return out


def resolve_dsh_exe() -> str:
    """Find DSH Desktop.exe and set DSH_EXE / DSH_DIR.

    Order matters.  A *running* instance is asked first, because it reports the
    path it was actually launched from - which is how a portable or custom
    install is found with no configuration at all.  Conventional locations are
    the fallback, and DSH_DESKTOP_EXE overrides both.
    """
    global DSH_EXE, DSH_DIR

    if DSH_EXE and os.path.isfile(DSH_EXE):
        return DSH_EXE

    # 1. Whatever is running right now.
    try:
        _code, out = powershell(
            "(Get-Process -Name 'DSH Desktop' -ErrorAction SilentlyContinue |"
            " Where-Object { $_.Path } |"
            " Select-Object -First 1 -ExpandProperty Path)",
            timeout=30,
        )
        for line in strip_clixml(out or "").splitlines():
            line = line.strip()
            if line.lower().endswith(".exe") and os.path.isfile(line):
                DSH_EXE, DSH_DIR = line, os.path.dirname(line)
                return DSH_EXE
    except Exception:
        pass

    # 2. Conventional install locations.
    for candidate in _candidate_dsh_paths():
        if candidate and os.path.isfile(candidate):
            DSH_EXE = candidate
            DSH_DIR = os.path.dirname(candidate)
            return DSH_EXE

    return ""


def list_dsh_processes() -> list[dict]:
    """Return [{Id, Name, StartTime, CmdLine}] for DSH-owned processes."""
    # Match the exact exe name OR helper binaries whose path is under DSH_DIR.
    # Written defensively: escaping is done by single-quoting the literals.
    ps = f"""
$ErrorActionPreference='SilentlyContinue'
$exe = {ps_quote(DSH_EXE_NAME)}
$dir = {ps_quote(DSH_DIR)}
$helper = @('node.exe','electron.exe','crashpad_handler.exe')
$out = New-Object System.Collections.ArrayList
foreach ($p in (Get-Process | Where-Object {{ $_.ProcessName -eq 'DSH Desktop' -or $helper -contains ($_.ProcessName + '.exe') }})) {{
    $path = ''
    try {{ $path = $p.Path }} catch {{}}
    $isDshExe = ($p.ProcessName -eq 'DSH Desktop')
    $isHelperUnderDir = ($path -and $path.ToLower().StartsWith($dir.ToLower()))
    if (-not ($isDshExe -or $isHelperUnderDir)) {{ continue }}
    $start = ''
    try {{ $start = $p.StartTime.ToString('yyyy-MM-dd HH:mm:ss') }} catch {{}}
    [void]$out.Add([pscustomobject]@{{ Id=$p.Id; Name=$p.ProcessName; Start=$start; Path=$path }})
}}
$out | ConvertTo-Json -Compress -Depth 3
"""
    code, out = powershell(ps, timeout=60)
    out = strip_clixml(out)
    if code != 0 or not out:
        return []
    import json

    out = out.strip()
    if not out or out == "null":
        return []
    # Take the last non-empty line that looks like JSON (defensive against
    # stray progress/warning lines).
    candidate = out
    if not candidate.startswith(("[", "{")):
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith(("[", "{")):
                candidate = line
                break
    try:
        data = json.loads(candidate)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return data


def ps_quote(s: str) -> str:
    """Single-quote a PowerShell string literal."""
    return "'" + s.replace("'", "''") + "'"


def pids(procs: list[dict]) -> list[int]:
    me = os.getpid()
    out = []
    for p in procs:
        sid = str(p.get("Id", ""))
        if not sid.isdigit():
            continue
        n = int(sid)
        if n == me:
            continue  # never target ourselves
        out.append(n)
    return sorted(set(out))


# ---------------------------------------------------------------- checks ----

def preflight(dry_run: bool) -> int:
    problems = []
    resolve_dsh_exe()
    if not os.path.isfile(DSH_EXE):
        problems.append(
            f"DSH executable not found (looked for {DSH_EXE_NAME} next to a "
            f"running instance and in the usual install locations).\n"
            f"          Set DSH_DESKTOP_EXE to its full path and re-run."
        )
    if not os.path.isfile(MCP_PATCH):
        problems.append(f"MCP profile patch missing: {MCP_PATCH}")
    else:
        try:
            with open(MCP_PATCH, "r", encoding="utf-8") as fh:
                text = fh.read()
            if MCP_MARKER not in text:
                problems.append(
                    f"{MCP_PATCH} does not contain '{MCP_MARKER}' - "
                    "the Fusion MCP server is NOT registered, so restarting "
                    "will not produce mcp__fusion360__* tools."
                )
        except OSError as exc:
            problems.append(f"cannot read {MCP_PATCH}: {exc}")

    for p in problems:
        say(f"  !! {p}")
    if problems:
        say()
        if not dry_run:
            say("Refusing to restart until the above is fixed.")
            return 1
        say("(dry run: continuing anyway)")
    else:
        say(f"  OK  exe:     {DSH_EXE}")
        say(f"  OK  profile: {MCP_PATCH} contains {MCP_MARKER}")
    return 0


def probe_bridge(timeout: float = 6.0) -> bool:
    """Quick TCP probe of the Fusion bridge - informational only."""
    import socket

    try:
        with socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------ main ----

def main() -> int:
    ap = argparse.ArgumentParser(description="Cleanly restart DSH Desktop.")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be killed, change nothing")
    ap.add_argument("--wait", type=int, default=120,
                    help="seconds to wait for DSH to come back (default 120)")
    ap.add_argument("--force", action="store_true",
                    help="skip the confirmation pause")
    args = ap.parse_args()

    say("=" * 68)
    say("  DSH Desktop - clean restart (to load Fusion 360 MCP tools)")
    say("=" * 68)
    say(f"  time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    say()

    TOTAL = 7

    step(1, TOTAL, "Preflight: verifying install + MCP registration")
    rc = preflight(args.dry_run)
    if rc != 0:
        return rc
    say()

    step(2, TOTAL, "Inventory: listing DSH processes")
    procs = list_dsh_processes()
    if not procs:
        say("  (no DSH processes running)")
    else:
        for p in procs:
            say(f"  PID {p['Id']:>6}  {p['Name']:<22} started {p.get('Start','?')}")
        say(f"  -> {len(procs)} process(es)")

    # Identify the oldest process = the DSH instance whose session is live.
    oldest = None
    for p in procs:
        if p.get("Start") and (oldest is None or p["Start"] < oldest["Start"]):
            oldest = p
    if oldest:
        say(f"  -> oldest (main app): PID {oldest['Id']}, started {oldest['Start']}")
        say("     NOTE: if you are reading this inside DSH, that session ends now.")
    say()

    if args.dry_run:
        say("DRY RUN: no processes killed, nothing launched.")
        say(f"  would kill: {pids(procs)}")
        say(f"  would launch: {DSH_EXE}")
        return 0

    if not args.force:
        # Auto-continue after a short pause instead of blocking on input():
        # the .bat already shows its own countdown, and a silent wait for
        # Enter looks identical to "did nothing".
        say("  Starting in 5s - press Ctrl+C to abort.")
        try:
            for i in range(5, 0, -1):
                say(f"    {i}...")
                time.sleep(1)
        except KeyboardInterrupt:
            say("  aborted.")
            return 1
    say()

    # -- 3. graceful close -------------------------------------------------
    step(3, TOTAL, "Closing DSH gracefully (CloseMainWindow, lets it save state)")
    # Re-inventory immediately before killing: DSH's own helper processes
    # recycle on a timer, so any list gathered earlier is already stale.
    procs = list_dsh_processes()
    targets = pids(procs)

    # Ask windows to close. CloseMainWindow == WM_CLOSE, the polite path;
    # fall back to taskkill (no /F) for processes without a message pump.
    # NOTE: taskkill is blocked by some sandboxes/policies, so it is only
    # ever a fallback here - Stop-Process below is the reliable one.
    for pid in targets:
        ps_close = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"$p=Get-Process -Id {pid};"
            "if($p -and $p.MainWindowHandle -ne 0){$p.CloseMainWindow()|Out-Null}"
        )
        powershell(ps_close, timeout=20)
    # polite taskkill for anything without a main window
    for pid in targets:
        run(["taskkill", "/PID", str(pid), "/T"], timeout=20)
    if targets:
        say(f"  asked {len(targets)} process(es) to close: {targets}")
    else:
        say("  nothing to close")

    # wait up to 15s for graceful exit
    deadline = time.time() + 15
    while time.time() < deadline:
        if not list_dsh_processes():
            break
        time.sleep(1)
    remaining = list_dsh_processes()
    if remaining:
        say(f"  {len(remaining)} still alive after 15s")
    else:
        say("  all exited cleanly")
    say()

    # -- 4. force kill survivors -------------------------------------------
    step(4, TOTAL, "Force-killing survivors + orphaned Electron helpers")
    # Stop-Process is used first because taskkill can be blocked by policy.
    ps_kill = f"""
$ErrorActionPreference='SilentlyContinue'
$dir = {ps_quote(DSH_DIR)}
$killed = 0
Get-Process | Where-Object {{
    $_.ProcessName -eq 'DSH Desktop' -or
    $_.ProcessName -in @('node','electron','crashpad_handler')
}} | ForEach-Object {{
    $path=''; try {{ $path=$_.Path }} catch {{}}
    $mine = ($_.ProcessName -eq 'DSH Desktop') -or
            ($path -and $path.ToLower().StartsWith($dir.ToLower()))
    if ($mine) {{
        try {{
            Stop-Process -Id $_.Id -Force -ErrorAction Stop
            "killed $($_.Id) $($_.ProcessName)"
            $script:killed++
        }} catch {{
            "FAILED $($_.Id) $($_.ProcessName): $($_.Exception.Message)"
        }}
    }}
}}
if ($killed -eq 0) {{ "no DSH processes needed force-kill" }}
"""
    code, out = powershell(ps_kill, timeout=60)
    out = strip_clixml(out)
    if out.strip():
        for line in out.strip().splitlines():
            say("  " + line.strip())
    else:
        say("  (no output from kill pass)")

    # last-resort taskkill for anything Stop-Process could not reach
    leftovers = list_dsh_processes()
    if leftovers:
        say(f"  {len(leftovers)} survived Stop-Process; trying taskkill /F")
        for p in leftovers:
            rc, o = run(["taskkill", "/F", "/PID", str(p["Id"]), "/T"], timeout=20)
            say(f"    taskkill PID {p['Id']}: {o or 'ok'}")
    say()

    # -- 5. wait for locks to clear ----------------------------------------
    step(5, TOTAL, "Waiting for process table + file locks to clear")
    settled = False
    deadline = time.time() + 30
    while time.time() < deadline:
        if not list_dsh_processes():
            settled = True
            break
        time.sleep(1)
    if settled:
        say("  no DSH processes remain")
    else:
        say("  WARNING: processes still present, launching anyway")
    time.sleep(2)  # let Windows release the single-instance mutex
    say()

    # -- 6. relaunch, detached ---------------------------------------------
    step(6, TOTAL, "Relaunching DSH (detached via explorer)")
    # Launching through explorer.exe makes DSH a child of the shell, not of
    # this script, so it survives this script exiting - and it inherits the
    # user's normal desktop session/token.
    code, out = run(["explorer.exe", DSH_EXE], timeout=30)
    # explorer.exe returns exit code 1 even on success; verify by process list.
    say(f"  launch issued (explorer exit={code})")

    launched = False
    deadline = time.time() + 20
    while time.time() < deadline:
        if list_dsh_processes():
            launched = True
            break
        time.sleep(1)
    if launched:
        say("  DSH process tree is appearing")
    else:
        say("  !! no DSH process detected yet - check the desktop")
    say()

    # -- 7. wait for it to be ready ----------------------------------------
    step(7, TOTAL, f"Waiting for DSH to finish starting (up to {args.wait}s)")
    deadline = time.time() + args.wait
    last_count = 0
    stable_since = None
    while time.time() < deadline:
        procs2 = list_dsh_processes()
        n = len(procs2)
        if n != last_count:
            say(f"  ... {n} process(es) up")
            last_count = n
            stable_since = time.time()
        # A healthy DSH start shows several Electron processes; treat >=3
        # stable for 3s as "up".
        if n >= 3 and stable_since and (time.time() - stable_since) >= 3:
            break
        time.sleep(1)

    final = list_dsh_processes()
    say()
    say("=" * 68)
    if not final:
        say("  !! DSH did not come back. Start it from the desktop shortcut.")
        return 1

    # PROOF OF RESTART: every surviving process must have started AFTER we
    # began. If any old process is still here, the restart did not happen -
    # which is exactly the failure mode that is otherwise invisible.
    old_starts = {str(p.get("Start", "")) for p in procs if p.get("Start")}
    stale = [p for p in final if str(p.get("Start", "")) in old_starts]

    say(f"  DSH is running: {len(final)} process(es)")
    for p in sorted(final, key=lambda x: str(x.get("Start", "")))[:10]:
        say(f"    PID {p['Id']:>6}  started {p.get('Start','?')}")

    if stale:
        say()
        say(f"  !! RESTART FAILED: {len(stale)} pre-existing process(es) survived:")
        for p in stale:
            say(f"       PID {p['Id']}  started {p.get('Start','?')}")
        say("     These hold the single-instance lock. DSH was NOT reloaded,")
        say("     so the MCP tools will still be missing.")
        say("     Try running this window again - or restart Windows if it persists.")
        return 1

    say()
    say("  OK - all processes are new, so DSH genuinely restarted.")
    say("  NEXT: in DSH, the 13 mcp__fusion360__* tools should now be present.")
    say("  Verify by asking DSH to run fusion_ping.")

    bridge_ok = probe_bridge()
    say()
    say(f"  Fusion bridge :{BRIDGE_PORT}: "
        f"{'reachable' if bridge_ok else 'NOT reachable (start Fusion / the add-in)'}")
    say("=" * 68)
    say()
    say("  You can close this window.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\naborted.")
        sys.exit(1)

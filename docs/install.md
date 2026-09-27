# Install

Three things have to be true before DSH can drive Fusion:

1. the **add-in** is loaded inside Fusion 360,
2. the **MCP server** is registered in a DSH profile,
3. DSH has been **restarted** since that registration.

Nothing here needs administrator rights.

---

## 1. Load the add-in in Fusion 360

Run the installer, which copies the add-in into Fusion's own `API/AddIns` folder
and flips `runOnStartup` on, so Fusion loads it automatically from then on:

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1
```

It prints the copied files, then verifies four things about the installed
manifest: `type`, `id`, `version`, and that `runOnStartup` is `true`, that the
braces balance, and that the file carries no BOM (Fusion parses it as JSON and
will silently refuse a BOM-prefixed manifest).

Useful switches:

| Switch | Effect |
|---|---|
| `-Source <dir>` | Install from a different copy of `fusion_addin/` |
| `-Target <dir>` | Install somewhere other than Fusion's AddIns folder |
| `-NoAutoStart` | Install with `runOnStartup: false` |

Then start Fusion 360 and confirm the add-in appears under
**Utilities → Add-Ins → Add-Ins tab**. If it is listed but not running, select it
and click **Run**.

<details>
<summary>Alternative: load it from the workspace without installing</summary>

1. **Utilities → Add-Ins → Scripts and Add-Ins** (or `Shift+S`).
2. Open the **Add-Ins** tab, click the green **`+`**, and select `fusion_addin/`
   — the folder holding `FusionDSHBridge.manifest`.
3. Select **FusionDSHBridge** and click **Run**.

Fusion does not require an add-in to live in its own `API/AddIns` directory. This
is the path to use while editing the add-in, since Fusion then runs the file you
are editing instead of a copy of it.
</details>

---

## 2. Register the MCP server with DSH

The easy way — fills in every machine-specific path for you, appends the entry to
your profile's patch layer, and backs the file up first:

```powershell
python tools\install_dsh_mcp.py
```

It is idempotent: run it twice and the second run reports that the entry is
already there and changes nothing. `--dry-run` prints what it would append without
writing, `--profile <name>` targets a profile other than `desktop`, and
`--python <path>` overrides the interpreter it picks for DSH to spawn.

Then skip to [step 3](#3-restart-dsh).

<details>
<summary>Doing it by hand instead</summary>

Open your DSH profile patch file:

```
%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml
```

That file is a **top-level YAML array** of loader patch entries. Append the
contents of [`dsh/cordis.patch.yml`](../dsh/cordis.patch.yml) to it, and edit the
three machine-specific values first:

| Value | Set it to |
|---|---|
| `command` | Absolute path to a Python 3.8+ interpreter |
| `args[0]` | Absolute path to `mcp_server/fusion_mcp_server.py` |
| `cwd` | Absolute path to this repo |
</details>

`- insert:` appends rows to the profile's plugin tree. `name` names the DSH
package to instantiate — `@deepseek-ai/dsh-mcp-client` ships with DSH — and
`config` is handed to it. The resulting tools are namespaced
`mcp__fusion360__<tool>`.

Two settings are deliberate:

- **`toolCallTimeoutMs: 120000`** — modelling calls are slow, and the first call
  after a cold start has been measured at ~24 s. The default would time out.
- **`failOnStartupError: false`** — keeps DSH booting when Fusion is closed. The
  tools appear regardless and report the bridge as unreachable.

<details>
<summary>If your profile patch file does not exist yet</summary>

`install_dsh_mcp.py` creates it, including any missing parent directories. If you
are writing it by hand, note that a patch layer is a top-level YAML **array** — a
bare `- insert:` block is a valid one-element array, so an empty file needs no
`[]` placeholder in front of it.
</details>

---

## 3. Restart DSH

**This step is not optional.** The plugin tree is read at startup, so a newly
added row is *not* picked up by the live patch-reload layer — `patchReload: live`
reloads changes to rows that already exist, not new ones.

Either restart DSH Desktop normally, or use the helper, which proves the restart
actually happened rather than assuming it:

```powershell
python tools\restart_dsh.py
```

It stops the process tree, waits for the single-instance lock to clear, relaunches
the app, and then verifies the new process is a **different** process from the one
it killed. `--dry-run` shows the plan without doing it; `--wait` waits for the new
instance to come up.

On Windows this matters more than it looks: DSH Desktop runs as several Electron
processes, and killing only the window leaves orphans holding the single-instance
lock, so the relaunch silently reattaches to the old instance instead of loading
the new config. `tools\restart_dsh.bat` is a double-clickable wrapper around the
same script.

---

## 4. Verify

Start Fusion 360, then ask DSH to call `mcp__fusion360__fusion_status`. A healthy
reply looks like:

```json
{
  "bridge": "FusionDSHBridge",
  "bridgeVersion": "1.0.3",
  "fusionVersion": "2705.1.15",
  "user": "you@example.com",
  "host": "127.0.0.1",
  "port": 27182
}
```

You can also check the bridge directly, bypassing DSH entirely:

```powershell
python tools\probe_bridge.py --cmd ping
```

If `ping` answers but the DSH tools do not exist, the problem is step 2 or 3. If
the DSH tools exist but report the bridge unreachable, the problem is step 1.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `cannot reach the Fusion 360 bridge` | Fusion is closed, or the add-in is not started |
| Tools missing from DSH entirely | DSH was not restarted after the profile edit |
| `timed out waiting for Fusion's primary thread` | A modal dialog is blocking Fusion; dismiss it |
| Add-in starts but nothing listens | Port already in use — check `bridge.log` |
| `no active Fusion design is open` | No design document is open in Fusion |
| First tool call takes ~24 s | Cold start. It converges after one call |
| Calls hang while you use the mouse | Expected — see below |

**Do not drive Fusion with the mouse while DSH is working.** The Fusion API is
single-threaded, so a user interaction queues ahead of or behind the bridge's
work. It will not break anything; calls just get slower, and a modal dialog will
block them until it is dismissed.

### Checking the port yourself

```
netstat -ano | findstr :27182
```

On Windows, `Get-NetTCPConnection -LocalPort 27182` has been observed reporting
nothing while the port was demonstrably listening — prefer `netstat -ano` or
`probe_bridge.py`.

### Fusion was closed and reopened

The add-in loads automatically via `runOnStartup`, so it comes back on its own.
Fusion takes roughly **85 seconds** from a cold start before the port is
listening; ping it rather than assuming the install is broken.

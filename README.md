# dsh-fusion360

**Drive Autodesk Fusion 360 from DeepSeek Harness.** Query the model, change
parameters, export files, and run arbitrary Fusion API Python — from a chat.

[中文说明](README.zh-CN.md) · [Install](docs/install.md) · [Verification](docs/verification.md) · [Fusion API notes](docs/fusion-api-notes.md)

---

> ## 🐋 This is a DeepSeek Harness VibeCoding artifact
>
> This project was **vibe-coded end to end by DeepSeek Harness (DSH)**. Nobody
> wrote it by hand and then handed it to an agent to tidy up: the agent built it
> in conversation, driving a live Fusion 360 through **the very bridge it was
> still writing**.
>
> Concretely, the agent:
>
> - read DSH's own `app.asar` and Fusion's shipped Python type stubs to learn the
>   two APIs instead of trusting its memory of them;
> - wrote the Fusion add-in, the MCP server, and three test harnesses;
> - installed the add-in into Fusion, restarted DSH, and then called its own tools
>   back through the MCP layer;
> - drove **15 modelling commands** against real Fusion and checked each result
>   against its closed-form volume, rather than settling for "the call did not
>   raise" — see [the measured table](docs/verification.md);
> - hit **16 places where its recall of the Fusion API disagreed with reality**,
>   and corrected every one from the actual error text. Those are written up in
>   [Fusion API notes](docs/fusion-api-notes.md).
>
> The mistakes are documented rather than hidden, because they are the useful
> part: most of them fail **silently** in Fusion's API — a chamfer that produces
> a healthy feature and changes nothing, a sketch that quietly absorbs a circle
> as a hole. If you are writing Fusion API code yourself, that page will save you
> a day.
>
> The prose in this README is the agent's too. Every number in it is a
> measurement the agent took on this machine, not a claim copied from a doc.

---

## What it is

Fusion 360 has **no headless mode**. Any modelling operation needs a running
Fusion instance, and the Fusion API is single-threaded — it may only be called
from Fusion's primary thread. So a plain MCP server that shells out to Python
cannot work. This is a two-part bridge:

```
┌─────────────┐   MCP over stdio    ┌──────────────────┐   JSON over     ┌────────────────────┐
│     DSH     │ ──────────────────► │  MCP server      │ ── loopback ──► │  Fusion 360        │
│ (dsh-mcp-   │   newline JSON      │  (this repo,     │   TCP :27182    │  add-in            │
│  client)    │ ◄────────────────── │   stdlib only)   │ ◄─────────────  │  (primary thread)  │
└─────────────┘                     └──────────────────┘                 └────────────────────┘
```

The MCP server runs as an ordinary DSH-spawned subprocess. The add-in runs
*inside* Fusion, joins the socket requests back onto Fusion's primary thread with
a `CustomEvent`, and executes them there.

## Quick start

```powershell
# 1. Install the add-in into Fusion's own AddIns folder (enables runOnStartup)
powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1

# 2. Register the MCP server with DSH - fills in every path for you
python tools\install_dsh_mcp.py

# 3. Restart DSH - a new profile row is NOT hot-loaded
python tools\restart_dsh.py

# 4. Start Fusion 360, then verify
```

Ask DSH to call `mcp__fusion360__fusion_status`. A healthy reply:

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

Full walkthrough, including the manual add-in path and troubleshooting:
**[docs/install.md](docs/install.md)**.

### Prove it works

[`examples/build_a_part.py`](examples/build_a_part.py) builds a 100 × 60 × 8 mm
plate with four R6 corner rounds and four ⌀4.5 through holes, and checks its own
volume at every stage — 48000 → 47752.78 → **47243.84 mm³**. Run it as the `code`
argument of `mcp__fusion360__fusion_run_python` and it either matches or raises.

It is the fastest way to tell a working install from a broken one, and it is
worth reading as an example of the API's sharp edges being handled: profile
selection by bounding box, the sketch-frame conversion, and the chamfer's
`isTangentChain` trap are all commented in place.

## Tools

Thirteen tools, exposed to DSH as `mcp__fusion360__<name>`.

| Tool | Purpose |
|---|---|
| `fusion_status` | Reachability, Fusion version, signed-in user |
| `fusion_document_info` | Active design: units, body/parameter counts, bounding box |
| `fusion_list_bodies` | Every body with volume (mm³), area (mm²), bbox, material |
| `fusion_list_parameters` | All model and user parameters |
| `fusion_set_parameter` | Set an expression and recompute — the parametric path |
| `fusion_create_document` | New empty design |
| `fusion_list_documents` | Open documents |
| `fusion_open_document` | Open a cloud data file by name |
| `fusion_save` | Save the active document |
| `fusion_export` | Export to STEP/IGES/STL/OBJ/F3D/SMT/SAT |
| `fusion_text_command` | Raw Fusion text command |
| `fusion_reload_bridge` | Re-execute the add-in from disk and restart its listener |
| `fusion_run_python` | **Arbitrary Python on Fusion's primary thread** |

`fusion_run_python` is the escape hatch that makes the set complete: anything
Fusion's API can do, you can do. `adsk`, `app`, `ui`, `design` and `root` come
pre-bound, `stdout` is captured, and assigning to `result` returns a value.

```python
sketch = root.sketches.add(root.xYConstructionPlane)
sketch.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), 2.0)   # centimetres
extrude = root.features.extrudeFeatures
inp = extrude.createInput(
    sketch.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
inp.setDistanceExtent(False, adsk.core.ValueInput.createByReal(1.0))
extrude.add(inp)
result = {'bodies': root.bRepBodies.count}
```

> **Units:** the Fusion API works in **centimetres and radians** regardless of the
> document's display units. The read-only tools convert to millimetres for you;
> code passed to `fusion_run_python` does not get that treatment.

## What actually works

Every command below was driven through `fusion_run_python` against a live Fusion
**2705.1.15** and checked against the closed-form volume. These are measurements,
not smoke tests.

| Command | Measured | Analytic |
|---|---|---|
| Sketch: rectangle, circle, arc, fitted spline | 8 curves, 3 closed profiles | — |
| Extrude, new body 60 × 40 × 10 | 24000.00 mm³ | 24000 |
| Extrude, join — boss ⌀16 × 16 | +3216.99 mm³ | +3216.99 |
| Extrude, cut through-all — ⌀10 | −785.40 mm³ | −785.40 |
| Fillet, 4 vertical edges R3 | −77.26 mm³ | −77.26 |
| Chamfer, 4 top edges 1 mm | −98.67 mm³, 6 → 10 faces | −100 |
| Loft, ⌀40 → ⌀16 over 30 mm | 19603.54 mm³ | 19603.54 |
| Revolve, rectangle about Z, 360° | 42411.50 mm³ | 42411.50 |
| Sweep, ⌀10 circle along a 50 mm line | 3926.99 mm³ | 3926.99 |
| Shell, top face open, 2 mm wall | 7872.00 mm³ | 7872 |
| Hole ⌀4.5 through 8 mm | −127.23 mm³ | −127.23 |
| Rectangular pattern 2 × 2 | 47491.06 mm³, 4 holes | 47491.06 |
| Full part: plate + R6 + 4 × ⌀4.5 | 48000.00 → 47752.78 → 47243.84 | same |
| Sketch on a solid face + blind cut | 23547.61 mm³ | 23547.61 |

The chamfer's shortfall against a naive `area × length` figure is the corner
treatment where four chamfers meet — it reproduces on a plain box (98.667 vs
100 mm³), so it is geometry, not error.

Full tables, including the MCP-layer round trip and the STL/STEP export checks:
**[docs/verification.md](docs/verification.md)**.

## Read this before writing Fusion API code

Fusion renames and removes API members between releases, and the failures are
**silent**. Four that cost real time here:

- **`Sketches.add(face)` silently injects the face outline** as ordinary sketch
  lines. `profiles.item(0)` on a sketch made on a plate's top face is *the plate*,
  not the circle you just drew. Select profiles by `boundingBox`, never by index.
- **A closed curve inside another becomes a hole in it.** Draw a circle inside a
  rectangle in one sketch and the rectangle's profile turns into an annulus. Give
  pockets their own sketch.
- **A chamfer after a fillet needs `isTangentChain=True`.** With `False` the
  feature is created, `healthState` is healthy, `errorOrWarningMessage` is empty,
  and *nothing changes*.
- **`Face.geometry.normal` does not tell top from bottom.** Both planar faces of
  a plate can report `+Z` — it describes the underlying surface, not the face.

All sixteen are in **[docs/fusion-api-notes.md](docs/fusion-api-notes.md)**, each
with the error text it produces and the fix.

## Layout

| Path | Role |
|---|---|
| `fusion_addin/` | The add-in that runs **inside** Fusion 360 |
| `mcp_server/fusion_mcp_server.py` | MCP server DSH spawns; standard library only |
| `dsh/cordis.patch.yml` | The profile patch entry that registers it with DSH |
| `docs/` | Install guide, measurements, Fusion API notes |
| `examples/` | A commented part build you can run as-is |
| `tools/` | Tests, probes, installer, DSH restart helper |

## Development

```powershell
# Full MCP exchange against the fake bridge - no Fusion, no subprocesses
python tools\smoke_test.py

# Add-in worker -> primary thread hand-off, against a stubbed adsk package
python tools\addin_stub_test.py

# Against a live Fusion: builds real geometry, reads it back, exports it
python tools\verify_e2e.py

# Protocol handshake only
python mcp_server\fusion_mcp_server.py --selftest

# Talk to the bridge directly (bypasses DSH entirely)
python tools\probe_bridge.py --cmd ping

# Re-install the add-in after editing it
powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1
```

`--args-file` exists on `probe_bridge.py` because both PowerShell and cmd mangle
inline JSON quotes when passing arguments to a native executable.

### Reloading the add-in without Fusion's UI

A code change normally needs a human to click **Stop** then **Run** in the Scripts
and Add-Ins dialog. `fusion_reload_bridge` (or `probe_bridge.py --cmd reload`)
re-executes the add-in from disk and rebinds its listener instead, so iteration
needs no GUI at all. The listener drops for about a second; poll until `ping`
reports the new `bridgeVersion`.

If a self-reload fails the listener is gone and only the GUI can bring it back,
so keep the manual path as the recovery route.

## Configuration

**Port** — edit `fusion_addin/bridge_config.json` and the matching
`FUSION_DSH_BRIDGE_PORT` in `dsh/cordis.patch.yml`. Default `27182`.

**Security** — the endpoint binds `127.0.0.1` only, so it is not reachable from
the network. It is still an **unauthenticated local RPC that can execute Python
inside Fusion**; treat access to this machine's loopback as the trust boundary.
Do not expose the port.

**Log** — the add-in appends to `fusion_addin/bridge.log` and also writes to
Fusion's own log via `app.log()`. Check it first when the add-in will not start.

## Design notes

- **No third-party dependencies.** The MCP server implements the protocol
  directly, so no `pip install` is needed and the interpreter can be any
  Python 3.8+.
- **Tool names are stable.** DSH renders them as `mcp__fusion360__<tool>`, so
  renaming a tool would break session history — treat the names as frozen.
- **`failOnStartupError: false`** keeps DSH booting when Fusion is closed. The
  tools appear regardless and report the bridge as unreachable.

## License

MIT — see [LICENSE](LICENSE).

Fusion 360 and Autodesk are trademarks of Autodesk, Inc. This project is not
affiliated with or endorsed by Autodesk.

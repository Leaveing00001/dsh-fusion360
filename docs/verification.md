# Verification

Everything on this page was run on this machine against a live **Fusion 360
2705.1.15** on Windows. The point of the tables is that they compare against
**closed-form values**, not against "the call returned without raising".

Three independent layers are exercised, because each can fail on its own:

| Layer | Script | Needs Fusion? | Checks |
|---|---|---|---|
| MCP protocol | `tools/smoke_test.py` | no | 16/16 |
| Add-in worker → primary thread | `tools/addin_stub_test.py` | no | 22/22 |
| The real thing | `tools/verify_e2e.py` | yes | 12/12 |

---

## End-to-end, over a raw socket

`tools/verify_e2e.py` — builds real geometry, reads it back, exports it, cleans up.

| Stage | Result |
|---|---|
| Bridge reachable, reports the right build | pass |
| `create_document` | pass |
| `run_python`: sketch + extrude | one body |
| Volume of a 60 × 40 × 10 mm plate | **24000 mm³ exactly** |
| `document_info`, `list_bodies`, bounding box | 60 × 40 × 10 mm |
| STEP export | valid `ISO-10303-21` |
| STL export | binary, 12 triangles, 684 bytes |
| Cleanup | Fusion left with no documents open |

## End-to-end, through the DSH MCP layer

The same path re-run through the real `mcp__fusion360__*` tools inside DSH, which
additionally proves the MCP transport, the tool schemas, and typed argument
marshalling:

| Stage | Result |
|---|---|
| `fusion_status` | v1.0.3 / Fusion 2705.1.15 |
| `fusion_create_document` + `fusion_list_documents` | one untitled document |
| `fusion_run_python`: sketch + extrude | body `实体1`, 1 profile |
| Volume of a 60 × 40 × 10 mm plate | **24000 mm³ exactly** |
| `fusion_list_bodies` | 1 solid, 24000 mm³, area 6800 mm², bbox 60 × 40 × 10 |
| `fusion_document_info` | units mm, 1 body, 1 component |
| `fusion_list_parameters` | `d1 = 10.00 mm`, `d2 = 0.0 deg` |
| `fusion_set_parameter` `d1 = 25 mm` | recomputed to 60000 mm³, bbox 60 × 40 × 25 |
| Cleanup | all documents closed, 0 remaining |

The parameter test is the meaningful one: changing `d1` and watching the volume go
24000 → 60000 mm³ proves the round trip carries **typed values both ways**, not
just strings.

## Modelling commands, measured

Each row was driven through `fusion_run_python` and checked against the
closed-form volume.

| Command | API entry point | Measured | Analytic |
|---|---|---|---|
| Sketch: rectangle, circle, arc, fitted spline | `sketchCurves.sketchLines` / `sketchCircles` / `sketchArcs` / `sketchFittedSplines` | 8 curves, 3 closed profiles | — |
| Extrude, new body 60 × 40 × 10 | `extrudeFeatures` + `NewBodyFeatureOperation` | 24000.00 mm³ | 24000 |
| Extrude, join — boss ⌀16 × 16 | `JoinFeatureOperation` | +3216.99 mm³ | +3216.99 |
| Extrude, cut through-all — ⌀10 | `CutFeatureOperation` + `setAllExtent` | −785.40 mm³ | −785.40 |
| Fillet, 4 vertical edges R3 | `filletFeatures` + `addConstantRadiusEdgeSet` | −77.26 mm³ | −77.26 |
| Chamfer, 4 top edges 1 mm | `chamferFeatures` + `setToEqualDistance` | −98.67 mm³, 6 → 10 faces | −100 |
| Loft, ⌀40 → ⌀16 over 30 mm | `loftFeatures` + offset construction plane | 19603.54 mm³ | 19603.54 |
| Revolve, rectangle about Z, 360° | `revolveFeatures.createInput(profile, axis, op)` | 42411.50 mm³ | 42411.50 |
| Sweep, ⌀10 circle along a 50 mm line | `Path.create` + `sweepFeatures` | 3926.99 mm³ | 3926.99 |
| Shell, top face open, 2 mm wall | `shellFeatures` + `insideThickness` | 7872.00 mm³ | 7872 |
| Hole ⌀4.5 through 8 mm | `holeFeatures.createSimpleInput` | −127.23 mm³ | −127.23 |
| Rectangular pattern 2 × 2 | `rectangularPatternFeatures` | 47491.06 mm³, 4 holes | 47491.06 |
| Full part: plate + R6 + 4 × ⌀4.5 | extrude → fillet → cut | 48000.00 → 47752.78 → 47243.84 mm³ | same |
| Sketch on a solid face + blind cut | `sketches.add(face)` + `modelToSketchSpace` | 23547.61 mm³ | 23547.61 |

### The one row that does not match

The chamfer comes out at **−98.667 mm³** where a naive `perimeter × leg / 2 ×
length` figure says −100.

That figure is wrong, not Fusion. It ignores how the four chamfers meet at the
corners. The discrepancy reproduces exactly on a plain box with no fillets and no
other features, which rules out interaction with earlier features — so it is
geometry, and the analytic value was the thing at fault.

Worth stating plainly because it is the one place in this document where the
"measured vs analytic" framing needed a human judgement rather than agreement.

---

## The runnable example

`examples/build_a_part.py` builds the "full part" row above and checks itself at
every stage, so it fails loudly rather than producing a wrong solid. It is the
fastest way to confirm a new install actually works end to end:

| Stage | Volume | Expected |
|---|---|---|
| 100 × 60 × 8 plate | 48000.00 mm³ | 48000.00 |
| after four R6 corner rounds | 47752.78 mm³ | 47752.78 |
| after four ⌀4.5 through holes | 47243.84 mm³ | 47243.84 |

Confirmed twice over: once by the script's own assertions, and once independently
through `fusion_list_bodies`, which reported **47243.8406743523 mm³** on a
100 × 60 × 8 bounding box.

Send it to Fusion with:

```python
_p = r'<repo>\examples\build_a_part.py'
exec(compile(open(_p, encoding='utf-8').read(), _p, 'exec'), globals())
```

as the `code` argument of `mcp__fusion360__fusion_run_python`.

---

## Reproducing

```powershell
python tools\smoke_test.py        # 16/16, no Fusion needed
python tools\addin_stub_test.py   # 22/22, no Fusion needed
python tools\verify_e2e.py        # 12/12, needs Fusion running with the add-in

python mcp_server\fusion_mcp_server.py --selftest   # protocol handshake only
python tools\probe_bridge.py --cmd ping             # bridge only
```

The two offline suites are the ones to run in CI — they need no Fusion licence and
no GUI session.

## What is *not* verified

Stated so the tables above are not read as broader than they are:

- **Windows only.** The manifest declares `windows|mac`, and the add-in code is
  portable, but every measurement here is from one Windows machine.
- **One Fusion version.** 2705.1.15. Fusion removes and renames API members
  between releases; see [Fusion API notes](fusion-api-notes.md).
- **No CAM, no drawings, no simulation.** The bridge can reach them —
  `fusion_run_python` reaches everything — but nothing there has been measured.
- **No concurrent clients.** One DSH session was driving one Fusion instance.

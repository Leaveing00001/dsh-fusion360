# -*- coding: utf-8 -*-
"""Build a real part, and check it against the closed-form volume.

Paste this into DSH as a call to `mcp__fusion360__fusion_run_python`, or send it
over the bridge directly:

    python tools/probe_bridge.py --cmd run_python --args-file example_args.json

Part: a 100 x 60 x 8 mm plate, four R6 corner rounds, four ⌀4.5 through holes.

Expected, and measured on Fusion 2705.1.15:

    plate                     48000.00 mm^3
    after four R6 rounds      47752.78 mm^3     (-247.22)
    after four ⌀4.5 holes     47243.84 mm^3     (-508.94)

`fusion_run_python` pre-binds `adsk`, `app`, `ui`, `design` and `root`; the
imports below are there so the file also runs as a plain script inside Fusion's
own Python console.

    UNITS: the Fusion API is centimetres and radians. Every length below is
    converted with MM() on the way in. Nothing else in this file is in mm.
"""

import adsk.core
import adsk.fusion

# --------------------------------------------------------------------------
# parameters (millimetres)
# --------------------------------------------------------------------------

LENGTH = 100.0          # plate X
WIDTH = 60.0            # plate Y
HEIGHT = 8.0            # plate Z

CORNER_RADIUS = 6.0     # fillet on the four vertical edges

HOLE_DIAMETER = 4.5
HOLE_INSET = 12.0       # hole centre distance from each edge

EXPECTED = {
    'plate': 48000.00,
    'rounded': 47752.78,
    'drilled': 47243.84,
}


def MM(value):
    """Millimetres -> the centimetres the Fusion API wants."""
    return value / 10.0


def volume_mm3(body):
    return body.volume * 1000.0          # cm^3 -> mm^3


# --------------------------------------------------------------------------
# helpers
#
# These exist because of two Fusion behaviours documented in
# docs/fusion-api-notes.md: profiles must be chosen by boundingBox (never by
# index), and the top face must be chosen by height (both planar faces of a
# plate can report the same normal).
# --------------------------------------------------------------------------

def find_profile(sketch, width_mm, height_mm, tol=0.05):
    """The profile whose bounding box is width x height mm."""
    for i in range(sketch.profiles.count):
        profile = sketch.profiles.item(i)
        box = profile.boundingBox
        w = (box.maxPoint.x - box.minPoint.x) * 10.0
        h = (box.maxPoint.y - box.minPoint.y) * 10.0
        if abs(w - width_mm) < tol and abs(h - height_mm) < tol:
            return profile
    raise RuntimeError('no %.1f x %.1f mm profile' % (width_mm, height_mm))


def top_face(body):
    """The face with the highest minPoint.z - never select this by normal."""
    faces = [body.faces.item(i) for i in range(body.faces.count)]
    return max(faces, key=lambda f: f.boundingBox.minPoint.z)


def vertical_edges(body, tol=1e-6):
    """Edges whose two ends differ in Z."""
    found = []
    for i in range(body.edges.count):
        edge = body.edges.item(i)
        a, b = edge.startVertex.geometry, edge.endVertex.geometry
        if abs(a.z - b.z) > tol:
            found.append(edge)
    return found


def assert_volume(body, key):
    got = volume_mm3(body)
    want = EXPECTED[key]
    ok = abs(got - want) < 0.05
    print('%-10s %12.2f mm^3   expected %12.2f   %s'
          % (key, got, want, 'ok' if ok else 'MISMATCH'))
    if not ok:
        raise RuntimeError('%s: %.2f != %.2f' % (key, got, want))
    return got


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build():
    if design is None:
        raise RuntimeError('no active design - open or create a document first')

    root = design.rootComponent

    # --- 1. plate: sketch a rectangle on XY, extrude it -------------------
    sketch = root.sketches.add(root.xYConstructionPlane)
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0.0, 0.0, 0.0),
        adsk.core.Point3D.create(MM(LENGTH), MM(WIDTH), 0.0))

    # By boundingBox, not profiles.item(0): a sketch on a face arrives with the
    # face outline already in it, so index 0 is not necessarily yours.
    profile = find_profile(sketch, LENGTH, WIDTH)

    extrudes = root.features.extrudeFeatures
    extrude_input = extrudes.createInput(
        profile, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    extrude_input.setDistanceExtent(
        False, adsk.core.ValueInput.createByReal(MM(HEIGHT)))
    extrude = extrudes.add(extrude_input)

    body = extrude.bodies.item(0)
    body.name = 'plate'
    assert_volume(body, 'plate')

    # --- 2. four corner rounds -------------------------------------------
    edges = vertical_edges(body)
    if len(edges) != 4:
        raise RuntimeError('expected 4 vertical edges, found %d' % len(edges))

    collection = adsk.core.ObjectCollection.create()
    for edge in edges:
        collection.add(edge)

    fillets = root.features.filletFeatures
    fillet_input = fillets.createInput()
    # addConstantRadiusEdgeSet(edges, radius, isTangentChain). The third
    # argument matters: with False on an already-filleted body, Fusion reports
    # a healthy feature that changes nothing. See notes 7 in
    # docs/fusion-api-notes.md.
    fillet_input.addConstantRadiusEdgeSet(
        collection,
        adsk.core.ValueInput.createByReal(MM(CORNER_RADIUS)),
        True)
    fillets.add(fillet_input)
    assert_volume(body, 'rounded')

    # --- 3. four ⌀4.5 through holes ---------------------------------------
    # Holes are positioned with a SKETCH point, not a model point:
    # setPositionByPoint(face, modelPoint) fails with
    # "InternalValidationError : logicalSelection".
    face = top_face(body)
    hole_sketch = root.sketches.add(face)

    centres_mm = [
        (HOLE_INSET, HOLE_INSET),
        (LENGTH - HOLE_INSET, HOLE_INSET),
        (LENGTH - HOLE_INSET, WIDTH - HOLE_INSET),
        (HOLE_INSET, WIDTH - HOLE_INSET),
    ]

    holes = root.features.holeFeatures
    for x_mm, y_mm in centres_mm:
        # A sketch on a face has its own frame - convert, never assume.
        model_point = adsk.core.Point3D.create(MM(x_mm), MM(y_mm), MM(HEIGHT))
        local = hole_sketch.modelToSketchSpace(model_point)
        sketch_point = hole_sketch.sketchPoints.add(local)

        # createSimpleInput takes the DIAMETER, not the position. The position
        # is a separate call, and it must be the sketch point:
        # setPositionByPoint(face, modelPoint) fails with
        # "InternalValidationError : logicalSelection".
        hole_input = holes.createSimpleInput(
            adsk.core.ValueInput.createByReal(MM(HOLE_DIAMETER)))
        hole_input.setPositionBySketchPoint(sketch_point)
        # Through-all. The hole's natural direction is OPPOSITE the sketch
        # normal (per the API docs), and this sketch sits on the top face, so
        # the natural direction already points down into the material.
        hole_input.setAllExtent(
            adsk.fusion.ExtentDirections.PositiveExtentDirection)
        # A plain Python list, not an ObjectCollection.
        hole_input.participantBodies = [body]
        holes.add(hole_input)

    assert_volume(body, 'drilled')

    return {
        'body': body.name,
        'volume_mm3': round(volume_mm3(body), 2),
        'faces': body.faces.count,
        'edges': body.edges.count,
        'bbox_mm': [
            round((body.boundingBox.maxPoint.x - body.boundingBox.minPoint.x) * 10.0, 2),
            round((body.boundingBox.maxPoint.y - body.boundingBox.minPoint.y) * 10.0, 2),
            round((body.boundingBox.maxPoint.z - body.boundingBox.minPoint.z) * 10.0, 2),
        ],
        'holes': len(centres_mm),
    }


result = build()
print(result)

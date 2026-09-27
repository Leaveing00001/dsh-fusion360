# Fusion API notes

Sixteen things that cost real time while building this bridge.

They are collected because of a shared property: **most of them fail silently.**
Fusion's API will happily create a feature that does nothing, or hand you a
profile you did not ask for, without raising. The only defence that worked here
was to check every signature against `__doc__` at runtime and to verify results by
measuring geometry, rather than trusting either recall or a clean return value.

The authoritative reference for *your* installed version is the stub package
Fusion ships:

```
%APPDATA%\Autodesk\Autodesk Fusion 360\API\Python\defs\adsk\
```

Grep that before assuming a member exists. Every entry below is a case where
recall and that directory disagreed.

---

## Units

**The API uses centimetres and radians**, regardless of the document's display
units. A 60 mm plate is `6.0`. This is not a gotcha that produces an error — it
produces a plate 10× too big. Read-only tools in this repo convert to millimetres;
`fusion_run_python` does not.

---

## Sketches and profiles

### 1. `Profile` has no `area`

```
AttributeError: 'Profile' object has no attribute 'area'
```

Use `profile.boundingBox`. Selecting a profile by area is the natural thing to
want, and the natural thing is not available.

### 2. `Line3D` has no `direction`

```
AttributeError: 'Line3D' object has no attribute 'direction'
```

Derive it from the delta between `startPoint` and `endPoint`:

```python
dx = line.endPoint.x - line.startPoint.x
dy = line.endPoint.y - line.startPoint.y
is_vertical = abs(dy) > abs(dx)
```

### 3. `Sketches.add(face)` silently injects the face outline

The single most expensive trap here. Adding a sketch on the top face of a
60 × 40 mm plate gives you, **before you draw anything**:

```
sketchCurves.count == 4
profiles.count     == 1        # the 60 x 40 plate itself
```

Those four lines are the face outline, added as ordinary sketch geometry. So
`profiles.item(0)` is the *plate*, not the circle you just drew — and a cut
against it removes the whole face instead of your hole. Measured damage: a cut
that should have been 452 mm³ removed 9600 mm³ instead.

**Select profiles by `boundingBox`, never by index:**

```python
def find_profile(sketch, w_mm, h_mm, tol=0.01):
    for i in range(sketch.profiles.count):
        p = sketch.profiles.item(i)
        bb = p.boundingBox
        w = (bb.maxPoint.x - bb.minPoint.x) * 10.0
        h = (bb.maxPoint.y - bb.minPoint.y) * 10.0
        if abs(w - w_mm) < tol and abs(h - h_mm) < tol:
            return p
    raise RuntimeError('no profile %.1fx%.1f' % (w_mm, h_mm))
```

### 4. A closed curve inside another becomes a hole in it

Within a single sketch, a circle drawn inside a rectangle turns the rectangle's
profile into an **annulus**. The extrude then yields a plate with a hole where a
solid plate was intended — and a later cut at that position fails with:

```
RuntimeError: 3 : 未找到要剪切或相交的目标实体！
```

Measured: the extrude gave 21204 mm³ where 24000 was expected. The difference is
exactly `π(8² + 5²) × 10 = 2796.02` mm³ — the two circles, to the digit.

**Give pockets their own sketch.** The nesting is a legitimate Fusion behaviour,
but it is applied without asking and it changes the meaning of geometry you have
already built.

### 5. `Face.geometry.normal` does not tell top from bottom

Both planar faces of a plate can report `+Z`, because the normal describes the
**underlying surface**, not the face's orientation in the body.

Select by height instead:

```python
def top_face(body):
    faces = [body.faces.item(i) for i in range(body.faces.count)]
    return max(faces, key=lambda f: f.boundingBox.minPoint.z)
```

### 6. `Sketches.add(face)` also flips axes

The sketch created on a bottom face came back with a transform of
`diag(-1, 1, -1)` and zero translation. A model point of `(3, 2, 1)` converted to
sketch space as `(-3, 2, -1)`.

Never assume a shared frame. Convert:

```python
local = sketch.modelToSketchSpace(adsk.core.Point3D.create(x, y, z))
```

`sketchToModelSpace` is the inverse, and `sketch.transform` shows the frame.

---

## Features

### 7. A chamfer after a fillet needs `isTangentChain=True`

The worst failure mode in this list, because nothing at all signals it:

- the feature is created,
- `healthState` is healthy,
- `errorOrWarningMessage` is **empty**,
- and the volume and face count are **unchanged**.

```python
chamfer_input.isTangentChain = True   # False silently does nothing here
```

Same edges with `True`: −96.378 mm³ and 10 → 18 faces.

If you take one thing from this page: a healthy feature is not evidence that
anything happened. Measure.

### 8. Sweep needs a `Path`, and the enum is `ChainedCurveOptions`

Passing a raw sketch line raises a **SWIG** `TypeError`, not a Fusion error, so the
message talks about C++ types:

```
TypeError: SweepFeatures_createInput, argument 3 of type
  'adsk::core::Ptr<adsk::fusion::Path> const &'
```

Wrap it:

```python
path = adsk.fusion.Path.create(
    curve, adsk.fusion.ChainedCurveOptions.connectedChainedCurves)
```

`adsk.fusion.ChainOptions` does not exist — the name is `ChainedCurveOptions`. The
other members are `noChainedCurves`, `openEdgesChainedCurves`,
`tangentAndOpenEdgesChainedCurves`, `tangentChainedCurves`.

### 9. `createSimpleInput` takes the diameter, not the position

The obvious reading of "simple input" is that you hand it where the hole goes.
You do not:

```python
holes.createSimpleInput(model_point)
# TypeError: in method 'HoleFeatures_createSimpleInput', argument 2 of type
#   'adsk::core::Ptr< adsk::core::ValueInput > const &'
```

Argument 2 is the **diameter**:

```python
hole_input = holes.createSimpleInput(
    adsk.core.ValueInput.createByReal(diameter_cm))
hole_input.setPositionBySketchPoint(sketch_point)     # position, separately
hole_input.setAllExtent(
    adsk.fusion.ExtentDirections.PositiveExtentDirection)   # through-all
hole_input.participantBodies = [body]                 # a plain list
holes.add(hole_input)
```

Both extent setters take **one** argument — `setDistanceExtent(distance)` and
`setAllExtent(direction)`. A two-argument `setDistanceExtent` was tried here on
the assumption it mirrored `ExtrudeFeatureInput`; it does not, and the error is a
plain `TypeError: takes 2 positional arguments but 3 were given`.

**Direction is inverted relative to the sketch.** From the docs:

> The natural direction will be **opposite** the normal of the sketch.

So a hole sketched on a top face (normal `+Z`) already points **down** into the
material, and `PositiveExtentDirection` means "along that natural direction" —
which is what makes through-all work without flipping anything.

Positioning by **sketch point** is not a stylistic choice.
`setPositionByPoint(planarEntity, point)` fails with:

```
RuntimeError: 2 : InternalValidationError : logicalSelection
```

Its documented signature — a planar entity *and* a point — is exactly what was
passed, so this looks like a genuine defect rather than a misuse. It is also the
more tempting call, because it takes a model-space point and so appears to avoid
the sketch-frame conversion in note 6.

So the hole needs a sketch to live on. Adding one to a face brings its own frame,
so convert model coordinates with `sketch.modelToSketchSpace` first (note 6), and
remember that `Sketches.add(face)` has already seeded it with the face outline
(note 3) — which is harmless here, because the holes are positioned by explicit
sketch points rather than by profile index.

### 10. `participantBodies` wants a plain Python list

An `ObjectCollection` raises a SWIG `TypeError`; the parameter is marshalled as
`std::vector<Ptr<BRepBody>>`:

```python
hole_input.participantBodies = [body]        # not an ObjectCollection
```

### 11. `RectangularPatternFeatures.createInput` takes five arguments

```python
createInput(inputEntities, directionOneEntity, quantityOne,
            distanceOne, patternDistanceType)
```

The second direction is **not** an argument. Set it on the returned input:

```python
inp.quantityTwo = 2
inp.distanceTwo = adsk.core.ValueInput.createByReal(4.0)
inp.directionTwoEntity = edge
```

with `patternDistanceType =
adsk.fusion.PatternDistanceType.SpacingPatternDistanceType`.

A plausible-looking six-argument call fails with `takes 6 positional arguments but
7 were given` — the count includes `self` if you reach the method off the class
rather than off the collection. Use the bound method.

### 12. `ChamferFeatureInput.chamferEdgeSets` exists; `ChamferFeature`'s does not

Asymmetric, and the error only appears when you try to read back what you set:

- **write** — `ChamferFeatureInput.chamferEdgeSets`
- **read** — `ChamferFeature.edgeSets`

`ChamferFeature.chamferEdgeSets` raises `AttributeError`.

### 13. `ExtrudeFeatureInput.setOneSideExtent` signature

```python
extent = adsk.fusion.DistanceExtentDefinition.create(
    adsk.core.ValueInput.createByReal(distance_cm))
extrude_input.setOneSideExtent(
    extent, adsk.fusion.ExtentDirections.PositiveExtentDirection)
```

---

## Members Fusion removed

These were present in older releases and are gone in 2705. The removals are silent
at author time and only surface as an `AttributeError` from inside Fusion.

| Removed | Replacement used here |
|---|---|
| `Component.bodies` | `Component.bRepBodies`, read via `count`/`item` |
| `Parameter.isUserParameter` | `Parameter.objectType` ends with `UserParameter` |

---

## Add-in plumbing

### 14. `fireCustomEvent`'s return value is not trustworthy

```python
app.fireCustomEvent(EVENT_ID, payload)   # returns False...
```

...while the event is delivered **normally**. Never gate on the boolean. Wait for
the primary thread to drain the queue instead.

This matters specifically for the pattern this repo uses: a socket worker thread
cannot call the Fusion API, so it enqueues work, fires a custom event, and waits on
a `queue.Queue`. A `False` return is not a failure and must not be treated as one.

### 15. The `Application` proxy must be obtained inside the worker thread

One captured on the primary thread does not marshal across. In the add-in:

```python
def _log(message):
    try:
        app = adsk.core.Application.get()   # inside the calling thread
        ...
```

The same applies to `adsk.core.Application.get()` inside any thread that needs it
— cache it per thread, not globally.

### 16. Fusion's event plumbing holds weak references

The event object **and** every handler must stay referenced from Python, or the
callback is collected and the bridge goes deaf with no error:

```python
_HANDLERS = []   # module level, deliberately never cleared
```

Because Fusion performs repeated `run()`/`stop()` cycles on a loaded add-in,
bridge state belongs at module level, not inside the add-in class.

---

## A note on method

Every signature used in this repo was confirmed against `__doc__` at runtime rather
than recalled. That is not a stylistic preference — it is the only reason entries
7 and 3 were caught at all, since neither produces a usable error.

The companion habit is to **measure geometry** after every operation. Most of the
entries above were found by a volume not matching its closed form, with the error
text arriving later or not at all.

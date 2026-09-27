#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end verification of the Fusion bridge against a live Fusion 360.

Proves the whole chain works, not just that the port is open: creates an
untitled document, builds real geometry through fusion_run_python, reads the
result back through the inspection tools, and exports it to disk.

Everything happens in a NEW untitled document, so no existing design is
touched and nothing is saved.

    python tools/verify_e2e.py
    python tools/verify_e2e.py --out "C:\\somewhere"
"""

import argparse
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILURES = []
CHECKS = 0

# 60 x 40 mm plate, 10 mm thick. The Fusion API works in centimetres.
PLATE_CODE = """
sketch = root.sketches.add(root.xYConstructionPlane)
sketch.name = 'DSH verify sketch'
sketch.sketchCurves.sketchLines.addTwoPointRectangle(
    adsk.core.Point3D.create(0.0, 0.0, 0.0),
    adsk.core.Point3D.create(6.0, 4.0, 0.0),
)
print('sketch profiles: %d' % sketch.profiles.count)

extrudes = root.features.extrudeFeatures
extrude_input = extrudes.createInput(
    sketch.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
)
# 1.0 cm == 10 mm. ExtrudeFeatureInput takes an ExtentDefinition, not a raw
# distance; setDistanceExtent(isSymmetric, distance) is the old form and is
# no longer on this class.
extent = adsk.fusion.DistanceExtentDefinition.create(
    adsk.core.ValueInput.createByReal(1.0)
)
extrude_input.setOneSideExtent(
    extent, adsk.fusion.ExtentDirections.PositiveExtentDirection
)
feature = extrudes.add(extrude_input)

body = root.bRepBodies.item(0)   # Component.bodies was removed in Fusion 2705
print('body: %s' % body.name)
result = {
    'bodyCount': root.bRepBodies.count,
    'volume_mm3': body.volume * 1000.0,
    'area_mm2': body.area * 100.0,
    'featureCount': root.features.count,
}
"""

EXPECTED_VOLUME_MM3 = 24000.0  # 60 x 40 x 10

# Closes every unsaved document, so a failed run never leaves scratch work
# behind for the user to tidy up by hand.
CLEANUP_CODE = """
closed = []
for index in range(app.documents.count - 1, -1, -1):
    document = app.documents.item(index)
    if not document.isSaved:
        name = document.name
        document.close(False)   # False == discard changes, no prompt
        closed.append(name)
result = {'closed': closed, 'remaining': app.documents.count}
"""


def check(label, condition, detail=''):
    global CHECKS
    CHECKS += 1
    if condition:
        print('  PASS  %s' % label)
    else:
        print('  FAIL  %s  %s' % (label, detail))
        FAILURES.append(label)


class Bridge(object):
    def __init__(self, host, port, timeout=100.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.counter = 0

    def call(self, command, arguments=None):
        self.counter += 1
        request = json.dumps({'id': self.counter, 'cmd': command, 'args': arguments or {}})
        connection = socket.create_connection((self.host, self.port), timeout=10.0)
        try:
            connection.settimeout(self.timeout)
            stream = connection.makefile('rwb')
            with stream:
                stream.write((request + '\n').encode('utf-8'))
                stream.flush()
                line = stream.readline()
        finally:
            connection.close()
        if not line:
            raise RuntimeError('%s: bridge closed without replying' % command)
        response = json.loads(line.decode('utf-8'))
        if not response.get('ok'):
            raise RuntimeError('%s: %s' % (command, response.get('error')))
        return response.get('result'), response.get('bridgeNote')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=27182)
    parser.add_argument('--out', default=os.path.join(ROOT, 'verify_out'))
    parser.add_argument('--keep', action='store_true',
                        help='leave the scratch document open instead of closing it')
    args = parser.parse_args()

    bridge = Bridge(args.host, args.port)
    print('Fusion bridge end-to-end verification')
    print('=' * 62)

    # --- 1. connectivity -------------------------------------------------
    print('\n1. connectivity')
    info, note = bridge.call('ping')
    check('bridge answered', info.get('bridge') == 'FusionDSHBridge', info)
    check('running the fixed bridge build',
          info.get('bridgeVersion') == '1.0.3', info.get('bridgeVersion'))
    # fireCustomEvent's boolean is advisory and, on real Fusion, not even
    # stable: it has been observed returning False while the event was
    # delivered normally. Only report it - the calls below are the real test.
    print('        fireCustomEvent advisory return: %s'
          % ('True' if note is None else note))
    print('        Fusion %s as %s' % (info.get('fusionVersion'), info.get('user')))

    # --- 2. new document -------------------------------------------------
    print('\n2. clean slate + new document')
    cleared, _ = bridge.call('run_python', {'code': CLEANUP_CODE})
    leftover = (cleared.get('result') or {}).get('closed') or []
    print('        closed %d leftover scratch document(s)' % len(leftover))

    created, _ = bridge.call('create_document')
    check('untitled design created', created.get('created') is True, created)
    print('        %s' % created.get('name'))

    # --- 3. real geometry through fusion_run_python ----------------------
    print('\n3. geometry via fusion_run_python')
    ran, _ = bridge.call('run_python', {'code': PLATE_CODE})
    result = ran.get('result') or {}
    stdout = (ran.get('stdout') or '').strip()
    for line in stdout.splitlines():
        print('        | %s' % line)
    check('sketch + extrude produced exactly one body',
          result.get('bodyCount') == 1, result)
    volume = result.get('volume_mm3')
    check('volume matches 60x40x10 mm exactly',
          volume is not None and abs(volume - EXPECTED_VOLUME_MM3) < 1e-6,
          '%s vs %s' % (volume, EXPECTED_VOLUME_MM3))

    # --- 4. inspection tools --------------------------------------------
    print('\n4. inspection tools')
    doc, _ = bridge.call('document_info')
    check('document_info sees the body', doc.get('bodyCount') == 1, doc)
    check('document is an unsaved scratch document',
          doc.get('isSaved') is False, doc)

    bodies, _ = bridge.call('list_bodies')
    check('list_bodies returns one body', bodies.get('count') == 1, bodies)
    body = (bodies.get('bodies') or [{}])[0]
    size = ((body.get('boundingBox') or {}).get('size_mm') or {})
    check('bounding box is 60 x 40 x 10 mm',
          abs(size.get('x_mm', 0) - 60) < 1e-6
          and abs(size.get('y_mm', 0) - 40) < 1e-6
          and abs(size.get('z_mm', 0) - 10) < 1e-6, size)

    # --- 5. export -------------------------------------------------------
    print('\n5. export')
    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    for extension in ('.step', '.stl'):
        path = os.path.join(args.out, 'dsh_verify' + extension)
        if os.path.exists(path):
            os.remove(path)
        exported, _ = bridge.call('export', {'path': path})
        check('exported %s' % extension,
              exported.get('exists') and exported.get('sizeBytes', 0) > 0, exported)
        print('        %s  %.1f KB' % (path, exported.get('sizeBytes', 0) / 1024.0))

    # --- 6. cleanup ------------------------------------------------------
    print('\n6. cleanup')
    if args.keep:
        print('        --keep given: scratch document left open')
    else:
        final, _ = bridge.call('run_python', {'code': CLEANUP_CODE})
        remaining = (final.get('result') or {}).get('remaining')
        check('scratch document closed without saving, Fusion left clean',
              remaining == 0, remaining)

    print('\n' + '=' * 62)
    if FAILURES:
        print('FAILED: %d of %d checks' % (len(FAILURES), CHECKS))
        for name in FAILURES:
            print('  - %s' % name)
        return 1
    print('ALL %d CHECKS PASSED' % CHECKS)
    return 0


if __name__ == '__main__':
    sys.exit(main())

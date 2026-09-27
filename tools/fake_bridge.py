#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A stand-in for the Fusion add-in, for testing without Fusion.

Listens on the bridge port and answers the same newline-delimited JSON
protocol FusionDSHBridge speaks, so the MCP server end can be exercised
end to end on a machine where Fusion is closed.

    python fake_bridge.py --port 27182
"""

import argparse
import json
import socket
import sys
import threading

FAKE = {
    'ping': {
        'bridge': 'FakeBridge',
        'bridgeVersion': '0.0.0-test',
        'fusionVersion': '2605.1.18 (simulated)',
        'user': 'selftest@example.invalid',
    },
    'document_info': {
        'name': 'FakeDesign',
        'isSaved': True,
        'isModified': False,
        'units': 'mm',
        'bodyCount': 1,
        'componentCount': 1,
        'parameterCount': 2,
        'boundingBox': {
            'size_mm': {'x_mm': 80.0, 'y_mm': 60.0, 'z_mm': 10.0},
        },
    },
    'list_bodies': {
        'count': 1,
        'bodies': [{
            'path': 'FakeDesign/Body1',
            'name': 'Body1',
            'isSolid': True,
            'volume_mm3': 48000.0,
            'area_mm2': 13600.0,
            'material': 'Steel',
        }],
    },
    'list_parameters': {
        'count': 1,
        'parameters': [{
            'name': 'width', 'expression': '80 mm', 'value': 8.0,
            'unit': 'mm', 'isUserParameter': True, 'comment': '',
        }],
    },
    'set_parameter': {'name': 'width', 'expression': '80 mm', 'value': 8.0, 'unit': 'mm'},
    'run_python': {'stdout': 'hello from the fake bridge\n', 'result': {'ok': True}},
}


def handle(connection):
    try:
        stream = connection.makefile('rwb')
        with stream:
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                request = json.loads(line.decode('utf-8'))
                command = request.get('cmd')
                if command in FAKE:
                    response = {'id': request.get('id'), 'ok': True, 'result': FAKE[command]}
                else:
                    response = {
                        'id': request.get('id'),
                        'ok': False,
                        'error': 'fake bridge has no command %r' % (command,),
                    }
                stream.write((json.dumps(response) + '\n').encode('utf-8'))
                stream.flush()
    except Exception as exc:
        sys.stderr.write('[fake-bridge] %s\n' % (exc,))
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=27182)
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(8)
    sys.stderr.write('[fake-bridge] listening on %s:%d\n' % (args.host, args.port))
    sys.stderr.flush()

    try:
        while True:
            connection, _ = server.accept()
            thread = threading.Thread(target=handle, args=(connection,))
            thread.daemon = True
            thread.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

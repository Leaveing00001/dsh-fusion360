#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fusion 360 MCP server - the DSH-facing half of the bridge.

Speaks the Model Context Protocol over stdio to DSH, and speaks a small
newline-delimited JSON protocol over a loopback socket to the FusionDSHBridge
add-in running inside Fusion 360.

Standard library only: DSH spawns this with whatever interpreter the profile
config names, and that interpreter is not guaranteed to have ``mcp`` or any
other third-party wheel installed.

stdout carries the MCP stream and nothing else.  All diagnostics go to stderr.
"""

import argparse
import json
import os
import socket
import sys
import traceback

SERVER_NAME = 'fusion360'
SERVER_VERSION = '1.0.0'

# Mirrors the versions @modelcontextprotocol/sdk 1.30.0 accepts, so whichever
# one DSH asks for is echoed back unchanged.
SUPPORTED_PROTOCOL_VERSIONS = (
    '2025-11-25',
    '2025-06-18',
    '2025-03-26',
    '2024-11-05',
    '2024-10-07',
)
FALLBACK_PROTOCOL_VERSION = '2025-03-26'

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 27182


def log(message):
    sys.stderr.write('[fusion-mcp] %s\n' % (message,))
    sys.stderr.flush()


class BridgeError(RuntimeError):
    """Raised when the Fusion add-in cannot be reached or reports an error."""


class FusionBridge(object):
    """Client for the loopback endpoint the Fusion add-in listens on."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=120.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._counter = 0

    def call(self, command, arguments=None):
        self._counter += 1
        request = {
            'id': self._counter,
            'cmd': command,
            'args': arguments or {},
        }
        payload = (json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8')

        try:
            connection = socket.create_connection((self.host, self.port), timeout=10.0)
        except OSError as exc:
            raise BridgeError(
                'cannot reach the Fusion 360 bridge on %s:%d (%s).\n'
                'Checklist:\n'
                '  1. Is Fusion 360 running?\n'
                '  2. Is the "FusionDSHBridge" add-in started? '
                '(Utilities > Add-Ins > Add-Ins tab > FusionDSHBridge > Run)\n'
                '  3. Does the port match bridge_config.json?'
                % (self.host, self.port, exc)
            )

        try:
            connection.settimeout(self.timeout)
            stream = connection.makefile('rwb')
            with stream:
                stream.write(payload)
                stream.flush()
                line = stream.readline()
        except socket.timeout:
            raise BridgeError(
                'the Fusion bridge did not answer within %.0fs; Fusion\'s main '
                'thread is probably blocked by a modal dialog' % (self.timeout,)
            )
        except OSError as exc:
            raise BridgeError('bridge connection failed: %s' % (exc,))
        finally:
            try:
                connection.close()
            except Exception:
                pass

        if not line:
            raise BridgeError('the Fusion bridge closed the connection without replying')

        try:
            response = json.loads(line.decode('utf-8'))
        except ValueError as exc:
            raise BridgeError('malformed reply from the Fusion bridge: %s' % (exc,))

        if not response.get('ok'):
            raise BridgeError(response.get('error') or 'unknown bridge failure')
        return response.get('result')


# --------------------------------------------------------------------------
# helpers shared by the tool handlers
# --------------------------------------------------------------------------

def _pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False)


def _resolve_path(path):
    """Expand a user-supplied path against the server's working directory."""
    path = os.path.expandvars(os.path.expanduser(str(path)))
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    return os.path.normpath(path)


# --------------------------------------------------------------------------
# tool catalogue
# --------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        'name': 'fusion_status',
        'description': (
            'Check whether the Fusion 360 bridge is reachable and report the '
            'Fusion version, signed-in user, and whether a document is open. '
            'Call this first when anything fails.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_document_info',
        'description': (
            'Describe the active Fusion design: name, saved/modified state, '
            'default length units, body and parameter counts, and the overall '
            'bounding box in millimetres.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_list_bodies',
        'description': (
            'List every BRep body in the active design, including nested '
            'component occurrences. Reports volume (mm^3), surface area (mm^2), '
            'bounding box (mm), solid/surface state and material.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_list_parameters',
        'description': (
            'List all model and user parameters of the active design with their '
            'expression, evaluated value, unit and comment.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_set_parameter',
        'description': (
            'Set a parameter expression and recompute the design. This is the '
            'cleanest way to drive a parametric model: change dimensions rather '
            'than rebuilding geometry.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': 'Parameter name, e.g. "d1" or "width".'},
                'expression': {
                    'type': 'string',
                    'description': 'New expression including units, e.g. "40 mm" or "d0 * 2".',
                },
            },
            'required': ['name', 'expression'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'fusion_create_document',
        'description': 'Create and activate a new empty Fusion design document.',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_list_documents',
        'description': 'List every document currently open in Fusion.',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'fusion_open_document',
        'description': (
            'Open a cloud data file by name from the active Fusion project. '
            'Note: this searches the cloud project tree, not local disk.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {'name': {'type': 'string', 'description': 'Cloud data file name.'}},
            'required': ['name'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'fusion_save',
        'description': 'Save the active document, optionally with a version description.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'description': {'type': 'string', 'description': 'Version description.'},
                'name': {'type': 'string', 'description': 'Name to use if never saved before.'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'fusion_export',
        'description': (
            'Export the active design to a file. The format is chosen from the '
            'extension: .step .stp .iges .igs .stl .obj .f3d .smt .sat. '
            'Missing parent directories are created.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Destination file path; relative paths resolve against the server working directory.',
                },
                'binary': {
                    'type': 'boolean',
                    'description': 'For .stl only: write binary STL (default true).',
                },
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'fusion_text_command',
        'description': (
            'Run a raw Fusion text command (the same commands the Text Commands '
            'palette accepts). An escape hatch for API surface the other tools '
            'do not cover.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {'command': {'type': 'string', 'description': 'Text command to execute.'}},
            'required': ['command'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'fusion_run_python',
        'description': (
            'Execute arbitrary Python inside Fusion on its primary thread. '
            'Pre-bound names: adsk, app, ui, design, root. stdout is captured '
            'and returned; assign to a variable named "result" to return a value. '
            'This is the general-purpose escape hatch for any modelling task the '
            'named tools do not cover - sketches, extrudes, fillets, patterns, '
            'CAM setup, and so on.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'code': {'type': 'string', 'description': 'Python source to execute inside Fusion.'},
            },
            'required': ['code'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'fusion_reload_bridge',
        'description': (
            'Re-execute the Fusion add-in from disk and restart its listener, '
            'without a trip through Fusion\'s Scripts and Add-Ins dialog. Use '
            'this after the add-in source has been updated on disk. The '
            'listener drops for about a second.'
        ),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
]


class ToolRuntime(object):
    def __init__(self, bridge):
        self.bridge = bridge

    def fusion_status(self, args):
        return _pretty(self.bridge.call('ping'))

    def fusion_document_info(self, args):
        return _pretty(self.bridge.call('document_info'))

    def fusion_list_bodies(self, args):
        return _pretty(self.bridge.call('list_bodies'))

    def fusion_list_parameters(self, args):
        return _pretty(self.bridge.call('list_parameters'))

    def fusion_set_parameter(self, args):
        return _pretty(self.bridge.call('set_parameter', {
            'name': args['name'],
            'expression': args['expression'],
        }))

    def fusion_create_document(self, args):
        return _pretty(self.bridge.call('create_document'))

    def fusion_list_documents(self, args):
        return _pretty(self.bridge.call('list_documents'))

    def fusion_open_document(self, args):
        return _pretty(self.bridge.call('open_document', {'name': args['name']}))

    def fusion_save(self, args):
        payload = {}
        if args.get('description') is not None:
            payload['description'] = args['description']
        if args.get('name') is not None:
            payload['name'] = args['name']
        return _pretty(self.bridge.call('save', payload))

    def fusion_export(self, args):
        payload = {'path': _resolve_path(args['path'])}
        if args.get('binary') is not None:
            payload['binary'] = bool(args['binary'])
        return _pretty(self.bridge.call('export', payload))

    def fusion_text_command(self, args):
        return _pretty(self.bridge.call('text_command', {'command': args['command']}))

    def fusion_run_python(self, args):
        result = self.bridge.call('run_python', {'code': args['code']})
        chunks = []
        stdout = result.get('stdout') or ''
        if stdout.strip():
            chunks.append('--- stdout ---\n%s' % (stdout.rstrip(),))
        chunks.append('--- result ---\n%s' % (_pretty(result.get('result')),))
        return '\n'.join(chunks)

    def fusion_reload_bridge(self, args):
        return _pretty(self.bridge.call('reload'))


# --------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# --------------------------------------------------------------------------

def _result(request_id, payload):
    return {'jsonrpc': '2.0', 'id': request_id, 'result': payload}


def _error(request_id, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': error}


def _negotiate_version(params):
    requested = (params or {}).get('protocolVersion')
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return FALLBACK_PROTOCOL_VERSION


def serve(bridge, stdin=None, stdout=None):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    runtime = ToolRuntime(bridge)

    for raw in stdin:
        line = raw.strip()
        if not line:
            continue

        try:
            message = json.loads(line.decode('utf-8'))
        except ValueError as exc:
            log('dropping unparseable line: %s' % (exc,))
            continue

        method = message.get('method')
        request_id = message.get('id')
        params = message.get('params') or {}

        # Notifications carry no id and must never be answered.
        if request_id is None:
            if method == 'notifications/initialized':
                log('client initialised')
            elif method == 'notifications/cancelled':
                log('client cancelled a request')
            continue

        try:
            if method == 'initialize':
                response = _result(request_id, {
                    'protocolVersion': _negotiate_version(params),
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
                })
            elif method == 'ping':
                response = _result(request_id, {})
            elif method == 'tools/list':
                response = _result(request_id, {'tools': TOOL_DEFINITIONS})
            elif method == 'tools/call':
                name = params.get('name')
                arguments = params.get('arguments') or {}
                handler = getattr(runtime, name, None)
                if handler is None or name not in {t['name'] for t in TOOL_DEFINITIONS}:
                    response = _result(request_id, {
                        'content': [{'type': 'text', 'text': 'unknown tool: %s' % (name,)}],
                        'isError': True,
                    })
                else:
                    try:
                        response = _result(request_id, {
                            'content': [{'type': 'text', 'text': handler(arguments)}],
                            'isError': False,
                        })
                    except BridgeError as exc:
                        log('tool %s failed: %s' % (name, exc))
                        response = _result(request_id, {
                            'content': [{'type': 'text', 'text': str(exc)}],
                            'isError': True,
                        })
                    except Exception:
                        detail = traceback.format_exc()
                        log('tool %s raised: %s' % (name, detail))
                        response = _result(request_id, {
                            'content': [{'type': 'text', 'text': detail}],
                            'isError': True,
                        })
            else:
                response = _error(request_id, -32601, 'method not found: %s' % (method,))
        except Exception:
            response = _error(request_id, -32603, 'internal error',
                              traceback.format_exc())

        stdout.write((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        stdout.flush()


def selftest():
    """Exercise the handshake and tool listing without needing Fusion."""
    import io

    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                    'clientInfo': {'name': 'selftest', 'version': '0'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'fusion_status', 'arguments': {}}},
    ]
    stdin = io.BytesIO(
        b''.join((json.dumps(r) + '\n').encode('utf-8') for r in requests)
    )
    stdout = io.BytesIO()

    serve(FusionBridge(), stdin=stdin, stdout=stdout)

    output = stdout.getvalue().decode('utf-8')
    for line in output.splitlines():
        if not line.strip():
            continue
        message = json.loads(line)
        if 'result' in message and 'tools' in message['result']:
            names = [t['name'] for t in message['result']['tools']]
            log('tools/list -> %d tools: %s' % (len(names), ', '.join(names)))
        elif message.get('id') == 3:
            text = message['result']['content'][0]['text']
            log('tools/call fusion_status -> isError=%s' % (message['result'].get('isError'),))
            log('  first line: %s' % (text.splitlines()[0] if text else '',))
        else:
            log('reply: %s' % (line[:160],))
    log('selftest complete')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fusion 360 MCP server for DSH')
    parser.add_argument('--host', default=os.environ.get('FUSION_DSH_BRIDGE_HOST', DEFAULT_HOST))
    parser.add_argument('--port', type=int,
                        default=int(os.environ.get('FUSION_DSH_BRIDGE_PORT', DEFAULT_PORT)))
    parser.add_argument('--timeout', type=float,
                        default=float(os.environ.get('FUSION_DSH_BRIDGE_TIMEOUT', '110')))
    parser.add_argument('--selftest', action='store_true',
                        help='run an offline handshake check and exit')
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    log('v%s starting; bridge target %s:%d' % (SERVER_VERSION, args.host, args.port))
    serve(FusionBridge(args.host, args.port, args.timeout))
    return 0


if __name__ == '__main__':
    sys.exit(main())

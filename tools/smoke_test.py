#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end smoke test for the Fusion 360 MCP server.

Runs the fake Fusion bridge and the MCP server inside a single process, wires
in-memory pipes between them, and asserts the full MCP exchange.  No Fusion,
no network, no subprocesses.

    python smoke_test.py
"""

import importlib.util
import io
import json
import os
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mcp = load('fusion_mcp_server', os.path.join(ROOT, 'mcp_server', 'fusion_mcp_server.py'))
fake = load('fake_bridge', os.path.join(HERE, 'fake_bridge.py'))

FAILURES = []
CHECKS = 0


def check(label, condition, detail=''):
    global CHECKS
    CHECKS += 1
    if condition:
        print('  PASS  %s' % label)
    else:
        print('  FAIL  %s %s' % (label, detail))
        FAILURES.append(label)


def free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def start_fake_bridge(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', port))
    server.listen(8)

    def loop():
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            thread = threading.Thread(target=fake.handle, args=(connection,))
            thread.daemon = True
            thread.start()

    thread = threading.Thread(target=loop)
    thread.daemon = True
    thread.start()
    return server


def run_server(port, requests):
    payload = b''.join((json.dumps(r) + '\n').encode('utf-8') for r in requests)
    stdin = io.BytesIO(payload)
    stdout = io.BytesIO()

    # stderr carries human diagnostics only; keep it out of the report.
    original_stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        mcp.serve(mcp.FusionBridge('127.0.0.1', port, timeout=10.0),
                  stdin=stdin, stdout=stdout)
    finally:
        sys.stderr = original_stderr

    replies = {}
    for line in stdout.getvalue().decode('utf-8').splitlines():
        if line.strip():
            message = json.loads(line)
            replies[message.get('id')] = message
    return replies


def main():
    print('Fusion 360 MCP server - end-to-end smoke test')
    print('=' * 60)

    port = free_port()
    server = start_fake_bridge(port)
    print('fake bridge listening on 127.0.0.1:%d\n' % port)

    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                    'clientInfo': {'name': 'smoke', 'version': '1'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'fusion_status', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
         'params': {'name': 'fusion_list_bodies', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
         'params': {'name': 'fusion_run_python', 'arguments': {'code': 'pass'}}},
        {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
         'params': {'name': 'fusion_does_not_exist', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 7, 'method': 'ping', 'params': {}},
        {'jsonrpc': '2.0', 'id': 8, 'method': 'resources/list', 'params': {}},
    ]

    try:
        replies = run_server(port, requests)
    finally:
        server.close()

    print('\nprotocol')
    init = replies.get(1, {})
    check('initialize returns a result', 'result' in init, init)
    check('protocolVersion echoes the client request',
          init.get('result', {}).get('protocolVersion') == '2025-11-25',
          init.get('result'))
    check('declares exactly the tools capability',
          init.get('result', {}).get('capabilities') == {'tools': {'listChanged': False}},
          init.get('result', {}).get('capabilities'))
    check('serverInfo names the server',
          init.get('result', {}).get('serverInfo', {}).get('name') == 'fusion360')
    check('notification produced no reply', None not in replies)

    print('\ntools/list')
    tools = replies.get(2, {}).get('result', {}).get('tools', [])
    names = [t['name'] for t in tools]
    check('returns 13 tools', len(tools) == 13, '%d returned' % len(tools))
    check('every tool carries a description',
          all(t.get('description') for t in tools))
    check('every tool carries an object inputSchema',
          all(t.get('inputSchema', {}).get('type') == 'object' for t in tools))
    check('tool names are unique', len(set(names)) == len(names))
    print('        %s' % ', '.join(names))

    print('\ntools/call')
    status = replies.get(3, {}).get('result', {})
    text = status.get('content', [{}])[0].get('text', '')
    check('fusion_status is not an error', status.get('isError') is False, status)
    check('fusion_status reached the bridge', 'FakeBridge' in text, text[:200])

    bodies = replies.get(4, {}).get('result', {})
    body_text = bodies.get('content', [{}])[0].get('text', '')
    check('fusion_list_bodies returned the body',
          'Body1' in body_text and '48000' in body_text, body_text[:200])

    runpy = replies.get(5, {}).get('result', {})
    runpy_text = runpy.get('content', [{}])[0].get('text', '')
    check('fusion_run_python surfaces stdout',
          'hello from the fake bridge' in runpy_text, runpy_text[:200])

    unknown_tool = replies.get(6, {}).get('result', {})
    check('unknown tool is reported as a tool error',
          unknown_tool.get('isError') is True, unknown_tool)

    print('\nerror handling')
    check('ping succeeds', replies.get(7, {}).get('result') == {},
          replies.get(7))
    unknown_method = replies.get(8, {})
    check('unknown method returns -32601',
          unknown_method.get('error', {}).get('code') == -32601, unknown_method)

    print('\n' + '=' * 60)
    if FAILURES:
        print('FAILED: %d of %d checks' % (len(FAILURES), CHECKS))
        for name in FAILURES:
            print('  - %s' % name)
        return 1
    print('ALL %d CHECKS PASSED' % CHECKS)
    return 0


if __name__ == '__main__':
    sys.exit(main())

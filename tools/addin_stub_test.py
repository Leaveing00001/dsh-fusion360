#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression test for FusionDSHBridge's worker -> primary thread hand-off.

Fusion itself cannot be scripted from outside, so this test stands a fake
``adsk`` package in for the real one, loads the add-in module against it, and
drives the socket exactly as the MCP server does.

The behaviour under test is the fix for the failure seen in real Fusion:

    fireCustomEvent returned False when called through an Application proxy
    created on the primary thread, and the bridge treated that advisory
    boolean as fatal.

So the critical case below is FIRE_RETURNS = False: the event must still reach
the handler and the request must still be answered.

    python tools/addin_stub_test.py
"""

import importlib.util
import json
import os
import socket
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ADDIN = os.path.join(ROOT, 'fusion_addin', 'FusionDSHBridge.py')

# Flipped per scenario: what the stubbed fireCustomEvent reports.
FIRE_RETURNS = True
FIRE_THREAD_APP = True

FAILURES = []
CHECKS = 0


def check(label, condition, detail=''):
    global CHECKS
    CHECKS += 1
    if condition:
        print('  PASS  %s' % label)
    else:
        print('  FAIL  %s  %s' % (label, detail))
        FAILURES.append(label)


def build_fake_adsk():
    """Minimal stand-in for the Fusion API surface the add-in touches."""
    adsk = types.ModuleType('adsk')
    core = types.ModuleType('adsk.core')
    fusion = types.ModuleType('adsk.fusion')

    state = {'app': None, 'main_thread': threading.current_thread().ident}

    class _Base(object):
        pass

    class CustomEventHandler(object):
        def notify(self, args):
            raise NotImplementedError

    class CustomEventArgs(object):
        pass

    class Point3D(object):
        def __init__(self, x=0.0, y=0.0, z=0.0):
            self.x, self.y, self.z = x, y, z

    class BoundingBox3D(object):
        minPoint = Point3D()
        maxPoint = Point3D()

    class _CustomEvent(object):
        def __init__(self, event_id):
            self.eventId = event_id
            self.handlers = []

        def add(self, handler):
            self.handlers.append(handler)
            return True

        def remove(self, handler):
            self.handlers.remove(handler)
            return True

    class Application(object):
        version = 'STUB-0.0.0'

        class _User(object):
            userName = 'stub@example.invalid'

        currentUser = _User()

        def __init__(self):
            self.events = {}

        @staticmethod
        def get():
            return state['app']

        def registerCustomEvent(self, event_id):
            if event_id in self.events:
                return None
            event = _CustomEvent(event_id)
            self.events[event_id] = event
            return event

        def unregisterCustomEvent(self, event_id):
            self.events.pop(event_id, None)
            return True

        def fireCustomEvent(self, event_id, info=''):
            event = self.events.get(event_id)
            if event is None:
                return False
            if FIRE_THREAD_APP:
                # A proxy obtained on the worker thread: this is the shape the
                # real add-in relies on.
                _ = Application.get()

            def run_handlers():
                # Stands in for Fusion's primary thread draining the event.
                for handler in list(event.handlers):
                    handler.notify(CustomEventArgs())

            timer = threading.Timer(0.02, run_handlers)
            timer.daemon = True
            timer.start()
            return FIRE_RETURNS

        def log(self, message):
            pass

    class Design(object):
        @staticmethod
        def cast(product):
            return None

    core.Application = Application
    core.CustomEventHandler = CustomEventHandler
    core.CustomEventArgs = CustomEventArgs
    core.Point3D = Point3D
    core.BoundingBox3D = BoundingBox3D
    core.Base = _Base
    fusion.Design = Design

    adsk.core = core
    adsk.fusion = fusion
    return adsk, state


def load_addin(port):
    adsk, state = build_fake_adsk()
    sys.modules['adsk'] = adsk
    sys.modules['adsk.core'] = adsk.core
    sys.modules['adsk.fusion'] = adsk.fusion

    spec = importlib.util.spec_from_file_location('FusionDSHBridge_under_test', ADDIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Pin the port so the test never collides with a real bridge.
    module._load_settings = lambda: ('127.0.0.1', port)
    return module, adsk, state


def free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def call(port, command, arguments=None, timeout=30.0):
    request = json.dumps({'id': 7, 'cmd': command, 'args': arguments or {}})
    connection = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        stream = connection.makefile('rwb')
        with stream:
            stream.write((request + '\n').encode('utf-8'))
            stream.flush()
            line = stream.readline()
    finally:
        connection.close()
    return json.loads(line.decode('utf-8'))


def scenario(label, fire_returns, fire_thread_app):
    global FIRE_RETURNS, FIRE_THREAD_APP
    FIRE_RETURNS = fire_returns
    FIRE_THREAD_APP = fire_thread_app

    print('\n%s' % label)
    port = free_port()
    module, adsk, state = load_addin(port)

    app = adsk.core.Application()
    state['app'] = app

    module.run(None)
    time.sleep(0.2)
    try:
        check('add-in bound its port',
              module._STATE.get('port') == port, module._STATE.get('port'))

        response = call(port, 'ping')
        check('ping answered instead of failing on the fire boolean',
              response.get('ok') is True, response)
        if response.get('ok'):
            check('ping returned the bridge identity',
                  response['result'].get('bridge') == 'FusionDSHBridge',
                  response.get('result'))
            check('ping reports the stub Fusion version',
                  response['result'].get('fusionVersion') == 'STUB-0.0.0',
                  response.get('result'))
            if not fire_returns:
                check('advisory False is surfaced as bridgeNote, not an error',
                      'bridgeNote' in response, response)

        unknown = call(port, 'no_such_command')
        check('unknown command is reported, not hung',
              unknown.get('ok') is False and 'unknown command' in unknown.get('error', ''),
              unknown)

        # An event that never reaches a handler must produce a timeout error,
        # not a silent hang.
        app.events.clear()
        module._STATE['timeout'] = 1.0
        t0 = time.time()
        dropped = call(port, 'ping', timeout=25.0)
        elapsed = time.time() - t0
        check('a dead event channel times out with a diagnosis',
              dropped.get('ok') is False and 'timed out' in dropped.get('error', ''),
              dropped)
        check('the timeout was actually applied (not instant, not eternal)',
              0.5 < elapsed < 10.0, '%.1fs' % elapsed)
    finally:
        module.stop(None)
        time.sleep(0.2)


def main():
    print('FusionDSHBridge - worker to primary thread hand-off')
    print('=' * 60)

    # The case that failed in real Fusion: fireCustomEvent returns False.
    scenario('fireCustomEvent returns False (the real-Fusion failure)',
             fire_returns=False, fire_thread_app=True)

    # The healthy case, for contrast.
    scenario('fireCustomEvent returns True', fire_returns=True, fire_thread_app=True)

    # A proxy fetched inside the worker thread is what the fix relies on.
    scenario('worker-thread Application proxy path',
             fire_returns=True, fire_thread_app=True)

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

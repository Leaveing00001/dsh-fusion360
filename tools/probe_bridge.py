#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe the Fusion DSH Bridge without going through DSH.

Connects straight to the add-in's loopback endpoint, so the Fusion half of the
bridge can be verified on its own.

    python tools/probe_bridge.py                 # one attempt
    python tools/probe_bridge.py --wait 300      # poll until it answers
    python tools/probe_bridge.py --cmd document_info
"""

import argparse
import json
import socket
import sys
import time

EXIT_UNREACHABLE = 1


def call(host, port, command, arguments=None, timeout=20.0):
    request = json.dumps({'id': 1, 'cmd': command, 'args': arguments or {}})
    connection = socket.create_connection((host, port), timeout=timeout)
    try:
        stream = connection.makefile('rwb')
        with stream:
            stream.write((request + '\n').encode('utf-8'))
            stream.flush()
            line = stream.readline()
    finally:
        connection.close()

    if not line:
        raise RuntimeError('bridge closed the connection without replying')
    response = json.loads(line.decode('utf-8'))
    if not response.get('ok'):
        raise RuntimeError(response.get('error') or 'bridge reported a failure')
    return response.get('result')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=27182)
    parser.add_argument('--cmd', default='ping')
    parser.add_argument('--args', default='{}', help='JSON object of command arguments')
    parser.add_argument('--args-file',
                        help='read the JSON arguments from a file instead of --args; '
                             'use this when the shell mangles inline JSON quotes')
    parser.add_argument('--wait', type=float, default=0.0,
                        help='seconds to keep retrying before giving up')
    args = parser.parse_args()

    deadline = time.time() + args.wait
    attempt = 0
    last_error = None

    if args.args_file:
        with open(args.args_file, 'r', encoding='utf-8') as handle:
            arguments = json.load(handle)
    else:
        arguments = json.loads(args.args)

    while True:
        attempt += 1
        try:
            result = call(args.host, args.port, args.cmd, arguments)
            print(json.dumps({
                'connected': True,
                'host': args.host,
                'port': args.port,
                'command': args.cmd,
                'attempts': attempt,
                'elapsedSeconds': round(args.wait - max(0.0, deadline - time.time()), 1),
                'result': result,
            }, ensure_ascii=False, indent=2))
            return 0
        except (OSError, RuntimeError) as exc:
            last_error = exc
            if time.time() >= deadline:
                break
            time.sleep(3)

    print(json.dumps({
        'connected': False,
        'host': args.host,
        'port': args.port,
        'attempts': attempt,
        'error': str(last_error),
    }, ensure_ascii=False, indent=2))
    return EXIT_UNREACHABLE


if __name__ == '__main__':
    sys.exit(main())

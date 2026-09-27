# -*- coding: utf-8 -*-
"""Fusion DSH Bridge - Autodesk Fusion 360 add-in (primary-thread half).

Opens a loopback TCP endpoint and executes newline-delimited JSON commands
received on it.  Every command is marshalled onto Fusion's primary thread with
a CustomEvent, because the Fusion API is not thread safe: the socket worker
threads only enqueue and wait, and ``_drain`` runs the actual API calls.

Wire format (newline-delimited JSON, UTF-8):

    request   {"id": <any>, "cmd": "<name>", "args": {...}}
    response  {"id": <any>, "ok": true,  "result": {...}}
              {"id": <any>, "ok": false, "error": "<text>"}

Deliberately dependency-free: the file is executed by the Python that Fusion
ships, so it may only use the standard library plus ``adsk``.
"""

import adsk.core
import adsk.fusion

import io
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback

BRIDGE_VERSION = '1.0.3'

# The wake event id must be unique among everything Fusion has registered.
EVENT_ID = 'FusionDSHBridge.Wake.v1'

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 27182

# Lengths inside the Fusion API are centimetres; these make output readable.
CM2MM = 10.0

_ADDIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SOURCE_PATH = os.path.abspath(__file__)
_LOG_PATH = os.path.join(_ADDIN_DIR, 'bridge.log')

# Populated by run(), cleared by stop().  Module level so it survives the
# repeated run()/stop() cycles Fusion performs on a loaded add-in.
_STATE = {}

# Fusion's event plumbing holds weak references: the event object and every
# handler must stay referenced from Python or the callback is collected.
_HANDLERS = []


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def _log(message):
    line = '[FusionDSHBridge] %s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), message)
    try:
        app = adsk.core.Application.get()
        if app:
            app.log(line)
    except Exception:
        pass
    try:
        with io.open(_LOG_PATH, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')
    except Exception:
        pass


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _load_settings():
    """Read bridge_config.json next to this file, falling back to defaults."""
    host, port = DEFAULT_HOST, DEFAULT_PORT
    path = os.path.join(_ADDIN_DIR, 'bridge_config.json')
    try:
        with io.open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        host = str(data.get('host', host))
        port = int(data.get('port', port))
    except Exception:
        # A missing or malformed file is not fatal; defaults are fine.
        pass
    return host, port


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------

def _jsonable(value, depth=0):
    """Coerce a Fusion API object into something json.dumps can encode."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if depth > 6:
        return repr(value)
    if isinstance(value, dict):
        return dict((str(k), _jsonable(v, depth + 1)) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in value]
    if isinstance(value, adsk.core.Point3D):
        return {'x': value.x, 'y': value.y, 'z': value.z}
    if isinstance(value, adsk.core.Vector3D):
        return {'x': value.x, 'y': value.y, 'z': value.z}
    if isinstance(value, adsk.core.BoundingBox3D):
        return {
            'min': _jsonable(value.minPoint, depth + 1),
            'max': _jsonable(value.maxPoint, depth + 1),
        }
    return repr(value)


def _point_mm(point):
    if point is None:
        return None
    return {
        'x_mm': point.x * CM2MM,
        'y_mm': point.y * CM2MM,
        'z_mm': point.z * CM2MM,
    }


def _bbox_mm(box):
    if box is None:
        return None
    lo, hi = box.minPoint, box.maxPoint
    return {
        'min_mm': _point_mm(lo),
        'max_mm': _point_mm(hi),
        'size_mm': {
            'x_mm': (hi.x - lo.x) * CM2MM,
            'y_mm': (hi.y - lo.y) * CM2MM,
            'z_mm': (hi.z - lo.z) * CM2MM,
        },
    }


# --------------------------------------------------------------------------
# context helpers
# --------------------------------------------------------------------------

def _design(app):
    return adsk.fusion.Design.cast(app.activeProduct)


def _require_design(app):
    design = _design(app)
    if design is None:
        raise RuntimeError(
            'no active Fusion design is open; open or create a design first'
        )
    return design


def _component_bodies(component):
    """Return a component's B-Rep body collection.

    Fusion 2705 removed ``Component.bodies``; ``bRepBodies`` is the current
    name. Index it rather than iterating, since only ``count``/``item`` are
    guaranteed across versions.
    """
    bodies = getattr(component, 'bRepBodies', None)
    if bodies is None:
        bodies = getattr(component, 'bodies', None)
    return bodies


def _iter_bodies(component, prefix=''):
    """Yield (path, body) for a component and, recursively, its occurrences."""
    name = prefix or component.name
    bodies = _component_bodies(component)
    if bodies is not None:
        for index in range(bodies.count):
            body = bodies.item(index)
            yield '%s/%s' % (name, body.name), body
    for occurrence in component.occurrences:
        child = occurrence.component
        yield from _iter_bodies(child, '%s/%s' % (name, occurrence.name))


# --------------------------------------------------------------------------
# commands - every one of these runs on the primary thread
# --------------------------------------------------------------------------

def _cmd_ping(app, args):
    user = None
    try:
        user = app.currentUser.userName if app.currentUser else None
    except Exception:
        pass
    return {
        'bridge': 'FusionDSHBridge',
        'bridgeVersion': BRIDGE_VERSION,
        'fusionVersion': app.version,
        'user': user,
        'host': _STATE.get('host'),
        'port': _STATE.get('port'),
    }


def _cmd_status(app, args):
    document = app.activeDocument
    design = _design(app)
    return {
        'hasActiveDocument': document is not None,
        'documentName': document.name if document else None,
        'isSaved': document.isSaved if document else None,
        'isModified': document.isModified if document else None,
        'documentType': (design.designType if design else None),
        'openDocumentCount': app.documents.count,
    }


def _cmd_document_info(app, args):
    design = _require_design(app)
    document = app.activeDocument
    root = design.rootComponent
    body_count = sum(1 for _ in _iter_bodies(root))
    try:
        units = design.unitsManager.defaultLengthUnits
    except Exception:
        units = None
    return {
        'name': document.name,
        'isSaved': document.isSaved,
        'isModified': document.isModified,
        'units': units,
        'bodyCount': body_count,
        'componentCount': root.occurrences.count + 1,
        'parameterCount': design.allParameters.count,
        'boundingBox': _bbox_mm(getattr(root, 'boundingBox', None)) if body_count else None,
    }


def _cmd_list_bodies(app, args):
    design = _require_design(app)
    bodies = []
    for path, body in _iter_bodies(design.rootComponent):
        try:
            entry = {
                'path': path,
                'name': body.name,
                'isSolid': body.isSolid,
                'isVisible': body.isVisible,
                'volume_mm3': body.volume * (CM2MM ** 3),
                'area_mm2': body.area * (CM2MM ** 2),
                'boundingBox': _bbox_mm(body.boundingBox),
            }
            try:
                entry['material'] = body.material.name if body.material else None
            except Exception:
                entry['material'] = None
            bodies.append(entry)
        except Exception as exc:
            bodies.append({'path': path, 'error': str(exc)})
    return {'count': len(bodies), 'bodies': bodies}


def _cmd_list_parameters(app, args):
    design = _require_design(app)
    parameters = []
    for index in range(design.allParameters.count):
        parameter = design.allParameters.item(index)

        # Parameter.isUserParameter was removed in Fusion 2705.  objectType
        # ("adsk::fusion::UserParameter" / "adsk::fusion::ModelParameter") is
        # the discriminator that has been stable across versions.
        try:
            object_type = parameter.objectType or ''
        except Exception:
            object_type = ''

        entry = {
            'name': parameter.name,
            'expression': parameter.expression,
            'unit': parameter.unit,
            'comment': parameter.comment,
            'isUserParameter': object_type.endswith('UserParameter'),
            'objectType': object_type,
        }
        try:
            # Only valid for numeric parameters; text parameters raise.
            entry['value'] = parameter.value
        except Exception:
            entry['value'] = None
        parameters.append(entry)
    return {'count': len(parameters), 'parameters': parameters}


def _cmd_set_parameter(app, args):
    design = _require_design(app)
    name = args.get('name')
    expression = args.get('expression')
    if not name or expression is None:
        raise ValueError('both "name" and "expression" are required')

    parameter = design.allParameters.itemByName(name)
    if parameter is None:
        user_parameters = design.userParameters
        parameter = user_parameters.itemByName(name)
        if parameter is None:
            raise ValueError('no parameter named %r' % (name,))

    parameter.expression = str(expression)
    design.computeAll()

    return {
        'name': parameter.name,
        'expression': parameter.expression,
        'value': parameter.value,
        'unit': parameter.unit,
    }


def _cmd_create_document(app, args):
    document = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    return {
        'name': document.name,
        'created': True,
    }


def _find_data_file(folder, name, depth=0):
    """Depth-first search for a DataFile by name inside a DataFolder."""
    if depth > 8:
        return None
    for index in range(folder.dataFiles.count):
        data_file = folder.dataFiles.item(index)
        if data_file.name == name:
            return data_file
    for index in range(folder.dataFolders.count):
        found = _find_data_file(folder.dataFolders.item(index), name, depth + 1)
        if found is not None:
            return found
    return None


def _cmd_open_document(app, args):
    """Open a cloud data file by name.

    ``Documents.open`` accepts only a DataFile, so this searches the active
    project's folder tree instead of taking a filesystem path.
    """
    name = args.get('name')
    if not name:
        raise ValueError('"name" is required (a cloud data file name)')

    try:
        root = app.data.activeProject.rootFolder
    except Exception:
        raise RuntimeError(
            'no active cloud project; sign in to Fusion and open a project first'
        )

    data_file = _find_data_file(root, str(name))
    if data_file is None:
        raise ValueError('no data file named %r in the active project' % (name,))

    document = app.documents.open(data_file, True)
    if document is None:
        raise RuntimeError('Fusion refused to open %r' % (name,))
    return {'name': document.name}


def _cmd_list_documents(app, args):
    documents = []
    for index in range(app.documents.count):
        document = app.documents.item(index)
        entry = {
            'name': document.name,
            'isSaved': document.isSaved,
            'isModified': document.isModified,
            'isActive': document == app.activeDocument,
        }
        try:
            entry['path'] = document.dataFile.filePath if document.dataFile else None
        except Exception:
            entry['path'] = None
        documents.append(entry)
    return {'count': len(documents), 'documents': documents}


def _cmd_save(app, args):
    document = app.activeDocument
    if document is None:
        raise RuntimeError('no active document to save')

    description = str(args.get('description', ''))

    if document.isSaved:
        document.save(description)
    else:
        # A never-saved document needs SaveAs, which requires a destination
        # folder.  That only exists when Fusion is signed in to a project.
        name = str(args.get('name') or document.name)
        try:
            folder = app.data.activeProject.rootFolder
        except Exception:
            raise RuntimeError(
                'the active document has never been saved and no cloud project '
                'is available; save it once by hand in Fusion, or sign in, then '
                'retry'
            )
        document.saveAs(name, folder, description, '')

    return {'name': document.name, 'isSaved': document.isSaved}


def _cmd_export(app, args):
    design = _require_design(app)
    path = args.get('path')
    if not path:
        raise ValueError('"path" is required')

    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)

    extension = os.path.splitext(path)[1].lower()
    manager = design.exportManager
    root = design.rootComponent

    if extension in ('.step', '.stp'):
        options = manager.createSTEPExportOptions(path)
    elif extension in ('.iges', '.igs'):
        options = manager.createIGESExportOptions(path)
    elif extension == '.stl':
        # Note the argument order: STL/OBJ take (geometry, filename), while
        # the STEP/IGES/SAT/SMT/F3D creators take (filename, geometry).
        options = manager.createSTLExportOptions(root, path)
        options.isBinary = bool(args.get('binary', True))
    elif extension == '.obj':
        options = manager.createOBJExportOptions(root, path)
    elif extension == '.f3d':
        options = manager.createFusionArchiveExportOptions(path)
    elif extension == '.smt':
        options = manager.createSMTExportOptions(path)
    elif extension == '.sat':
        options = manager.createSATExportOptions(path)
    else:
        raise ValueError(
            'unsupported export extension %r; expected one of '
            '.step .stp .iges .igs .stl .obj .f3d .smt .sat' % (extension,)
        )

    manager.execute(options)
    return {
        'path': path,
        'format': extension.lstrip('.'),
        'exists': os.path.isfile(path),
        'sizeBytes': os.path.getsize(path) if os.path.isfile(path) else 0,
    }


def _cmd_text_command(app, args):
    command = args.get('command')
    if not command:
        raise ValueError('"command" is required')
    output = app.executeTextCommand(str(command))
    return {'command': command, 'output': output}


def _cmd_run_python(app, args):
    """Execute arbitrary Python on the primary thread.

    This is the escape hatch that keeps the bridge useful for API surface the
    named commands do not cover.  Assign to ``result`` to return a value.
    """
    code = args.get('code')
    if not code:
        raise ValueError('"code" is required')

    scope = {
        'adsk': adsk,
        '__name__': '__dsh__',
    }
    scope['app'] = app
    try:
        scope['ui'] = app.userInterface
    except Exception:
        scope['ui'] = None
    design = _design(app)
    scope['design'] = design
    scope['root'] = design.rootComponent if design else None

    captured = io.StringIO()
    original = sys.stdout
    sys.stdout = captured
    try:
        exec(compile(code, '<dsh_run_python>', 'exec'), scope)
    finally:
        sys.stdout = original

    return {
        'stdout': captured.getvalue(),
        'result': _jsonable(scope.get('result')),
    }


def _cmd_reload(app, args):
    """Re-execute this add-in from disk, bypassing Fusion's UI.

    Without this, every code change needs a human to click Stop then Run in
    the Scripts and Add-Ins dialog. The restart is deferred so the reply to
    this request goes out over the old socket first.
    """
    def restart():
        time.sleep(0.5)
        try:
            stop(None)
        except Exception:
            pass
        try:
            with io.open(_SOURCE_PATH, 'r', encoding='utf-8') as handle:
                source = handle.read()
            # Rebinds this module's globals in place, so the `run` called
            # below is the freshly loaded one.
            exec(compile(source, _SOURCE_PATH, 'exec'), globals())
            run(None)
            _log('self-reload complete')
        except Exception:
            try:
                _log('self-reload failed: %s' % (traceback.format_exc(),))
            except Exception:
                pass

    thread = threading.Thread(target=restart, name='FusionDSHBridgeReload')
    thread.daemon = True
    thread.start()
    return {
        'scheduled': True,
        'versionBeforeReload': BRIDGE_VERSION,
        'source': _SOURCE_PATH,
    }


_COMMANDS = {
    'ping': _cmd_ping,
    'status': _cmd_status,
    'document_info': _cmd_document_info,
    'list_bodies': _cmd_list_bodies,
    'list_parameters': _cmd_list_parameters,
    'set_parameter': _cmd_set_parameter,
    'create_document': _cmd_create_document,
    'open_document': _cmd_open_document,
    'list_documents': _cmd_list_documents,
    'save': _cmd_save,
    'export': _cmd_export,
    'text_command': _cmd_text_command,
    'run_python': _cmd_run_python,
    'reload': _cmd_reload,
}


# --------------------------------------------------------------------------
# primary-thread marshalling
# --------------------------------------------------------------------------

class _Request(object):
    __slots__ = ('payload', 'done', 'response')

    def __init__(self, payload):
        self.payload = payload
        self.done = threading.Event()
        self.response = None


def _dispatch(app, payload):
    command = payload.get('cmd')
    handler = _COMMANDS.get(command)
    if handler is None:
        return {
            'ok': False,
            'error': 'unknown command %r; known: %s'
                     % (command, ', '.join(sorted(_COMMANDS))),
        }
    try:
        return {'ok': True, 'result': _jsonable(handler(app, payload.get('args') or {}))}
    except Exception:
        return {'ok': False, 'error': traceback.format_exc()}


def _drain():
    """Run every queued request.  Called on the primary thread only."""
    pending = _STATE.get('requests')
    if pending is None:
        return
    app = adsk.core.Application.get()
    while True:
        try:
            request = pending.get_nowait()
        except queue.Empty:
            return
        except Exception:
            return
        try:
            request.response = _dispatch(app, request.payload)
        except Exception:
            request.response = {'ok': False, 'error': traceback.format_exc()}
        finally:
            request.done.set()


class _WakeHandler(adsk.core.CustomEventHandler):
    """Receives the worker thread's wake-up on the primary thread."""

    def notify(self, args):
        _drain()


# --------------------------------------------------------------------------
# socket server
# --------------------------------------------------------------------------

def _fire(event_id, info=''):
    """Queue the wake-up event for execution on Fusion's primary thread.

    The Application proxy must be obtained *inside* the calling worker thread.
    A proxy created on the primary thread does not marshal across, and its
    fireCustomEvent call quietly returns False rather than raising.

    That boolean is advisory only: some Fusion builds report False for an event
    that does get queued, so callers wait on the drain instead of trusting it.
    Returns ``(queued, diagnostic)``.
    """
    try:
        app = adsk.core.Application.get()
    except Exception:
        return False, 'Application.get() raised on the worker thread: %s' % (
            traceback.format_exc(),)
    if app is None:
        return False, 'Application.get() returned None on the worker thread'
    try:
        queued = bool(app.fireCustomEvent(event_id, info))
    except Exception:
        return False, 'fireCustomEvent raised: %s' % (traceback.format_exc(),)
    return queued, 'fireCustomEvent returned %s' % (queued,)


def _handle_connection(connection):
    """Serve one client.  Runs on its own worker thread."""
    try:
        connection.settimeout(None)
        stream = connection.makefile('rwb')
        with stream:
            for raw in stream:
                line = raw.strip()
                if not line:
                    continue
                request_id = None
                try:
                    payload = json.loads(line.decode('utf-8'))
                    request_id = payload.get('id')

                    # Queue first, then wake the primary thread: firing before
                    # the request is visible could drain an empty queue.
                    pending = _STATE.get('requests')
                    if pending is None:
                        raise RuntimeError('the bridge is shutting down')
                    request = _Request(payload)
                    pending.put(request)

                    queued, detail = _fire(EVENT_ID, '')

                    timeout = _STATE.get('timeout', 100.0)
                    if not request.done.wait(timeout):
                        response = {
                            'id': request_id,
                            'ok': False,
                            'error': (
                                'timed out after %.0fs waiting for Fusion\'s primary '
                                'thread (%s). Either Fusion is blocked by a modal '
                                'dialog, or the wake-up event is not reaching its '
                                'handler.' % (timeout, detail)
                            ),
                        }
                    else:
                        response = dict(request.response or {})
                        response['id'] = request_id
                        if not queued:
                            # The event was handled despite the advisory False.
                            response['bridgeNote'] = detail
                except Exception:
                    response = {
                        'id': request_id,
                        'ok': False,
                        'error': traceback.format_exc(),
                    }

                stream.write(
                    (json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8')
                )
                stream.flush()
    except (ConnectionError, OSError):
        # Clients hang up routinely - a cancelled tool call, a probing client
        # that gave up, a closed socket. Not worth a traceback in the log.
        pass
    except Exception:
        _log('connection error: %s' % (traceback.format_exc(),))
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _serve(server_socket):
    _log('listening on %s:%s' % (_STATE.get('host'), _STATE.get('port')))
    while not _STATE.get('stopping'):
        try:
            connection, _ = server_socket.accept()
        except OSError:
            break
        except Exception:
            _log('accept failed: %s' % (traceback.format_exc(),))
            break
        thread = threading.Thread(
            target=_handle_connection,
            args=(connection,),
            name='FusionDSHBridgeConn',
        )
        thread.daemon = True
        thread.start()
    _log('server loop exited')


# --------------------------------------------------------------------------
# add-in entry points
# --------------------------------------------------------------------------

def _startup_probe():
    """Fire one round-trip event shortly after startup and log the outcome.

    This is the difference between "the port is open" and "the bridge actually
    works": a listening socket with a dead event channel looks healthy from
    outside until the first request times out.
    """
    time.sleep(3.0)
    # stop() clears _STATE, so re-read it rather than trusting either the
    # 'stopping' flag or the queue to still be there.
    pending = _STATE.get('requests')
    if pending is None or _STATE.get('stopping'):
        return
    probe = _Request({'cmd': 'ping', 'args': {}})
    pending.put(probe)
    queued, detail = _fire(EVENT_ID, '')
    if probe.done.wait(15.0):
        _log('startup probe: event round-trip OK (%s)' % (detail,))
    else:
        _log('startup probe: FAILED, primary thread never drained the queue '
             '(%s)' % (detail,))


def run(context):
    try:
        app = adsk.core.Application.get()
        host, port = _load_settings()

        # A previous run may have left the event registered.
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass

        custom_event = app.registerCustomEvent(EVENT_ID)
        if custom_event is None:
            raise RuntimeError(
                'could not register custom event %r; it is already in use' % (EVENT_ID,)
            )

        handler = _WakeHandler()
        if not custom_event.add(handler):
            raise RuntimeError('could not attach a handler to the custom event')
        _HANDLERS.append(handler)
        _HANDLERS.append(custom_event)

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(8)

        _STATE.clear()
        _STATE.update({
            'host': host,
            'port': port,
            'requests': queue.Queue(),
            'stopping': False,
            # Kept below the MCP server's own socket timeout so this error,
            # which is far more specific, is the one the caller sees.
            'timeout': 100.0,
            'socket': server_socket,
        })

        thread = threading.Thread(
            target=_serve, args=(server_socket,), name='FusionDSHBridgeServer'
        )
        thread.daemon = True
        _STATE['thread'] = thread
        thread.start()

        _log('v%s started on %s:%s (Fusion %s)'
             % (BRIDGE_VERSION, host, port, app.version))

        # Deferred, so run() has returned and Fusion is idle by the time the
        # event is fired; firing from inside run() could never be handled.
        probe = threading.Thread(target=_startup_probe, name='FusionDSHBridgeProbe')
        probe.daemon = True
        probe.start()
    except Exception:
        _log('run() failed: %s' % (traceback.format_exc(),))


def stop(context):
    try:
        _STATE['stopping'] = True

        server_socket = _STATE.get('socket')
        if server_socket is not None:
            try:
                server_socket.close()
            except Exception:
                pass

        app = adsk.core.Application.get()
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass

        while _HANDLERS:
            _HANDLERS.pop()

        _STATE.clear()
        _log('stopped')
    except Exception:
        _log('stop() failed: %s' % (traceback.format_exc(),))

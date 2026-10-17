#!/usr/bin/env python3
"""Exercise the compiled C ABI: a stalled stream must not block other calls."""

import base64
import ctypes as c
import json
import sys
import threading


class Buffer(c.Structure):
    _fields_ = [("ptr", c.c_void_p), ("len", c.c_size_t)]


Call = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_char_p, c.c_void_p, c.c_size_t, c.POINTER(Buffer))
Free = c.CFUNCTYPE(None, c.c_void_p, c.c_size_t)
PluginCall = c.CFUNCTYPE(c.c_int, c.c_char_p, c.c_void_p, c.c_size_t, c.POINTER(Buffer))
Shutdown = c.CFUNCTYPE(None)


class Host(c.Structure):
    _fields_ = [("abi_version", c.c_uint32), ("host_ctx", c.c_void_p), ("call", Call), ("free_buffer", Free)]


class Plugin(c.Structure):
    _fields_ = [("abi_version", c.c_uint32), ("call", PluginCall), ("free_buffer", Free), ("shutdown", Shutdown)]


def run(library):
    buffers, reads, errors = {}, {}, []
    mu = threading.Lock()
    entered, release = threading.Event(), threading.Event()
    closed = {name: threading.Event() for name in ["slow", "fast"]}
    emitted = []

    @Call
    def host_call(_, method, data, size, out):
        try:
            method = method.decode()
            req = json.loads(c.string_at(data, size))
            result = {}
            if method == "host.model.execute_stream":
                result = {"status_code": 200, "stream_id": req["headers"]["X-Probe"][0]}
            elif method == "host.model.stream_read":
                stream = req["stream_id"]
                if stream == "slow":
                    entered.set()
                    if not release.wait(15):
                        raise AssertionError("test did not release stalled upstream")
                reads[stream] = reads.get(stream, 0) + 1
                result = {"done": reads[stream] > 1}
                if reads[stream] == 1:
                    result["payload"] = base64.b64encode(stream.encode()).decode()
            elif method == "host.stream.emit":
                emitted.append((req["stream_id"], base64.b64decode(req["payload"]).decode()))
            elif method == "host.stream.close":
                if req.get("error"):
                    raise AssertionError(req["error"])
                closed[req["stream_id"]].set()
            raw = json.dumps({"ok": True, "result": result}).encode()
        except Exception as exc:
            errors.append(str(exc))
            raw = json.dumps({"ok": False, "error": {"code": "test_error", "message": str(exc)}}).encode()
        buf = c.create_string_buffer(raw)
        address = c.addressof(buf)
        with mu:
            buffers[address] = buf
        out[0] = Buffer(address, len(raw))
        return 0

    @Free
    def host_free(ptr, _):
        with mu:
            del buffers[ptr]

    host, plugin = Host(1, None, host_call, host_free), Plugin()
    lib = c.CDLL(library)
    lib.cliproxy_plugin_init.argtypes = [c.POINTER(Host), c.POINTER(Plugin)]
    assert lib.cliproxy_plugin_init(c.byref(host), c.byref(plugin)) == 0

    def call(method, payload):
        raw = json.dumps(payload).encode()
        request = c.create_string_buffer(raw)
        out = Buffer()
        code = plugin.call(method.encode(), request, len(raw), c.byref(out))
        try:
            env = json.loads(c.string_at(out.ptr, out.len))
        finally:
            plugin.free_buffer(out.ptr, out.len)
        assert code == 0 and env["ok"], env
        return env["result"]

    config = b"""client_models: [probe]
clients:
  codex:
    source_formats: [responses]
    candidates:
      - name: first
        channel_type: codex_oauth
        provider: codex
        model: target
        priority: 100
"""
    lifecycle = {"config_yaml": base64.b64encode(config).decode()}
    call("plugin.register", lifecycle)

    def stream(name):
        call("executor.execute_stream", {"Model": "probe", "SourceFormat": "responses", "Stream": True,
             "stream_id": name, "Headers": {"X-Probe": [name]}, "OriginalRequest": base64.b64encode(b'{"model":"probe"}').decode()})

    workers = []

    def background(fn):
        done = threading.Event()

        def worker():
            try:
                fn()
            except Exception as exc:
                errors.append(str(exc))
            finally:
                done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        workers.append(thread)
        return done

    stopped = None
    try:
        stream("slow")
        assert entered.wait(5), "slow stream never entered the real C host callback"
        background(lambda: stream("fast"))
        assert closed["fast"].wait(3), "stalled upstream blocked an independent stream at the C ABI"
        assert ("fast", "fast") in emitted and not closed["slow"].is_set()
        configured = background(lambda: call("plugin.reconfigure", lifecycle))
        assert configured.wait(3), "stalled upstream blocked reconfiguration"
        stopped = background(plugin.shutdown)
        assert not stopped.wait(0.1), "shutdown released the host while a stream was still active"
    finally:
        release.set()
        for worker in workers:
            worker.join(10)
            assert not worker.is_alive(), "ABI worker did not drain"
        if stopped is None:
            plugin.shutdown()
    assert closed["slow"].is_set(), "shutdown did not drain the active stream"
    assert not errors, errors
    assert not buffers, "host response buffers were not freed before shutdown"
    print("PASS: stalled stream isolated; reconfigure proceeds; shutdown drains callbacks and frees buffers")


if __name__ == "__main__":
    run(sys.argv[1])

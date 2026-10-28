"""Offline self-test: proves the counters work before any real endpoint is used.

Starts a mock /v1/responses server with three personalities and runs the probe
against each:

    good   emits 3 write_file calls then a summary   -> CALIBRATED : True
    lazy   emits text only, never writes             -> CALIBRATED : False,
                                                        b -> suspicious_no_mutation
    loop   emits the same read_file forever          -> b -> repeat_loop at step 3

No network, no API key, no model. If this fails, the harness is wrong and any
number it prints against a real endpoint is meaningless.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MODE = {"name": "good"}
PORT = 18771


def text_item(text):
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def fn_call(cid, name, args):
    return {"type": "function_call", "call_id": cid, "name": name,
            "arguments": json.dumps(args)}


def respond(items):
    mode = MODE["name"]
    step = sum(1 for i in items if i.get("type") == "function_call_output")
    if mode == "good":
        if step == 0:
            return [fn_call("c1", "write_file",
                            {"path": "control_out/one.txt", "content": "alpha"}),
                    fn_call("c2", "write_file",
                            {"path": "control_out/two.txt", "content": "bravo"}),
                    fn_call("c3", "write_file",
                            {"path": "control_out/three.txt", "content": "charlie"})]
        return [text_item("wrote three files")]
    if mode == "lazy":
        return [text_item("**Done.** The bundle looks fine.")]
    if mode == "loop":
        return [fn_call(f"r{step}", "read_file",
                        {"path": "service_api/config.py"})]
    raise AssertionError(mode)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode())
        out = respond(body.get("input") or [])
        payload = {
            "status": "completed",
            "output": out,
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def run(args, mode):
    MODE["name"] = mode
    env = dict(os.environ)
    env["G2A_BASE"] = f"http://127.0.0.1:{PORT}/v1/responses"
    env["G2A_KEY"] = "selftest"
    env["G2A_MODEL"] = "mock"
    r = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "reasoning_probe.py")]
                       + args, cwd=HERE, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout + r.stderr


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    work = os.path.join(HERE, "_selftest_runs")
    fails = []

    rc, out = run(["control", "--workdir", work], "good")
    ok = rc == 0 and "CALIBRATED : True" in out
    print(f"[{'PASS' if ok else 'FAIL'}] control/good -> CALIBRATED True (rc={rc})")
    if not ok:
        fails.append("control/good")
        print(out)

    rc, out = run(["control", "--workdir", work], "lazy")
    ok = rc == 2 and "CALIBRATED : False" in out
    print(f"[{'PASS' if ok else 'FAIL'}] control/lazy -> CALIBRATED False (rc={rc})")
    if not ok:
        fails.append("control/lazy")
        print(out)

    rc, out = run(["b", "--rounds", "1", "--workdir", work, "--max-steps", "4"], "lazy")
    ok = "suspicious_no_mutation" in out and "零写入率               : 1/1" in out
    print(f"[{'PASS' if ok else 'FAIL'}] b/lazy -> suspicious_no_mutation, 1/1 zero-write")
    if not ok:
        fails.append("b/lazy")
        print(out)

    rc, out = run(["b", "--rounds", "1", "--workdir", work, "--max-steps", "10"], "loop")
    ok = "repeat_loop" in out and "重复调用死循环         : 1/1" in out
    print(f"[{'PASS' if ok else 'FAIL'}] b/loop -> repeat_loop tripped")
    if not ok:
        fails.append("b/loop")
        print(out)

    rc, out = run(["b", "--rounds", "1", "--workdir", work, "--max-steps", "4"], "good")
    ok = "intent=3 writes_ok=3 disk=3" in out
    print(f"[{'PASS' if ok else 'FAIL'}] b/good -> intent==writes_ok==disk==3")
    if not ok:
        fails.append("b/good")
        print(out)

    srv.shutdown()
    print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

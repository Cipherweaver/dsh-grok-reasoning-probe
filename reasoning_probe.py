"""
Reasoning / mutation probe against a grok2api-compatible /v1/responses endpoint.

The question this file answers is not "how many reasoning tokens did we get" --
that number turned out to be decoupled from output quality. The question is:

    did the model actually perform the mutation the task asked for, and if not,
    what did it do instead?

That question needs a measurement that cannot be fooled by the model's own
report. Three counters must agree before a round counts as healthy:

    intent         write/edit tool calls the model emitted (counted by call_id,
                   not by item type -- see NOTE 4 in README)
    writes_ok      those calls whose arguments parsed and whose handler ran
    disk_mutations files under the work tree whose bytes actually changed

    CALIBRATED : True   <=>   intent == writes_ok == disk_mutations and all > 0

Run `control` first. If control is not calibrated, the harness is broken and
every number `b` prints afterwards is measuring the harness.

Subcommands
    control    known-good calibration prompt; must print CALIBRATED : True
    b          N rounds of a real agent loop against a bench bundle
    a          direction A: does forcing a text deliverable restore reasoning?
    sweep      the original 6-bucket input-length sweep (reasoning hypothesis,
               now dead -- kept because the negative result is the baseline)

Environment
    G2A_BASE    full endpoint URL, default http://127.0.0.1:8000/v1/responses
    G2A_KEY     bearer token, REQUIRED, no default on purpose
    G2A_MODEL   model name, default grok-4.7
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("G2A_BASE", "http://127.0.0.1:8000/v1/responses")
KEY = os.environ.get("G2A_KEY", "")
MODEL = os.environ.get("G2A_MODEL", "grok-4.7")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BENCH = os.path.join(HERE, "bench")
DEFAULT_WORKDIR = os.path.join(HERE, "_runs")

MUTATING = ("write_file", "edit_file")

# Verbs that make a task an execution task. If the task text contains one of
# these and the round produced no mutation, the round is not "done", whatever
# the model said.
EXEC_VERBS = ("改", "写", "创建", "替换", "改名", "重命名", "提交", "部署",
              "edit", "write", "create", "replace", "rename", "commit", "deploy",
              "fix", "update", "modify", "patch")

PARA = ("The scheduler records every dispatch decision in an append-only journal, "
        "including the account that was chosen, the route it belonged to, and the "
        "measured latency of the previous attempt. Operators read that journal when "
        "they need to reconstruct why a particular request landed where it did. ")

DEFAULT_FINAL = "When all four are done, summarise."
FINAL_TASK = ("Audit the deployment. Use read_file to inspect these files one at a time, "
              "in this order: C:/proj/a.txt, C:/proj/b.txt, C:/proj/c.txt, C:/proj/d.txt. "
              "After each file, report in one line what you found, then read the next. ")

FILE_BODY = {
    "C:/proj/a.txt": "service=api\nreplicas=3\nport=8080\n",
    "C:/proj/b.txt": "service=worker\nreplicas=1\nqueue=jobs\n",
    "C:/proj/c.txt": "service=cache\nreplicas=2\nengine=redis\n",
    "C:/proj/d.txt": "service=db\nreplicas=1\nengine=sqlite\npath=/data/backend.db\n",
}


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def call(payload, timeout=600):
    """One non-streaming request. Never raises on HTTP errors -- a failed step
    must show up as a row in the result, not as a dead run."""
    if not KEY:
        sys.exit("G2A_KEY is not set. export G2A_KEY=<token>")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE, data=data, method="POST", headers={
        "Authorization": "Bearer " + KEY,
        "Content-Type": "application/json",
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode()[:400],
                "elapsed": time.time() - t0}
    u = body.get("usage") or {}
    out_items = body.get("output") or []
    text = "".join(c.get("text", "") for i in out_items if i.get("type") == "message"
                   for c in (i.get("content") or []))
    return {
        "in": u.get("input_tokens"),
        "cached": (u.get("input_tokens_details") or {}).get("cached_tokens"),
        "out": u.get("output_tokens"),
        "reason": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
        "text_len": len(text),
        "text": text,
        "calls": collect_calls(out_items),
        "reason_items": [{"id": r.get("id"), "encrypted_content": r.get("encrypted_content")}
                         for r in out_items if r.get("type") == "reasoning"],
        "status": body.get("status"),
        "elapsed": round(time.time() - t0, 1),
    }


def collect_calls(out_items):
    """Tool calls, keyed on call_id.

    Filtering on `type == "function_call"` silently drops whole classes of
    calls: some gateways emit `custom_tool_call`, some put the tool name inside
    the arguments blob, some stream `input` instead of `arguments`. Any item
    carrying a call_id is a call; a call with unparseable arguments is recorded
    with bad_args=True rather than being dropped, because "the model tried to
    write and the JSON was malformed" is a distinct failure from "the model
    never tried".
    """
    calls = []
    for it in out_items:
        cid = it.get("call_id")
        if not cid:
            continue
        raw = it.get("arguments")
        if raw is None:
            raw = it.get("input")
        if isinstance(raw, dict):
            args, bad = raw, False
        else:
            try:
                args = json.loads(raw or "{}")
                bad = False
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": raw}
                bad = True
        name = it.get("name") or it.get("tool_name") or ""
        if not name and isinstance(args, dict):
            name = args.get("name") or args.get("tool") or ""
        calls.append({
            "call_id": cid,
            "name": name,
            "args": args if isinstance(args, dict) else {"_raw": args},
            "bad_args": bad,
            "raw_len": len(raw) if isinstance(raw, str) else 0,
        })
    return calls


def filler(tokens):
    if tokens <= 0:
        return ""
    need = tokens * 4          # ~4 chars per token for this latin text
    body = PARA * (need // len(PARA) + 1)
    return body[:need]


# --------------------------------------------------------------------------
# bench tree helpers
# --------------------------------------------------------------------------

def snapshot(root):
    """path -> bytes for every file under root."""
    out = {}
    for base, _, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            out[os.path.relpath(p, root)] = open(p, "rb").read()
    return out


def mutations(before, root):
    """Files whose bytes differ from the snapshot, plus adds and deletes."""
    after = snapshot(root)
    changed = [k for k in before if k in after and after[k] != before[k]]
    added = [k for k in after if k not in before]
    removed = [k for k in before if k not in after]
    return sorted(changed + added), sorted(added), sorted(removed)


def resolve(work, bench, target):
    """Models echo absolute paths from the prompt; rebase them into the work tree."""
    if not target:
        return ""
    if os.path.isabs(target):
        return os.path.join(work, os.path.relpath(target, bench))
    return os.path.join(work, target)


# --------------------------------------------------------------------------
# the agent loop
# --------------------------------------------------------------------------

TOOLS = [
    {"type": "function", "name": "read_file",
     "description": "Read a UTF-8 text file and return its contents.",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]}},
    {"type": "function", "name": "write_file",
     "description": "Write content to a file, replacing it wholesale.",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"},
                                   "content": {"type": "string"}},
                    "required": ["path", "content"]}},
    {"type": "function", "name": "edit_file",
     "description": "Replace one exact literal occurrence of old in a file.",
     "parameters": {"type": "object",
                    "properties": {"path": {"type": "string"},
                                   "old": {"type": "string"},
                                   "new": {"type": "string"}},
                    "required": ["path", "old", "new"]}},
]

NUDGE_NO_MUTATION = (
    "你没有产生任何文件写入。任务要求执行修改，不是报告。"
    "要么现在调用 write_file / edit_file 完成修改，要么明确说明为什么无法执行。"
)

NUDGE_REPEAT = (
    "你已经用相同参数调用 {name} {n} 次。重复读取不会产生新信息。"
    "请直接执行修改，或说明缺什么。"
)


def run_agent_loop(work, bench, task, max_steps=30, max_nudges=1,
                   repeat_limit=3, verbose=True):
    """One agent loop over a copied bench tree.

    Returns a dict with the three counters plus the per-step trace. Two guards
    run inside the loop, because both failure modes are cheap to detect here
    and expensive to discover downstream:

      repeat_limit  identical (name, arguments) this many times in a row is a
                    read loop, not progress -- nudge once, then stop paying for it
      max_nudges    a completed turn with zero mutations on an execution task
                    gets exactly one chance to correct itself, then the round is
                    recorded as blocked rather than retried forever
    """
    before = snapshot(work)
    items = [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": task}]}]
    intent = writes_ok = reads = 0
    bad_args = 0
    nudges = 0
    seen = []
    trace = []
    stop_reason = "max_steps"

    for step in range(1, max_steps + 1):
        res = call({"model": MODEL, "input": items, "tools": TOOLS,
                    "tool_choice": "auto", "store": False, "stream": False})
        if "error" in res:
            trace.append({"step": step, "error": res["error"],
                          "detail": res.get("detail", "")[:200]})
            stop_reason = "http_error"
            break
        trace.append({"step": step, "in": res["in"], "out": res["out"],
                      "reason": res["reason"], "text_len": res["text_len"],
                      "calls": [(c["name"], c["bad_args"]) for c in res["calls"]]})
        if verbose:
            print(f"  step{step:>2}  in={res['in']} out={res['out']} "
                  f"reason={res['reason']} text_len={res['text_len']} "
                  f"calls={[c['name'] for c in res['calls']]} {res['elapsed']}s")

        for r in res["reason_items"]:
            item = {"type": "reasoning", "id": r["id"], "summary": []}
            if r.get("encrypted_content"):
                item["encrypted_content"] = r["encrypted_content"]
            items.append(item)
        if res["text_len"]:
            items.append({"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": res["text"]}]})

        # ---- zero-mutation completion guard -------------------------------
        if not res["calls"]:
            done, _, _ = mutations(before, work)
            execution_task = any(v in task.lower() for v in EXEC_VERBS)
            if not done and execution_task and nudges < max_nudges:
                nudges += 1
                if verbose:
                    print(f"   -> completed with 0 mutations; nudging ({nudges}/{max_nudges})")
                items.append({"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": NUDGE_NO_MUTATION}]})
                continue
            stop_reason = "completed" if done or not execution_task else "suspicious_no_mutation"
            break

        # ---- repeated-identical-call guard --------------------------------
        sigs = [(c["name"], json.dumps(c["args"], sort_keys=True)) for c in res["calls"]]
        seen.extend(sigs)
        tail = seen[-repeat_limit:]
        if len(tail) == repeat_limit and len(set(tail)) == 1:
            if verbose:
                print(f"   -> identical {tail[0][0]} x{repeat_limit}; nudging and stopping")
            items.append({"type": "message", "role": "user",
                          "content": [{"type": "input_text",
                                       "text": NUDGE_REPEAT.format(name=tail[0][0],
                                                                   n=repeat_limit)}]})
            stop_reason = "repeat_loop"
            break

        for c in res["calls"]:
            intent += 1
            name, a = c["name"], c["args"]
            items.append({"type": "function_call", "call_id": c["call_id"],
                          "name": name, "arguments": json.dumps(a)})
            target = a.get("path", "") if isinstance(a, dict) else ""
            path = resolve(work, bench, target)

            if c["bad_args"]:
                bad_args += 1
                out = "ERROR: arguments were not valid JSON"
            elif name == "read_file":
                reads += 1
                out = "ERROR: not found"
                if path and os.path.isfile(path):
                    out = open(path, encoding="utf-8", errors="replace").read()
            elif name == "write_file":
                ok, out = _do_write(path, a)
                writes_ok += ok
            elif name == "edit_file":
                ok, out = _do_edit(path, a)
                writes_ok += ok
            else:
                out = f"ERROR: unknown tool {name}"
            items.append({"type": "function_call_output", "call_id": c["call_id"],
                          "output": out})

    changed, added, removed = mutations(before, work)
    return {
        "steps": len(trace),
        "intent": intent,
        "writes_ok": writes_ok,
        "disk_mutations": len(changed),
        "bad_args": bad_args,
        "reads": reads,
        "nudges": nudges,
        "stop_reason": stop_reason,
        "changed_files": changed,
        "added_files": added,
        "removed_files": removed,
        "final_out": trace[-1].get("out") if trace else None,
        "final_reason": trace[-1].get("reason") if trace else None,
        "final_text_len": trace[-1].get("text_len") if trace else None,
        "trace": trace,
    }


def _do_write(path, a):
    if not path:
        return 0, "ERROR: missing path"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    open(path, "w", encoding="utf-8", newline="").write(a.get("content", ""))
    return 1, "ok"


def _do_edit(path, a):
    if not path:
        return 0, "ERROR: missing path"
    if not os.path.isfile(path):
        return 0, "ERROR: not found"
    body = open(path, encoding="utf-8").read()
    old = a.get("old", "")
    if not old or old not in body:
        return 0, "ERROR: old string not found"
    open(path, "w", encoding="utf-8", newline="").write(
        body.replace(old, a.get("new", ""), 1))
    return 1, "ok"


# --------------------------------------------------------------------------
# control: calibrate the harness before trusting any measurement
# --------------------------------------------------------------------------

CONTROL_TASK = (
    "This is a harness calibration, not a task. You MUST call write_file at "
    "least 3 times in this turn, on these paths, with these exact contents:\n"
    "  control_out/one.txt   <- alpha\n"
    "  control_out/two.txt   <- bravo\n"
    "  control_out/three.txt <- charlie\n"
    "If you reply without making at least 3 successful write_file calls the run "
    "is FAILED. Do not summarise. Do not ask for confirmation. Write the files."
)


def cmd_control(args):
    """A model that will not write is not measurable. This prompt demands writes
    outright: if the three counters do not line up here, the counters are wrong,
    not the model."""
    work = os.path.join(args.workdir, "_control")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    print("=== CONTROL ===")
    print(f"work: {work}\n")
    r = run_agent_loop(work, work, CONTROL_TASK, max_steps=args.max_steps,
                       max_nudges=args.max_nudges)
    ok = (r["intent"] == r["writes_ok"] == r["disk_mutations"]
          and r["disk_mutations"] > 0)
    print("\n--- counters ---")
    print(f"  intent          : {r['intent']}")
    print(f"  writes_ok       : {r['writes_ok']}")
    print(f"  disk_mutations  : {r['disk_mutations']}  {r['changed_files']}")
    print(f"  bad_args        : {r['bad_args']}")
    print(f"  stop_reason     : {r['stop_reason']}")
    if r["bad_args"]:
        print("\n  WARNING: a call carried unparseable arguments. intent counted it, "
              "writes_ok did not. Fix the parser before trusting any b result.")
    print(f"\nCALIBRATED : {ok}")
    with open(os.path.join(args.workdir, "control_result.json"), "w",
              encoding="utf-8") as f:
        json.dump(r, f, indent=1, ensure_ascii=False)
    return 0 if ok else 2


# --------------------------------------------------------------------------
# b: the measurement
# --------------------------------------------------------------------------

def cmd_b(args):
    bench = os.path.abspath(args.bench)
    if not os.path.isdir(bench):
        sys.exit(f"bench baseline not found: {bench}")
    base = snapshot(bench)
    print(f"bench baseline: {len(base)} files under {bench}")
    task = args.bench_prompt or (
        f"You are auditing the bundle at {bench}. Read every file, then make the "
        "edits the bundle's own instructions ask for. Report one line per file "
        "at the end: path, byte size, and what you changed.")

    rounds = []
    for n in range(1, args.rounds + 1):
        work = os.path.join(args.workdir, f"round{n}")
        if os.path.isdir(work):
            shutil.rmtree(work)
        shutil.copytree(bench, work)
        print(f"\n--- round {n} ---")
        r = run_agent_loop(work, bench, task, max_steps=args.max_steps,
                           max_nudges=args.max_nudges)
        r["round"] = n
        rounds.append(r)
        print(f"  intent={r['intent']} writes_ok={r['writes_ok']} "
              f"disk={r['disk_mutations']} stop={r['stop_reason']}")

    total = len(rounds)
    zero = sum(1 for r in rounds if r["disk_mutations"] == 0)
    looped = sum(1 for r in rounds if r["stop_reason"] == "repeat_loop")
    suspicious = sum(1 for r in rounds if r["stop_reason"] == "suspicious_no_mutation")
    print("\n===== DIRECTION B =====")
    print(f"rounds                 : {total}")
    print(f"零写入率               : {zero}/{total} = {zero / total:.0%}")
    print(f"重复调用死循环         : {looped}/{total} = {looped / total:.0%}")
    print(f"零写入且被判不可信     : {suspicious}/{total} = {suspicious / total:.0%}")
    print(f"bad_args 总次数        : {sum(r['bad_args'] for r in rounds)}")
    with open(os.path.join(args.workdir, "direction_b_result.json"), "w",
              encoding="utf-8") as f:
        json.dump(rounds, f, indent=1, ensure_ascii=False)


# --------------------------------------------------------------------------
# a: does a forced text deliverable restore reasoning?
# --------------------------------------------------------------------------

def run_read_loop(pad_tokens, replay_reasoning=True, label="tool-loop",
                  max_steps=5, final_prompt=DEFAULT_FINAL, verbose=True):
    if verbose:
        print(f"\n=== {label}  pad~{pad_tokens}  replay_reasoning={replay_reasoning} ===")
        print(f"    final_prompt={final_prompt!r}")
    task = FINAL_TASK + final_prompt
    user_text = (filler(pad_tokens) + "\n\n" + task) if pad_tokens else task
    items = [{"type": "message", "role": "user",
              "content": [{"type": "input_text", "text": user_text}]}]
    tools = [TOOLS[0]]
    rows = []
    for step in range(1, max_steps + 1):
        res = call({"model": MODEL, "input": items, "tools": tools,
                    "tool_choice": "auto", "store": False, "stream": False})
        if "error" in res:
            if verbose:
                print(f"  step{step} {res['error']} {res.get('detail', '')[:200]}")
            rows.append((step, res))
            break
        if verbose:
            print(f"  step{step}  in={res['in']} cache={res['cached']} out={res['out']} "
                  f"reason={res['reason']} text_len={res['text_len']} "
                  f"calls={[c['name'] for c in res['calls']]} {res['elapsed']}s")
        rows.append((step, res))
        for r in res["reason_items"]:
            if not replay_reasoning:
                continue
            item = {"type": "reasoning", "id": r["id"], "summary": []}
            if r.get("encrypted_content"):
                item["encrypted_content"] = r["encrypted_content"]
            items.append(item)
        if res["text_len"]:
            items.append({"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": res["text"]}]})
        if not res["calls"]:
            if verbose:
                print("   -> no tool call, loop ended")
            break
        for c in res["calls"]:
            items.append({"type": "function_call", "call_id": c["call_id"],
                          "name": c["name"], "arguments": json.dumps(c["args"])})
            items.append({"type": "function_call_output", "call_id": c["call_id"],
                          "output": FILE_BODY.get(c["args"].get("path", ""),
                                                  "ERROR: not found")})
    return rows


def summarize(rows):
    ok = [(s, r) for s, r in rows if "error" not in r]
    final = ok[-1][1] if ok else {}
    return {
        "steps": len(rows),
        "inputs": [r.get("in") for _, r in ok],
        "outs": [r.get("out") for _, r in ok],
        "reasons": [r.get("reason") for _, r in ok],
        "final_out": final.get("out"),
        "final_reason": final.get("reason"),
        "final_text_len": final.get("text_len"),
        "final_text": final.get("text", ""),
        "calls": sum(len(r.get("calls") or []) for _, r in ok),
    }


def cmd_a(args):
    prompt = args.final_prompt or (
        "Summarise the four readings you collected and describe the distribution "
        "of replica counts across the services. State explicitly which service "
        "has the most replicas and which has the fewest.")
    results = {}
    for pad in args.pads:
        results[f"pad{pad}"] = summarize(run_read_loop(pad, True, "direction-A",
                                                       final_prompt=prompt))
    print("\n===== DIRECTION A =====")
    print(f"final_prompt: {prompt!r}\n")
    print(f"{'pad':>8} {'final_out':>10} {'final_reason':>13} {'text_len':>9}  verdict")
    for k, s in results.items():
        verdict = "REASONING RESTORED" if (s["final_reason"] or 0) > 0 else "still zero"
        print(f"{k:>8} {s['final_out']!s:>10} {s['final_reason']!s:>13} "
              f"{s['final_text_len']!s:>9}  {verdict}")
    restored = sum(1 for s in results.values() if (s["final_reason"] or 0) > 0)
    print(f"\nrestored {restored}/{len(results)}")
    with open(os.path.join(args.workdir, "direction_a_result.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, indent=1, ensure_ascii=False)


# --------------------------------------------------------------------------
# sweep: the original hypothesis, kept for its negative result
# --------------------------------------------------------------------------

def cmd_sweep(args):
    out = {}
    for pad in (0, 40000, 48000, 50000, 52000, 55000):
        out[f"pad{pad}_replay"] = run_read_loop(pad, True, "tool-loop")
    out["pad52000_noreplay"] = run_read_loop(52000, False, "tool-loop no reasoning replay")
    print("\n\n===== SUMMARY =====")
    for k, rows in out.items():
        print(k, [(s, r.get("in"), r.get("out"), r.get("reason")) for s, r in rows])
    with open(os.path.join(args.workdir, "sweep_result.json"), "w", encoding="utf-8") as f:
        json.dump({k: [[s, {kk: vv for kk, vv in r.items() if kk != "reason_items"}]
                       for s, r in rows] for k, rows in out.items()}, f, indent=1)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, bench=True):
        p.add_argument("--workdir", default=DEFAULT_WORKDIR)
        p.add_argument("--max-steps", type=int, default=30)
        p.add_argument("--max-nudges", type=int, default=1)
        if bench:
            p.add_argument("--bench", default=DEFAULT_BENCH)

    c = sub.add_parser("control", help="calibrate the counters; must print CALIBRATED : True")
    common(c, bench=False)
    c.set_defaults(func=cmd_control)

    b = sub.add_parser("b", help="N rounds of a real agent loop")
    common(b)
    b.add_argument("--rounds", type=int, default=8)
    b.add_argument("--bench-prompt", help="override the audit prompt")
    b.set_defaults(func=cmd_b)

    a = sub.add_parser("a", help="direction A: forced text deliverable")
    common(a, bench=False)
    a.add_argument("--final-prompt", help="closing instruction")
    a.add_argument("--pads", type=int, nargs="+", default=[0, 40000, 50000, 55000])
    a.set_defaults(func=cmd_a)

    s = sub.add_parser("sweep", help="6-bucket input-length sweep (negative result)")
    common(s, bench=False)
    s.set_defaults(func=cmd_sweep)

    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

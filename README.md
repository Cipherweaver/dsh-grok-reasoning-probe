# dsh-grok-reasoning-probe

A measurement harness for one specific failure: **an agent model that reads everything, writes nothing, and reports success.**

It was built while chasing a different hypothesis and kept because that hypothesis died. The negative result is part of the payload.

---

## 1. What the failure looks like

A turn that ends `status: completed`, with a coherent summary, correct file contents quoted back in the prose, `finish_reason: stop` — and **zero bytes changed on disk**.

Nothing upstream reports an error:

- proxy logs: `POST /v1/responses → 200` every time, one account, one route
- harness: `turn/end {"kind":"completed"}`, every tool result `isError=false`
- input tokens monotonically increasing, 90%+ cache hit — context was never lost

The model did not run out of context and did not get rate limited. It judged the step finished when the step was a *report*, not a *mutation*.

### Why reasoning_tokens is not the metric

The original hypothesis was "upstream strips reasoning above ~50K input tokens." `sweep` killed it: the 50K bucket carried the **most** reasoning of any bucket (final step 238 vs 221 / 193 / 0 / 0). And in a control run with a complete 562-character four-paragraph summary, `reasoning_tokens` was `0`. Reasoning presence and output quality are **decoupled**. Use it as a diagnostic, never as a health check.

### Why usage.output_tokens is not the metric

Across 8 measured rounds, `residual = output_tokens - text_len - reasoning_tokens` was **negative every time**, while `text_len` is a character count that cannot exceed a token count. Either the gateway double-counts replayed history or it omits tool-call arguments. Any conclusion drawn from small `output_tokens` values ("the model degraded to 3 tokens") is an artifact of that accounting, not a model behavior.

**Only disk is trustworthy.** Everything else in this repo is instrumentation around that one fact.

---

## 2. The counters

Three numbers must agree before a round counts as healthy:

| counter | meaning |
|---|---|
| `intent` | write/edit calls the model emitted, counted **by `call_id` presence** |
| `writes_ok` | those calls whose arguments parsed and whose handler ran |
| `disk_mutations` | files under the work tree whose bytes actually changed |

```
CALIBRATED : True   <=>   intent == writes_ok == disk_mutations and all > 0
```

This triple exists because each single counter was individually fooled during development:

- **`intent` alone** over-counts. One round produced `output_tokens=2171` with `text_len=60` — 2100 tokens of `arguments`, i.e. the model really did construct a write — but the argument JSON was malformed. The handler never ran. Counting intent said "1 write"; the disk said "0 changes".
- **`writes_ok` alone** is blind to writes that succeed and change nothing (identical content, wrong path resolved outside the work tree).
- **`disk_mutations` alone** cannot distinguish "the model declined to write" from "the harness failed to parse a write it was given."

---

## 3. Two failure modes, two guards

Both are implemented inside `run_agent_loop`, because both are cheap to catch there and expensive to discover downstream.

### 3.1 Zero-mutation completion

`status: completed` + `disk_mutations == 0` + task text containing an execution verb (`改/写/创建/替换/commit/deploy/...`).

The loop does **not** accept that as an ending. It appends one corrective turn:

```
你没有产生任何文件写入。任务要求修改，不是报告。
要么现在调用 write_file / edit_file 完成修改，要么明确说明为什么无法执行。
```

Then it allows exactly one retry (`--max-nudges`, default 1) and records `stop_reason: suspicious_no_mutation`. It does not retry indefinitely: this is a **stable** failure, not jitter. Twelve consecutive rounds produced twelve zero-write runs. Retrying burns quota to reproduce the same result.

### 3.2 Identical-call read loop

One round spent **30 steps on `list_dir`**, argument length cycling 14–38 characters, output lengths repeating 18/14/11/14/16/18/23/25 until `max_steps`. Zero writes, but a different mechanism: the model was not "done", it was spinning.

`run_agent_loop` keeps a signature window of `(name, json(arguments))`. Three identical consecutive calls trip `stop_reason: repeat_loop` and a nudge:

```
你已经用相同参数调用 list_dir 3 次。重复读取不会产生新信息。
请直接执行修改，或说明缺什么。
```

Raising `--max-steps` is not a fix; it only buys more spinning.

---

## 4. Layer 3: the prompt, which is the cheapest lever

This model is extremely sensitive to an **explicit failure condition**. Same task, same harness:

| prompt | rounds | rounds with ≥1 write |
|---|---|---|
| bundle's own errata text | 4 | **0** |
| self-written template | 8 | **0** |
| errata text + "必须调用 write_file ≥3 次，否则 FAILED" | 1 | **1** (3 writes, 3 disk mutations, step 5) |

`errata_prompt.txt` is the third variant. It is a stopgap, not a fix — but it is a one-line change with a measurable effect, so it goes in the prompt while the harness guard handles the cases where it is ignored.

---

## 5. Running it

```bash
export G2A_BASE=http://127.0.0.1:8000/v1/responses
export G2A_KEY=<your token>        # required; there is deliberately no default
export G2A_MODEL=grok-4.7

python reasoning_probe.py control            # must print CALIBRATED : True
python reasoning_probe.py b --rounds 8       # the measurement
python reasoning_probe.py b --bench-prompt "$(cat errata_prompt.txt)"
python reasoning_probe.py a --pads 0 40000 50000 55000
python reasoning_probe.py sweep              # the dead hypothesis, for the record
```

`control` exits `0` on calibrated, `2` on not. **Do not trust a `b` result from a run whose `control` failed** — you would be measuring the parser, not the model.

`--bench` defaults to `./bench`, `--workdir` to `./_runs`. Every round copies the bench tree fresh; nothing is carried between rounds.

### Offline self-test

No endpoint needed:

```bash
python _selftest.py
```

It spins up a mock `/v1/responses` with three personalities — writes 3 files / never writes / repeats one read forever — and asserts that `control` and `b` report the right thing for each:

```
[PASS] control/good -> CALIBRATED True (rc=0)
[PASS] control/lazy -> CALIBRATED False (rc=2)
[PASS] b/lazy -> suspicious_no_mutation, 1/1 zero-write
[PASS] b/loop -> repeat_loop tripped
[PASS] b/good -> intent==writes_ok==disk==3
ALL PASS
```

---

## 6. Notes that cost real time

**1. Never count on `type == "function_call"`.** Some gateways emit `custom_tool_call`, some stream `input` instead of `arguments`, some hide the tool name inside the argument blob. Filtering on the item type makes an entire class of calls vanish silently — a whole probe run once reported "0 calls" for this reason. `collect_calls` keys on `call_id` and records unparseable arguments as `bad_args=True` rather than dropping them.

**2. Calibrate before instrumenting.** The first version of these counters measured its own bug. Always run `control` on a prompt that *cannot* be refused; only then does a `b` number mean anything.

**3. `bad_args > 0` invalidates the round.** It means a write was attempted and lost in transport. Recorded, never silently folded into "the model didn't write."

**4. Bound every retry.** The failure is deterministic. One nudge, then `blocked` with a stated reason — never an unbounded loop.

**5. Read `stop_reason`, not just the counts.** `completed`, `suspicious_no_mutation`, `repeat_loop`, `http_error`, `max_steps` are five different things. A round that hit `max_steps` with 3 writes is not the same as one that finished cleanly with 3.

---

## 7. Repo contents and provenance

```
reasoning_probe.py     the harness (control / b / a / sweep)
errata_prompt.txt      direction-B prompt with the explicit completion criterion
bench/                 11-file bundle; each file carries an inline ERRATA instruction
_selftest.py           offline mock-endpoint test, no network or key required
results/               captured sweep output
```

**Provenance, stated plainly.** The zero-write rates quoted above (4/4, 8/8, 12/12, 0/11 vs 9+) were measured against a bench bundle on a different machine; that bundle is gone. `bench/` in this repo is a **reconstruction** with the same shape — 11 files, defects described in-place, a task that requires mutation to be correct — not the original tree. So the qualitative result (zero-write is stable and prompt-sensitive) transfers; the exact percentages do not. Re-run `control` then `b` on your own endpoint before quoting any number.

`results/sweep_summary.json` is real captured output from the input-length sweep, including the pad0 counter-example (complete summary, `reasoning_tokens = 0`).

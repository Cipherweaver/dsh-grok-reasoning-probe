# Delivery notes — instrument build from the other machine

This file exists because the operator asked for the instrument script fixed on one
machine to be made reachable from the other. The two machines have no shared
channel, so the transport is this repository.

## What is here

| path | what it is |
| --- | --- |
| `reasoning_probe.py` | the instrument (repo copy, 26677 bytes, `control`/`b`/`a`/`sweep`) |
| `bench/` | 11-file bench bundle used by `b` |
| `results/sweep_summary.json` | the only result file that was actually produced |

## What is *not* here, and why

`reasoning_probe.py` in this repo is **not** the 31279-byte build that produced
`CALIBRATED : True` and the 8/8 + 4/4 zero-write runs. That build lives on the
other machine under `D:\MyeProgram\Others\_tools\`.

Differences that matter:

- the 31279-byte build hardcodes a `G2A_KEY` default value. This repo's copy is
  already redacted (`os.environ.get("G2A_KEY", "")` + a `sys.exit` when unset).
  **Do not re-introduce a key into this repo.**
- the 31279-byte build records `final_text` per round and does the
  `intent` / `unknown` / `unparsed` split plus `unseen_out_types` bookkeeping.

## How to move the 31279-byte build across

It was split into three double-base64 parts. Reassemble on the target machine:

```bash
python - <<'PY'
import base64, pathlib
parts = [pathlib.Path(f"g{i}.m64").read_text().strip() for i in range(3)]
outer = "".join(base64.b64decode(p).decode("ascii") for p in parts)
data = base64.b64decode(outer)
pathlib.Path("reasoning_probe.py").write_bytes(data)
print(len(data))
PY
```

Expect `31230` bytes after redaction, `31279` before. Verify with:

```bash
sha256sum reasoning_probe.py
```

Expected for the redacted build:
`0b1d524a76c5fefca2a5e3c1dce8376f53d47b65e0e73116b1584670607cf8cc`

Never write a key into the delivered file; pass it at runtime:

```bash
export G2A_BASE=http://<host>:8000/v1/responses
export G2A_KEY=<token>
export G2A_MODEL=grok-4.7
python reasoning_probe.py control --workdir ./runs
python reasoning_probe.py b --rounds 4 --workdir ./runs
```

## Reading order

1. `control` must print `CALIBRATED : True` before any `b` number is quoted.
2. `b` reports `zero DISK mutation` — that is the load-bearing figure, not the
   `write` counter.
3. `reasoning_tokens` is not a health signal; a run can reason heavily and still
   make no edit.

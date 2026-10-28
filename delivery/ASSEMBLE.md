# Assembling `reasoning_probe.py` (the 31230-byte instrument build)

The script that actually produced `CALIBRATED : True` and the zero-write readings is
31230 bytes, `sha256 0b1d524a76c5fefca2a5e3c1dce8376f53d47b65e0e73116b1584670607cf8cc`.

It is stored here as base64 split into **21 files of 2000 characters** under
`delivery/parts/`, because that is the largest single line that survives every
transfer hop intact.

## Why not just commit the .py

The repository copy of `reasoning_probe.py` (root) is a 26677-byte build. It is a
working instrument — same `control` / `b` / `a` / `sweep` subcommands, same
calibration contract — but it is **not** byte-identical to the build that produced
the numbers in `README.md` §7. Treat the root copy as a reference implementation and
the assembled file below as the reproduction artifact.

## Rebuild (one command)

```sh
mkdir -p parts
for i in $(seq -w 0 20); do
  curl -fsSL \
    "https://raw.githubusercontent.com/Cipherweaver/dsh-grok-reasoning-probe/main/delivery/parts/p0${i}.txt" \
    -o "parts/p0${i}.txt"
done
cat parts/p0*.txt | tr -d '\r\n' | base64 -d > reasoning_probe.31230.py
sha256sum reasoning_probe.31230.py
# expect: 0b1d524a76c5fefca2a5e3c1dce8376f53d47b65e0e73116b1584670607cf8cc
```

On Windows PowerShell:

```powershell
$a = (0..20 | ForEach-Object { "parts\p{0:D3}.txt" -f $_ } |
      ForEach-Object { (Get-Content $_ -Raw) }) -join ""
$a = $a -replace "[\r\n]", ""
[IO.File]::WriteAllBytes("$PWD\reasoning_probe.31230.py",
  [Convert]::FromBase64String($a))
```

## Verify before trusting any reading

```sh
export G2A_BASE=http://<grok2api-host>:8000/v1/responses
export G2A_KEY=<token>
export G2A_MODEL=grok-4.7
python reasoning_probe.31230.py control
```

The last line must read `CALIBRATED : True`, which means
`intent == writes_ok == disk_mutations` and all three are non-zero. Until that
prints, no number from the `b` subcommand means anything.

## Reading order for the results

1. `control` prints `CALIBRATED : True` — the instrument can see writes.
2. `zero DISK mutation` is the load-bearing metric. **Not** `write calls`, not
   `output_tokens`, not `reasoning_tokens`.
3. `reasoning_tokens == 0` is not a health signal. It means the model judged that
   step to need no weighing. Steps with a full text deliverable can still report 0.

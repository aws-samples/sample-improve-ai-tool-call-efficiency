# sample-improve-ai-tool-call-efficiency

**A runtime fix for CLI version drift in coding agents.** A model's knowledge of a
command-line tool is frozen at training time. So it types `docker-compose up` long
after the command became `docker compose`, `kubectl run --generator=...` long after
that flag was removed, `aws s3 cp --acl` into a bucket that rejects ACLs. The
command fails, the agent reads the error, retries, and you pay for both turns.

You don't need to retrain the model to fix that. You can hand it the *current form
of the command* at inference time, right before it runs.

This repo is a pair of dependency-free
[Claude Code](https://docs.claude.com/en/docs/claude-code) hooks that do exactly
that:

| Hook | Fires | Job |
|------|-------|-----|
| `pre_tool_use.py` | before every Bash call | **Correct** — match the command against a *delta store* of known stale→current forms; surface a hint before it runs |
| `post_tool_use.py` | after every tool call | **Measure + learn** — record the call, flag likely drift, and auto-harvest new stale→correct pairs from your own sessions |

Everything is Python standard library. No packages, no network calls, no telemetry
leaving your machine — the hooks append JSONL to a local directory you control.

## Further reading

The approach here is written up in a two-part series, if you want the measurement
methodology and results behind it:

1. [When the Model's Memory Goes Stale: Measuring CLI Version Drift](https://builder.aws.com/content/3GDxWr6KOQBfkm4WQGl73V2XOc9/when-the-models-memory-goes-stale-measuring-cli-version-drift-or-tool-calling-efficiency-tce)
   — how often agents reach for outdated command syntax, and what the failed turns cost.
2. [Curing Version Drift with a Delta the Model Reads First](https://builder.aws.com/content/3H6hH3EQtdAqLlDpUO6fKrnYZ6N/curing-version-drift-with-a-delta-the-model-reads-first-or-tool-calling-efficiency-tce)
   — injecting the correction before the call, and how much of the gap it closes.

The `TCE_` environment-variable prefix throughout this repo comes from that series
(*Tool Calling Efficiency*).

## Install

Requirements: **Python 3** (standard library only — nothing to `pip install`) and
**Claude Code ≥ 2.x**.

```bash
git clone <this-repo> sample-improve-ai-tool-call-efficiency
cd sample-improve-ai-tool-call-efficiency
python3 install.py --inject      # wire hooks into ~/.claude/settings.json, hints ON
```

`install.py` writes absolute paths into your `~/.claude/settings.json` and is
idempotent (re-run to update paths, not duplicate them). Flags:

- `--inject` — also set `TCE_INJECT=1` so the PreToolUse hook actually *surfaces*
  corrections. Without it the hooks only **record** what happened, which is what you
  want if you're measuring your own drift rate before changing anything.
- `--settings PATH` — install into a project-local `.claude/settings.json` instead.
- `--print` — print the hooks block and change nothing.

### Manual install

Merge `examples/settings.example.json` into your settings, replacing `/ABS/PATH`
with the absolute path to this checkout. The schema:

```json
{
  "hooks": {
    "PreToolUse":  [{ "matcher": "Bash", "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/sample-improve-ai-tool-call-efficiency/hooks/pre_tool_use.py" }] }],
    "PostToolUse": [{ "matcher": "*",    "hooks": [{ "type": "command", "command": "python3 /ABS/PATH/sample-improve-ai-tool-call-efficiency/hooks/post_tool_use.py" }] }]
  }
}
```

- `PreToolUse` matches only `Bash` (that's where CLI drift lives); `PostToolUse`
  matches `*` (record everything).
- **Absolute paths are required** — Claude Code does not reliably resolve relative
  hook paths.
- Both hooks always `exit 0` — a hook must never break your session.
- The hint reaches the model via `hookSpecificOutput.additionalContext` — the only
  channel that works on modern Claude Code. The legacy `decision`/`reason` schema is
  a silent no-op on ≥2.x, so the hook emits **both**: the modern key for ≥2.x plus
  the legacy keys, harmlessly, for older versions.
- `install.py` tags the blocks it manages with a `_source` key so re-runs can find
  and update them; it is inert as far as Claude Code is concerned.

## How the correction works

On each Bash call, `pre_tool_use.py` checks the command against
`hooks/delta_store.json` — a set of regex patterns mapping known stale forms to the
current one. On a match, the agent gets a note *before* the command executes:

```
⚠️ this command may use STALE CLI syntax:
  • [docker-compose-v1-to-v2] docker-compose (v1, hyphen) -> `docker compose` (v2, subcommand). v1 is EOL.
    suggested: docker compose up -d
```

The agent reads it as ordinary context and corrects itself, instead of spending a
turn on a command that was never going to work. Nothing is blocked: if the match is
a false positive, the command still runs.

The shipped store has 33 deltas covering `kubectl`, `docker`, `aws`, `git`, `uv`,
`terraform`, `npm`/`corepack`, `cargo` and `node`. `delta_store.seed.json` is a
6-delta starter if you'd rather build your own from scratch.

### It learns from your sessions

`post_tool_use.py` watches for the pattern *command fails → near-identical command
succeeds* — same program-plus-subcommand fingerprint, computed so that
`docker-compose` and `docker compose` collapse to the same fingerprint (the
hyphen-to-subcommand split being a common drift shape in its own right).

Each such pair is appended to `drift_examples.jsonl` marked `unreviewed`:

```json
{"session_id": "s1", "stale_command": "docker-compose up", "working_command": "docker compose up", "fingerprint": "docker compose", "status": "unreviewed"}
```

Review them, and promote the real ones into `delta_store.json` as new patterns. The
store then grows from the drift you actually hit, on the tools you actually use.

## Configuration

Environment variables (read by `hooks/lib.py`):

| Var | Default | Purpose |
|-----|---------|---------|
| `TCE_INJECT` | `0` (off) | `1` enables the PreToolUse correction hints — the on/off switch |
| `TCE_CAPTURE_DIR` | `~/.claude/tokenomics-tce/` | where JSONL logs are written. Defaults outside any repo on purpose: the hooks fire for *all* your Claude Code sessions, not just this project |
| `TCE_DELTA_STORE` | `hooks/delta_store.json` | which store to match against; point it at your own edited copy |

## What gets written to `$TCE_CAPTURE_DIR`

| File | Contents |
|------|----------|
| `tool_calls.jsonl` | every call: tool, command, args, `exit_code`, `is_error`, and `class8_candidate` — true when a call both failed *and* showed a drift signal, i.e. a turn that was probably wasted on stale syntax |
| `drift_examples.jsonl` | auto-harvested `(stale → working)` command pairs |
| `interventions.jsonl` | which commands got a correction hint (when hints are on) |
| `pre_tool_use.jsonl` | pre-image of each Bash command, for pre/post pairing |
| `pending_fail_<session>.jsonl` | harvester scratch state: each failed Bash command plus its fingerprint, held per session until a later success pairs with it |

## Try it without installing (offline smoke test)

```bash
export TCE_CAPTURE_DIR=/tmp/tce_test
printf '%s' '{"session_id":"s1","tool_name":"Bash","tool_input":{"command":"docker-compose up"},"tool_response":{"stdout":"docker: compose is not a docker command.\n[exit 1]"}}' | python3 hooks/post_tool_use.py
printf '%s' '{"session_id":"s1","tool_name":"Bash","tool_input":{"command":"docker compose up"},"tool_response":{"stdout":"ok"}}' | python3 hooks/post_tool_use.py
cat /tmp/tce_test/tool_calls.jsonl /tmp/tce_test/drift_examples.jsonl
```

You should see the first call flagged `class8_candidate: true` — with
`stale_error_signature: true` and a `docker-compose-v1-to-v2` store match — the
second call clean, and a harvested `docker-compose → docker compose` pair.

To see the correction side instead, feed the same first payload to the other hook:

```bash
TCE_INJECT=1 TCE_CAPTURE_DIR=/tmp/tce_test printf '%s' \
  '{"session_id":"s1","tool_name":"Bash","tool_input":{"command":"docker-compose up -d"}}' \
  | TCE_INJECT=1 TCE_CAPTURE_DIR=/tmp/tce_test python3 hooks/pre_tool_use.py
```

> Use `printf '%s'`, not `echo` — zsh's builtin `echo` expands the `\n` inside the
> payload into a real newline, which makes it invalid JSON. The hooks swallow bad
> input by design (they must never break a session), so you'd get an empty record
> and no harvested pair instead of an error.

## Tests

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## Files

```
sample-improve-ai-tool-call-efficiency/
├── install.py                     # wire hooks into Claude Code settings.json
├── hooks/
│   ├── lib.py                     # delta matching + staleness detection (stdlib only)
│   ├── pre_tool_use.py            # surface corrections before the call
│   ├── post_tool_use.py           # record outcomes + self-harvest new deltas
│   ├── delta_store.json           # 33 shipped deltas (kubectl, docker, aws, git, ...)
│   └── delta_store.seed.json      # minimal 6-delta starter, to build your own
├── examples/settings.example.json
└── tests/
    ├── test_hooks_lib.py
    ├── test_pre_tool_use.py
    └── test_post_tool_use.py
```

## License

MIT-0 — see [LICENSE](LICENSE).

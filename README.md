# infinitLooper

A loop harness around the [Codex CLI](https://github.com/openai/codex). Give it a goal
once; it keeps working, evaluates its own result in a separate conversation, and
does not stop until the goal is met. It never asks for permission, and it is built
so that the model never needs a long context window.

```sh
cd my-project
codex-loop "Add a /health endpoint with tests; the whole test suite must pass."
```

> **Warning.** By default the agent runs with no approvals and no sandbox:
> every command it decides on runs with your full user rights, with network access,
> anywhere on disk, for as long as the loop runs. Use it in a throwaway machine,
> VM or container, or pass `--sandbox workspace-write` where Codex's sandbox works.

## How it works

| Step | Conversation | What it does |
| --- | --- | --- |
| once | planner | writes `CRITERIA.md`: 3 to 8 checkable completion criteria, each with the command that proves it |
| each iteration | worker | reads the goal, criteria, state and last verdict; does one chunk of work; rewrites `STATE.md` |
| each iteration | evaluator | runs every check itself, without trusting the worker, and returns a JSON verdict |

- The harness decides when it is over, not the model: the goal counts as met only
  when every criterion in the verdict passes.
- `GOAL.md` and `CRITERIA.md` are frozen. If an agent edits them, they are restored,
  so the bar cannot be lowered.
- After `--stuck-after` iterations without any file change, the worker is told to
  record the dead end and change approach.
- The loop ends on success, `--max-iters`, `--max-hours` or Ctrl+C. Exit code 0
  means the goal was met, 2 that a limit was reached. State is on disk, so running
  `codex-loop` with no goal resumes.

## Where the context lives

The agent's whole context is a set of files in a randomly named directory under
`/tmp`, created at the start and printed as `context: /tmp/codex-loop-xxxxxxxx`:
`GOAL.md`, `CRITERIA.md`, `STATE.md`, `EVAL.md`, `log.jsonl` (one row per conversation
with token usage, and every verdict) and `runs/` (the Codex event stream, stderr and
last message of each conversation). Nothing is written into the project except the
work itself.

`~/.codex-loop/sessions.json` remembers which directory belongs to which workspace,
so resuming needs no argument; `--state DIR` names one explicitly and `--tmp-dir`
changes the parent. `/tmp` is usually emptied on reboot: after that, start again
with the goal.

## Watching it work

On a terminal the harness shows a status line and the newest ten lines of what the
model is doing, redrawn in place (`--live-rows N` changes the number):

```
| worker #2  01:15  ~5165 tokens streamed  last activity 1s ago
[think] The parser rejects "2 3" but accepts "(2)(3)"; that needs a check for
$ /bin/bash -lc 'python3 -m unittest 2>&1 | tail -n 5'
  > exit 0: OK
```

The text is the model's own token stream (reasoning, answers, tool-call arguments)
plus the commands Codex runs and the last line of their output. Codex prints only
finished items, so the harness puts a small local pass-through in front of the model
server and reads the stream there. `--no-live` turns the view off; it is also off
when the output is not a terminal.

## Web UI

```sh
codex-loop --web          # http://127.0.0.1:8790/
```

**New task** (top left) starts a loop from the page: a goal, a working directory on the
machine (created if missing), optionally a limit on iterations. A running task has a
**Stop this task** button; a stopped task resumes with `codex-loop` in its directory,
or by starting it again from the page with the same goal. Tasks started from the page
write their terminal output to `~/.codex-loop/logs/`.

Starting, stopping and uploading need an access token, because whoever can start a
task can run commands as you. `codex-loop --web` prints a link that carries the token;
open it once and the browser remembers it. The token is kept in
`~/.codex-loop/web-token`; delete that file to get a new one. Reading needs no token.
`--web-open` drops the token: then anyone who can reach the page can start tasks. Use
it only on a network where you trust every machine.

A page with every task on the left and, for the selected one: the goal and status,
the latest progress (the same stream as the terminal view, refreshed every two
seconds), the latest evaluation per criterion, and the record of every conversation
with its duration and token counts. Click a conversation to see the commands it ran
and what it said.

Below that is the task's working directory: browse folders, preview text and images,
download any file, and upload files into the folder shown (button or drag and drop;
a file of the same name is replaced, up to 2 GB each; needs the token). The page cannot reach anything
outside the working directory, and it cannot delete or rename.

**Gallery** switches the folder to a grid of its pictures and videos, 48 to a page, with
first / previous / next / last. It can sort by name or newest first and can include
subfolders. Click a tile for the full picture or to play the video; the arrow keys move
to the neighbours and Esc closes. `/?task=NAME&dir=FOLDER&view=gallery` opens a gallery
directly.

Task records are kept in `~/.codex-loop/tasks/`, so the list survives a reboot; the
details of a task (progress, conversations, state) are read from its `/tmp` directory
and disappear with it. `--web-host 0.0.0.0` makes the page reachable from other
machines. Reading has no login: anyone who can reach it can read goals, logs, command
output and the working directories' files. `--web-port` changes the port.

## No long context

The design follows [You don't need long context](https://arxiv.org/abs/2611.05306)
(Belcak et al., 2026):

- **Fresh conversations.** Every planner, worker and evaluator run starts empty. It
  gets the task and a bounded state file, never an earlier transcript.
- **End-of-conversation compaction.** Inside a conversation Codex compacts at 75% of
  a deliberately small window (default 48,000 / 36,000 tokens), using a recap prompt
  with fixed sections: objective, established facts, applied changes, verification,
  dead ends, remaining plan. `STATE.md` uses the same sections.
- **Selective reading.** State lives in files under `/tmp`. The agent is told to read only the
  lines it needs, and a single tool result is capped at 2,500 tokens.

Not implemented: the paper's single-action mode (one command per conversation). A
worker conversation here does one chunk of work.

## Install

Requirements: Python 3.10+, the `codex` CLI, and a model server.

```sh
git clone https://github.com/shi3z/infinitLooper
ln -s "$PWD/infinitLooper/bin/codex-loop" ~/.local/bin/codex-loop
```

The default target is an OpenAI-compatible server with the Responses API at
`http://127.0.0.1:8080/v1`, which is what a local
[Strata](https://github.com/Niko1221/Strata) server provides. The harness writes its
own Codex configuration to `~/.codex-loop/homes/<context name>` and leaves
`~/.codex` alone, so several loops can run at once in different directories.

| Environment variable | Meaning |
| --- | --- |
| `CODEX_BIN` | the codex executable (default: `codex` on `PATH`) |
| `LOOP_URL` | the server address (default: `http://127.0.0.1:8080/v1`) |
| `LOOP_SERVER` | optional command that starts the server; run when nothing answers on `LOOP_URL` |

To use OpenAI's hosted models instead, run the script directly with Codex's built-in
provider: `python3 codex_loop.py --base-url '' --model <model> "goal"`.

## Usage

```sh
codex-loop "goal"                    # start in the current directory
codex-loop                           # resume
codex-loop --restart "another goal"  # discard the loop state and start over
codex-loop -C path/to/project --max-hours 8 --goal-file goal.md
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--context-window` | 48000 | context Codex may use per conversation |
| `--compact-at` | 75% of the window | token count at which Codex compacts |
| `--tool-output-tokens` | 2500 | cap on one tool result |
| `--sandbox` | `danger-full-access` | or `workspace-write`, `read-only` |
| `--iter-timeout` | 3600 | seconds one conversation may take |
| `--max-iters`, `--max-hours` | 0 | limits; 0 means run until the goal is met |
| `--stuck-after` | 3 | unchanged iterations before the change-approach notice |
| `--live-rows` | 10 | lines of the live view |
| `--no-live` | | no live view |
| `--web`, `--web-host`, `--web-port` | off, `127.0.0.1`, 8790 | serve the web UI instead of running a loop |
| `--web-open` | off | web UI without the access token |
| `--state`, `--tmp-dir` | remembered, `/tmp` | the context directory, and where a new one is created |

## Status

Tested with codex-cli 0.168.0 against a local Qwen3.8-Flash-Next (Strata, RTX A6000)
on small coding goals. In a test with a 13,000-token compaction threshold, no prompt
exceeded about 17,000 tokens. Long multi-iteration runs, the
fail-and-retry path and the stuck notice have had little exercise so far.

## License

[Apache License 2.0](LICENSE).

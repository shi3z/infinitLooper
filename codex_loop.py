#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""codex-loop: run Codex until an independently evaluated goal is met.

A driver around `codex exec`. It never asks the user anything: it plans completion
criteria once, then alternates a worker conversation and a separate evaluator
conversation until the evaluator finds every criterion satisfied.

It does not rely on a long context window (after "You don't need long context",
arXiv 2611.05306):
  * every worker / evaluator run is a fresh conversation that gets only the task
    and a bounded state file, never the previous transcript;
  * inside one conversation Codex compacts at a low threshold (end-of-conversation
    compaction) with a recap prompt of fixed sections;
  * durable state lives in files under .codex-loop/, and the agent is told to read
    files selectively; tool output is capped.

Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = ".codex-loop"
STATE_SECTIONS = [
    "Objective",
    "Established facts",
    "Applied changes",
    "Verification",
    "Dead ends",
    "Remaining plan",
]

COMPACT_PROMPT = """\
You are about to lose this conversation. Write the handoff that a fresh copy of you
needs to continue the same task with no other memory. Do not narrate what happened;
select only what the remaining work needs. Be exact: file paths, commands, values,
error messages. Keep it under 900 words. Use exactly these sections:

## Objective
The task, close to verbatim, and the completion criteria that are still open.
## Established facts
What you verified about the code and environment, with paths and exact values.
## Applied changes
Files you changed and what each change does.
## Verification
Commands you ran and their real results (pass / fail, key output).
## Dead ends
What you tried that did not work, and why, so it is not tried again.
## Remaining plan
Ordered next steps, the first one concrete enough to start immediately.
"""

COMMON_RULES = """\
Rules for context use (the context window is small on purpose):
- Never print a whole large file or log. Look first (`wc -l`, `rg -n`, `ls`), then
  read only the lines you need (`sed -n 'A,Bp'`, `head`, `tail`, `rg -n -C3`).
- Pipe long command output through `tail -n 40` or `rg` for the lines that matter.
- Anything you will need later goes into a file, not into your memory of this chat.
- Nobody is available to answer questions or approve anything. Never ask; decide,
  note the assumption in the state file, and continue.
"""

PLANNER_PROMPT = """\
You are the planner of an autonomous loop. Read the goal in `{sd}/GOAL.md` and look
at the working directory just enough to understand what exists.

Write `{sd}/CRITERIA.md`: the completion criteria for the goal. Requirements:
- 3 to 8 numbered criteria, each one line starting with `C1:`, `C2:`, ...
- each criterion is objectively checkable and is followed by an indented `check:`
  line giving the exact command or inspection that proves it
- together they cover the whole goal and nothing beyond it (no extra scope)
- a criterion must fail today if the work is not done yet

Do not start the work itself. Do not create any other file.

{rules}"""

WORKER_PROMPT = """\
You are the worker of an autonomous loop, iteration {n}. You have no memory of
earlier iterations; everything known is in these files:
- `{sd}/GOAL.md`      the goal, verbatim (read it first; never edit)
- `{sd}/CRITERIA.md`  the completion criteria (never edit)
- `{sd}/STATE.md`     the handoff written by the previous iteration
- `{sd}/EVAL.md`      the independent evaluator's latest verdict and what is missing
{stuck}
Do the next chunk of real work toward the unmet criteria: the first item of the
remaining plan, or what the evaluator says is missing. Verify what you did by
running it. Stop after one coherent chunk rather than trying to finish everything.

Before you end, overwrite `{sd}/STATE.md` (at most ~900 words) with exactly these
sections, selecting what the next iteration needs rather than narrating:
{sections}
Do not claim a criterion is met unless you ran its check in this iteration and saw
it pass. Your final message: two or three lines on what changed.

{rules}"""

EVALUATOR_PROMPT = """\
You are the independent evaluator of an autonomous loop. You did not do the work
and must not trust the worker's claims. Read `{sd}/GOAL.md` and `{sd}/CRITERIA.md`.
`{sd}/STATE.md` is the worker's own account: use it only to find things to check.

For every criterion, run its check yourself (and anything else needed to be sure)
and judge from what you observe now. A criterion passes only on direct evidence
from this conversation. If a check cannot be run, it fails. Also fail a criterion
that is met only by weakening it (deleted tests, hard-coded outputs, stubs).

Do not modify, create or delete any project file. Do not fix anything.

Your final message must be only a JSON object of this shape:
{{"criteria": [{{"id": "C1", "pass": true, "evidence": "command and what it showed"}}],
  "done": false,
  "feedback": "for the worker: what is missing or wrong, most important first"}}
`done` is true only if every criterion passes and the goal in GOAL.md is fully met.

{rules}"""

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criteria", "done", "feedback"],
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "pass", "evidence"],
                "properties": {
                    "id": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "done": {"type": "boolean"},
        "feedback": {"type": "string"},
    },
}


def log(msg: str) -> None:
    print(f"[codex-loop {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def write_codex_home(args: argparse.Namespace) -> Path:
    """A private CODEX_HOME so the user's own ~/.codex is never read or changed."""
    home = Path(args.codex_home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    cfg = [
        f"model = {toml_str(args.model)}",
        f"model_context_window = {args.context_window}",
        f"model_auto_compact_token_limit = {args.compact_at}",
        f"tool_output_token_limit = {args.tool_output_tokens}",
        f"compact_prompt = {toml_str(COMPACT_PROMPT)}",
        'approval_policy = "never"',
        "check_for_update_on_startup = false",
    ]
    if args.base_url:
        cfg += [
            'model_provider = "loop"',
            "",
            "[model_providers.loop]",
            'name = "codex-loop provider"',
            f"base_url = {toml_str(args.base_url)}",
            f"wire_api = {toml_str(args.wire_api)}",
            f"env_key = {toml_str(args.api_key_env)}",
            "stream_idle_timeout_ms = 600000",
        ]
    (home / "config.toml").write_text("\n".join(cfg) + "\n")
    return home


def extract_json(text: str) -> dict | None:
    """The last JSON object in text (models sometimes wrap it in prose or fences)."""
    dec = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text[i:])
            if isinstance(obj, dict) and "done" in obj:
                found = obj
            i = text.find("{", i + max(end, 1))
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
    return found


class Loop:
    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.ws = Path(args.workspace).resolve()
        self.sd = self.ws / STATE_DIR
        self.runs = self.sd / "runs"
        self.env = dict(os.environ)
        self.totals = {"conversations": 0, "input_tokens": 0, "output_tokens": 0}
        self.child: subprocess.Popen | None = None

    # ---- one fresh Codex conversation -------------------------------------
    def codex(self, role: str, n: int, prompt: str, sandbox: str, schema: Path | None = None) -> str:
        tag = f"{n:04d}-{role}"
        last = self.runs / f"{tag}.last.txt"
        events = self.runs / f"{tag}.jsonl"
        last.unlink(missing_ok=True)
        cmd = [self.a.codex_bin, "exec", "--skip-git-repo-check", "--ephemeral", "--json",
               "--color", "never", "-C", str(self.ws), "-o", str(last)]
        if sandbox == "danger-full-access":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd += ["--sandbox", sandbox]
        if schema is not None:
            cmd += ["--output-schema", str(schema)]
        cmd.append(prompt)
        t0 = time.time()
        usage = {}
        try:
            with open(events, "w") as ev, open(self.runs / f"{tag}.stderr.txt", "w") as err:
                self.child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=ev, stderr=err,
                                              env=self.env, start_new_session=True)
                try:
                    rc = self.child.wait(timeout=self.a.iter_timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(self.child.pid, signal.SIGKILL)
                    self.child.wait()
                    rc = "timeout"
                finally:
                    self.child = None
        except OSError as e:
            log(f"{role}: could not start codex: {e}")
            return ""
        for line in events.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "turn.completed":
                usage = e.get("usage") or {}
        self.totals["conversations"] += 1
        self.totals["input_tokens"] += int(usage.get("input_tokens", 0))
        self.totals["output_tokens"] += int(usage.get("output_tokens", 0))
        self.record({"iteration": n, "role": role, "rc": rc, "seconds": round(time.time() - t0, 1),
                     "usage": usage})
        log(f"{role}: rc={rc}, {time.time() - t0:.0f}s, in={usage.get('input_tokens', '?')} "
            f"out={usage.get('output_tokens', '?')} tokens")
        return last.read_text(errors="replace") if last.exists() else ""

    def record(self, row: dict) -> None:
        row["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(self.sd / "log.jsonl", "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- protected files ----------------------------------------------------
    def protect(self) -> None:
        """GOAL.md and CRITERIA.md are fixed; undo any edit so the bar cannot be lowered."""
        for name in ("GOAL.md", "CRITERIA.md"):
            keep = self.sd / "frozen" / name
            cur = self.sd / name
            if keep.exists() and sha(keep) != sha(cur):
                shutil.copyfile(keep, cur)
                log(f"{name} was modified by the agent: restored")

    def workspace_fingerprint(self) -> str:
        """Changes outside .codex-loop/, to notice an evaluator that edits or a stuck worker."""
        h = hashlib.sha256()
        for root, dirs, files in os.walk(self.ws):
            dirs[:] = sorted(d for d in dirs if d not in (STATE_DIR, ".git", "node_modules", ".venv",
                                                         "__pycache__", "target"))
            for f in sorted(files):
                p = Path(root) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                h.update(f"{p}|{st.st_size}|{st.st_mtime_ns}\n".encode())
        return h.hexdigest()

    # ---- phases -------------------------------------------------------------
    def setup(self) -> None:
        self.runs.mkdir(parents=True, exist_ok=True)
        (self.sd / "frozen").mkdir(exist_ok=True)
        goal = self.sd / "GOAL.md"
        if self.a.goal is not None:
            if goal.exists() and goal.read_text().strip() != self.a.goal.strip() and not self.a.restart:
                sys.exit(f"{goal} holds a different goal. Use --restart to replace it, or omit the goal to resume.")
            if self.a.restart and self.sd.exists():
                shutil.rmtree(self.sd)
                self.runs.mkdir(parents=True)
                (self.sd / "frozen").mkdir()
            goal.write_text(self.a.goal.strip() + "\n")
        elif not goal.exists():
            sys.exit("No goal given and nothing to resume in this workspace.")
        shutil.copyfile(goal, self.sd / "frozen" / "GOAL.md")
        for name, text in (("STATE.md", "(first iteration: nothing has been done yet)\n"),
                           ("EVAL.md", "(no evaluation yet)\n")):
            if not (self.sd / name).exists():
                (self.sd / name).write_text(text)
        (self.sd / "verdict.schema.json").write_text(json.dumps(VERDICT_SCHEMA))

    def plan(self) -> None:
        crit = self.sd / "CRITERIA.md"
        frozen = self.sd / "frozen" / "CRITERIA.md"
        if frozen.exists():
            return
        for attempt in range(1, 4):
            log(f"planning completion criteria (attempt {attempt})")
            self.codex("planner", 0, PLANNER_PROMPT.format(sd=STATE_DIR, rules=COMMON_RULES), self.a.sandbox)
            if crit.exists() and "C1" in crit.read_text(errors="replace"):
                shutil.copyfile(crit, frozen)
                log("criteria:\n" + crit.read_text(errors="replace").rstrip())
                return
        # The loop must still be able to run: fall back to the goal itself as the only criterion.
        crit.write_text("C1: the goal in GOAL.md is fully achieved\n    check: inspect and run whatever proves it\n")
        shutil.copyfile(crit, frozen)
        log("planner produced no criteria; using the goal itself as the single criterion")

    def evaluate(self, n: int) -> dict:
        before = self.workspace_fingerprint()
        prompt = EVALUATOR_PROMPT.format(sd=STATE_DIR, rules=COMMON_RULES)
        text = self.codex("evaluator", n, prompt, self.a.sandbox, self.sd / "verdict.schema.json")
        verdict = extract_json(text)
        if verdict is None:
            log("evaluator: no verdict in the answer, asking once more without the schema")
            text = self.codex("evaluator-retry", n, prompt, self.a.sandbox)
            verdict = extract_json(text)
        if self.workspace_fingerprint() != before:
            log("warning: project files changed during the evaluation")
        if verdict is None:
            verdict = {"criteria": [], "done": False,
                       "feedback": "The evaluator gave no usable verdict. Re-run every check in CRITERIA.md "
                                   "yourself and record the exact results in STATE.md."}
        crits = verdict.get("criteria") or []
        # The harness, not the model, decides: done needs every criterion passing.
        all_pass = bool(crits) and all(c.get("pass") is True for c in crits)
        verdict["done"] = bool(verdict.get("done")) and all_pass
        lines = [f"# Evaluation after iteration {n}", "",
                 f"done: {str(verdict['done']).lower()}", ""]
        for c in crits:
            lines.append(f"- {c.get('id')}: {'PASS' if c.get('pass') else 'FAIL'} - {c.get('evidence', '')}")
        lines += ["", "## Feedback", str(verdict.get("feedback", "")).strip(), ""]
        (self.sd / "EVAL.md").write_text("\n".join(lines))
        passed = sum(1 for c in crits if c.get("pass"))
        log(f"evaluation: {passed}/{len(crits)} criteria pass, done={verdict['done']}")
        self.record({"iteration": n, "role": "verdict", "verdict": verdict})
        return verdict

    def run(self) -> int:
        self.setup()
        self.plan()
        done_iters = sum(1 for p in self.runs.glob("*-worker.jsonl"))
        n = done_iters
        started = time.time()
        stale = 0
        failures = 0
        while True:
            n += 1
            if self.a.max_iters and n - done_iters > self.a.max_iters:
                log(f"stopped: --max-iters {self.a.max_iters} reached, goal not met")
                return 2
            if self.a.max_hours and time.time() - started > self.a.max_hours * 3600:
                log(f"stopped: --max-hours {self.a.max_hours} reached, goal not met")
                return 2
            log(f"=== iteration {n} ===")
            fp, st = self.workspace_fingerprint(), sha(self.sd / "STATE.md")
            stuck = ""
            if stale >= self.a.stuck_after:
                stuck = ("\nNOTICE: the last %d iterations changed nothing. The current plan is not working. "
                         "Record it under Dead ends and take a fundamentally different approach.\n" % stale)
            sections = "".join(f"## {s}\n" for s in STATE_SECTIONS)
            answer = self.codex("worker", n, WORKER_PROMPT.format(n=n, sd=STATE_DIR, stuck=stuck,
                                                                sections=sections, rules=COMMON_RULES),
                                self.a.sandbox)
            self.protect()
            if sha(self.sd / "STATE.md") == st:
                # The worker died or forgot: keep what it said so the next one is not blind.
                with open(self.sd / "STATE.md", "a") as f:
                    f.write(f"\n## Note from the harness (iteration {n})\nThe worker did not update this file. "
                            f"Its last message was:\n{answer.strip()[:2000] or '(none: it crashed or timed out)'}\n")
            changed = self.workspace_fingerprint() != fp
            stale = 0 if changed else stale + 1
            if not answer and not changed:
                failures += 1
                wait = min(300, 10 * 2 ** min(failures, 5))
                log(f"worker produced nothing ({failures} in a row); waiting {wait}s before retrying")
                time.sleep(wait)
                continue
            failures = 0
            verdict = self.evaluate(n)
            self.protect()
            if verdict["done"]:
                t = self.totals
                log(f"GOAL MET after {n} iteration(s), {t['conversations']} conversations, "
                    f"{t['input_tokens']} input / {t['output_tokens']} output tokens this run")
                return 0

    def stop(self, *_: object) -> None:
        if self.child is not None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except OSError:
                pass
        log("interrupted; state is on disk, run again without a goal to resume")
        sys.exit(130)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("goal", nargs="?", help="the goal; omit to resume the loop in this workspace")
    p.add_argument("--goal-file", help="read the goal from this file")
    p.add_argument("-C", "--workspace", default=".", help="directory the agent works in (default: .)")
    p.add_argument("--restart", action="store_true", help="discard this workspace's loop state and start over")
    p.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    p.add_argument("--codex-home", default="~/.codex-loop/home",
                   help="private CODEX_HOME written by the harness (default: %(default)s)")
    p.add_argument("--model", default="strata")
    p.add_argument("--base-url", default="http://127.0.0.1:8080/v1",
                   help="OpenAI-compatible server; pass '' to use Codex's built-in OpenAI provider")
    p.add_argument("--wire-api", default="responses")
    p.add_argument("--api-key-env", default="CODEX_LOOP_API_KEY",
                   help="environment variable holding the API key (set to 'local' if unset)")
    p.add_argument("--context-window", type=int, default=48000, help="context Codex may use per conversation")
    p.add_argument("--compact-at", type=int, default=0, help="compact at this many tokens (default: 75%% of the window)")
    p.add_argument("--tool-output-tokens", type=int, default=2500, help="cap on one tool result")
    p.add_argument("--sandbox", default="danger-full-access",
                   choices=["read-only", "workspace-write", "danger-full-access"],
                   help="default: no sandbox, commands run with your full user rights")
    p.add_argument("--iter-timeout", type=int, default=3600, help="seconds one conversation may take")
    p.add_argument("--max-iters", type=int, default=0, help="stop after this many iterations (0 = until the goal is met)")
    p.add_argument("--max-hours", type=float, default=0, help="stop after this long (0 = until the goal is met)")
    p.add_argument("--stuck-after", type=int, default=3,
                   help="iterations without any file change before the worker is told to change approach")
    a = p.parse_args()
    if a.goal_file:
        a.goal = Path(a.goal_file).read_text()
    if not a.compact_at:
        a.compact_at = a.context_window * 3 // 4
    if shutil.which(a.codex_bin) is None:
        sys.exit(f"codex binary not found: {a.codex_bin} (use --codex-bin or CODEX_BIN)")
    loop = Loop(a)
    loop.env["CODEX_HOME"] = str(write_codex_home(a))
    loop.env.setdefault(a.api_key_env, "local")
    signal.signal(signal.SIGINT, loop.stop)
    signal.signal(signal.SIGTERM, loop.stop)
    return loop.run()


if __name__ == "__main__":
    sys.exit(main())

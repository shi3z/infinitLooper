# SPDX-License-Identifier: Apache-2.0
"""Web UI for codex-loop: every task's record, and what the running one is doing right now.

Read-only. It serves what the loop already writes: the task records in
~/.codex-loop/tasks/ and, while it still exists, each task's state directory under /tmp.
Standard library only. Start it with `codex_loop.py --web`.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_TEXT = 40_000


def read(path: Path, limit: int = MAX_TEXT) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit] + "\n... (cut)"


def alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)  # type: ignore[arg-type]
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_task(path: Path) -> dict | None:
    try:
        t = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(t, dict) or not NAME.match(str(t.get("name", ""))):
        return None
    # A loop that was killed could not write its own ending: the process tells the truth.
    if t.get("status") == "running" and not alive(t.get("pid")):
        t["status"], t["ended"] = "stopped", "process ended"
    t["has_state"] = Path(str(t.get("state", ""))).is_dir()
    return t


def tasks(root: Path) -> list[dict]:
    out = [t for t in map(load_task, root.glob("*.json")) if t]
    return sorted(out, key=lambda t: t.get("updated", 0), reverse=True)


def detail(root: Path, name: str) -> dict | None:
    t = load_task(root / f"{name}.json")
    if t is None:
        return None
    sd = Path(str(t.get("state", "")))
    rows = []
    for line in read(sd / "log.jsonl", 2_000_000).splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    try:
        live = json.loads((sd / "live.json").read_text())
    except (OSError, ValueError):
        live = None
    t.update(criteria_text=read(sd / "CRITERIA.md"), state_text=read(sd / "STATE.md"),
             eval_text=read(sd / "EVAL.md"), log=rows, live=live, now=time.time())
    return t


def run(root: Path, name: str, tag: str) -> dict | None:
    t = load_task(root / f"{name}.json")
    if t is None or not NAME.match(tag):
        return None
    runs = Path(str(t.get("state", ""))) / "runs"
    steps = []
    for line in read(runs / f"{tag}.jsonl", 4_000_000).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        it = e.get("item") or {}
        if e.get("type") != "item.completed":
            continue
        if it.get("type") == "command_execution":
            steps.append({"kind": "cmd", "text": str(it.get("command", ""))[:2000], "exit": it.get("exit_code"),
                          "output": str(it.get("aggregated_output", ""))[-1500:]})
        elif it.get("type") in ("agent_message", "reasoning"):
            steps.append({"kind": "say", "text": str(it.get("text", ""))[:6000]})
        elif it.get("type") == "file_change":
            steps.append({"kind": "edit", "text": ", ".join(str(c.get("path", "")) for c in it.get("changes") or [])})
    return {"tag": tag, "steps": steps, "last": read(runs / f"{tag}.last.txt")}


class Handler(http.server.BaseHTTPRequestHandler):
    root: Path

    def log_message(self, *_: object) -> None:
        pass

    def send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        name = q.get("name", "")
        data: object = None
        if url.path == "/":
            # The newest task is drawn with the page itself, so there is no empty first paint.
            ts = tasks(self.root)
            boot = {"tasks": ts, "task": detail(self.root, ts[0]["name"]) if ts else None}
            blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
            return self.send(PAGE.replace("/*BOOT*/null", blob).encode(), "text/html; charset=utf-8")
        if url.path == "/api/tasks":
            data = tasks(self.root)
        elif url.path == "/api/task" and NAME.match(name):
            data = detail(self.root, name)
        elif url.path == "/api/run" and NAME.match(name):
            data = run(self.root, name, q.get("tag", ""))
        if data is None:
            return self.send(b'{"error": "not found"}', "application/json", 404)
        self.send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")


def serve(root: Path, host: str, port: int) -> int:
    Handler.root = root
    try:
        srv = http.server.ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"codex-loop web UI: cannot listen on {host}:{port} ({e.strerror}). Choose another with --web-port.")
        return 1
    srv.daemon_threads = True
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"codex-loop web UI: http://{shown}:{port}/   (Ctrl+C to stop)", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("It has no login: anyone who can reach this address can read the goals, logs and command output.",
              flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>codex-loop</title>
<style>
:root { --bg:#f6f7f9; --panel:#fff; --line:#dde1e7; --text:#1b1f24; --dim:#667085; --accent:#2f6fed;
        --ok:#1a7f4b; --bad:#c0392b; --run:#b7791f; --code:#f0f2f5; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171c; --panel:#1c2027; --line:#2c323c; --text:#e6e8eb;
        --dim:#98a2b3; --accent:#7aa5ff; --ok:#4cc38a; --bad:#ff7b72; --run:#e3b341; --code:#12151a; } }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.5 system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
       background:var(--bg); color:var(--text); }
.app { display:grid; grid-template-columns:320px 1fr; height:100vh; }
aside { border-right:1px solid var(--line); overflow:auto; background:var(--panel); }
aside h1 { font-size:15px; margin:0; padding:14px 16px; border-bottom:1px solid var(--line); }
.task { padding:10px 16px; border-bottom:1px solid var(--line); cursor:pointer; }
.task:hover, .task.sel { background:var(--bg); }
.task .g { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.meta { color:var(--dim); font-size:12px; overflow-wrap:anywhere; }
main { overflow:auto; padding:20px 24px 60px; }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600;
         border:1px solid currentColor; margin-right:6px; }
.running { color:var(--run); } .done { color:var(--ok); } .stopped { color:var(--dim); } .fail { color:var(--bad); }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); margin:26px 0 8px; }
.goal { font-size:16px; white-space:pre-wrap; overflow-wrap:anywhere; margin:8px 0 6px; }
.facts { display:flex; flex-wrap:wrap; gap:6px 22px; color:var(--dim); font-size:13px; }
.facts b { color:var(--text); font-weight:600; }
pre, .live { background:var(--code); border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin:0;
       font:12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space:pre-wrap;
       overflow-wrap:anywhere; }
.live { min-height:9em; max-height:28em; overflow:auto; }
.crit { display:grid; grid-template-columns:auto 1fr; gap:4px 10px; }
.crit .ev { color:var(--dim); font-size:13px; overflow-wrap:anywhere; }
.tablewrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:5px 12px 5px 0; border-bottom:1px solid var(--line); white-space:nowrap; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
th { color:var(--dim); font-weight:500; font-size:12px; }
tr.row { cursor:pointer; } tr.row:hover td { background:var(--panel); }
details { margin:8px 0; } summary { cursor:pointer; color:var(--accent); }
.step { margin:6px 0; } .step .c { color:var(--accent); }
.empty { color:var(--dim); padding:40px 0; }
@media (max-width: 760px) { .app { grid-template-columns:1fr; height:auto; } aside { max-height:40vh; } }
</style></head><body>
<div class="app">
  <aside><h1>codex-loop tasks</h1><div id="list"></div></aside>
  <main id="main"><div class="empty">No task selected.</div></main>
</div>
<script>
const $ = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; return e; };
const ago = s => { s = Math.max(0, Math.round(s)); if (s < 90) return s + " s"; if (s < 5400) return Math.round(s/60) + " min";
  if (s < 129600) return Math.round(s/3600) + " h"; return Math.round(s/86400) + " d"; };
const num = n => (n || 0).toLocaleString("en-US");
const when = t => new Date(t * 1000).toLocaleString();
let selected = location.hash.slice(1), openRun = "", runCache = {};

async function get(url) { const r = await fetch(url); return r.ok ? r.json() : null; }

function statusBadge(t) { return $("span", "badge " + t.status, t.status === "done" ? "goal met" : t.status); }

async function refreshList() { renderList(await get("/api/tasks") || []); }
function renderList(tasks) {
  const list = document.getElementById("list"); list.replaceChildren();
  if (!tasks.length) list.append($("div", "task meta", "No tasks yet. Start one with: codex-loop \"goal\""));
  if (!selected && tasks.length) { selected = tasks[0].name; refreshDetail(); }
  for (const t of tasks) {
    const d = $("div", "task" + (t.name === selected ? " sel" : ""));
    d.append(statusBadge(t), $("span", "meta", "updated " + ago(Date.now()/1000 - t.updated) + " ago"));
    d.append($("div", "g", t.goal || "(no goal)"), $("div", "meta", t.workspace));
    d.onclick = () => { selected = t.name; location.hash = t.name; openRun = ""; refreshList(); refreshDetail(); };
    list.append(d);
  }
}

function section(title) { return $("h2", "", title); }

async function showRun(name, tag, box) {
  const key = name + "/" + tag;
  const r = runCache[key] || await get(`/api/run?name=${name}&tag=${tag}`);
  box.replaceChildren();
  if (!r) { box.append($("div", "meta", "The run's files are gone.")); return; }
  if (r.last) runCache[key] = r;   // finished runs do not change
  for (const s of r.steps) {
    const d = $("div", "step");
    if (s.kind === "cmd") { d.append($("div", "c", "$ " + s.text + (s.exit === null ? "" : "   → exit " + s.exit)));
      if (s.output) d.append($("pre", "", s.output)); }
    else if (s.kind === "edit") d.append($("div", "c", "edit " + s.text));
    else d.append($("div", "", s.text));
    box.append(d);
  }
  if (!r.steps.length) box.append($("div", "meta", "Nothing recorded yet."));
}

async function refreshDetail() { if (selected) renderDetail(await get("/api/task?name=" + selected)); }
function renderDetail(t) {
  const main = document.getElementById("main");
  if (!t) { main.replaceChildren($("div", "empty", "This task's record is gone.")); return; }
  const liveBox = main.querySelector(".live");
  const keepScroll = main.scrollTop, stick = !liveBox || liveBox.scrollTop + liveBox.clientHeight >= liveBox.scrollHeight - 8;
  const out = [];
  const head = $("div"); head.append(statusBadge(t));
  if (t.ended) head.append($("span", "meta", t.ended));
  out.push(head, $("div", "goal", t.goal || ""));
  const f = $("div", "facts");
  const fact = (k, v) => { const s = $("span"); s.append(k + " ", $("b", "", v)); f.append(s); };
  fact("workspace", t.workspace); fact("started", when(t.started)); fact("updated", ago(t.now - t.updated) + " ago");
  fact("iteration", String(t.iteration ?? 0));
  if (t.criteria) fact("criteria passing", `${t.passed} / ${t.criteria}`);
  fact("tokens in / out", num(t.input_tokens) + " / " + num(t.output_tokens));
  out.push(f);

  out.push(section("Latest progress"));
  const lv = t.live;
  if (lv && lv.lines && lv.lines.length) {
    const st = lv.active && t.status === "running"
      ? `${lv.title} · running ${ago(t.now - lv.started)} · ~${num(lv.chunks)} tokens streamed · last activity ${ago(t.now - lv.last)} ago`
      : `${lv.title} · finished ${ago(t.now - lv.time)} ago`;
    out.push($("div", "meta", st), $("div", "live", lv.lines.join("\n")));
  } else out.push($("div", "meta", t.has_state ? "Nothing streamed yet." : "The state directory under /tmp is gone; only this record is left."));

  const verdicts = t.log.filter(r => r.role === "verdict");
  if (verdicts.length) {
    const v = verdicts[verdicts.length - 1];
    out.push(section(`Latest evaluation (iteration ${v.iteration})`));
    const c = $("div", "crit");
    for (const x of v.verdict.criteria || [])
      c.append($("span", "badge " + (x.pass ? "done" : "fail"), x.id + (x.pass ? " pass" : " fail")), $("span", "ev", x.evidence || ""));
    out.push(c);
    if (v.verdict.feedback) { const d = $("details"); d.append($("summary", "", "Feedback to the worker"), $("pre", "", v.verdict.feedback)); out.push(d); }
  }

  const runs = t.log.filter(r => r.role !== "verdict");
  if (runs.length) {
    out.push(section("Record"));
    const wrap = $("div", "tablewrap"), tb = $("table");
    const hr = $("tr"); for (const [h, n] of [["time",0],["iteration",1],["conversation",0],["result",0],["seconds",1],["tokens in",1],["tokens out",1]])
      hr.append($("th", n ? "num" : "", h)); tb.append(hr);
    for (const r of runs.slice().reverse()) {
      const tag = String(r.iteration).padStart(4, "0") + "-" + r.role;
      const tr = $("tr", "row");
      const v = verdicts.find(x => x.iteration === r.iteration && r.role.startsWith("evaluator"));
      const res = r.rc !== 0 ? "rc " + r.rc : v ? `${(v.verdict.criteria||[]).filter(c => c.pass).length}/${(v.verdict.criteria||[]).length} pass` : "ok";
      tr.append($("td", "", (r.time || "").replace("T", " ")), $("td", "num", String(r.iteration)), $("td", "", r.role), $("td", r.rc !== 0 ? "fail" : "", res),
        $("td", "num", String(Math.round(r.seconds))), $("td", "num", num((r.usage||{}).input_tokens)), $("td", "num", num((r.usage||{}).output_tokens)));
      tr.onclick = () => { openRun = openRun === tag ? "" : tag; refreshDetail(); };
      tb.append(tr);
      if (openRun === tag) { const x = $("tr"), td = $("td"); td.colSpan = 7; td.style.whiteSpace = "normal";
        const box = $("div", "", "loading…"); td.append(box); x.append(td); tb.append(x); showRun(t.name, tag, box); }
    }
    wrap.append(tb); out.push(wrap, $("div", "meta", "Click a row to see what that conversation did."));
  }
  for (const [title, text] of [["Completion criteria", t.criteria_text], ["State handed to the next iteration", t.state_text]])
    if (text) { const d = $("details"); d.append($("summary", "", title), $("pre", "", text)); out.push(d); }

  const openTitles = [...main.querySelectorAll("details[open] > summary")].map(s => s.textContent);
  main.replaceChildren(...out);
  for (const d of main.querySelectorAll("details")) if (openTitles.includes(d.firstChild.textContent)) d.open = true;
  main.scrollTop = keepScroll;
  const nl = main.querySelector(".live"); if (nl && stick) nl.scrollTop = nl.scrollHeight;
}
const BOOT = /*BOOT*/null;
if (BOOT && BOOT.tasks.length && (!selected || selected === BOOT.tasks[0].name)) {
  selected = BOOT.tasks[0].name; renderList(BOOT.tasks); renderDetail(BOOT.task);
} else { refreshList(); refreshDetail(); }
setInterval(refreshList, 5000);
setInterval(() => { if (!document.hidden && !window.getSelection().toString()) refreshDetail(); }, 2000);
</script></body></html>
"""

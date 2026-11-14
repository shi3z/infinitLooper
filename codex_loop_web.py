# SPDX-License-Identifier: Apache-2.0
"""Web UI for codex-loop: every task's record, and what the running one is doing right now.

It serves what the loop already writes: the task records in ~/.codex-loop/tasks/ and,
while it still exists, each task's state directory under /tmp. It also browses the task's
working directory, takes uploads into it, and can start and stop tasks. Everything that
changes something (start, stop, upload) needs the access token in ~/.codex-loop/web-token,
unless the UI was started with --web-open.
Standard library only. Start it with `codex_loop.py --web`.
"""
from __future__ import annotations

import hmac
import http.server
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_TEXT = 40_000
MAX_PREVIEW = 300_000
MAX_UPLOAD = 2 * 1024 ** 3
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
          ".webp": "image/webp", ".avif": "image/avif", ".bmp": "image/bmp", ".svg": "image/svg+xml"}
VIDEOS = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
          ".ogv": "video/ogg"}
MEDIA = {**IMAGES, **VIDEOS}
INLINE = set(MEDIA.values()) | {"application/pdf"}
MAX_SCAN = 50_000


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


def inside(root: Path, name: str, rel: str) -> tuple[Path, Path] | None:
    """(workspace, target) for a path inside the task's working directory; None when it leads outside it."""
    t = load_task(root / f"{name}.json") if NAME.match(name) else None
    if t is None:
        return None
    try:
        ws = Path(str(t.get("workspace", ""))).resolve(strict=True)
        target = (ws / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    return (ws, target) if target == ws or ws in target.parents else None


def listing(ws: Path, d: Path) -> dict | None:
    if not d.is_dir():
        return None
    entries = []
    try:
        for e in os.scandir(d):
            try:
                st = e.stat()
                entries.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                entries.append({"name": e.name, "dir": False, "size": 0, "mtime": 0})
    except OSError:
        return None
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": str(d.relative_to(ws)) if d != ws else "", "entries": entries[:5000], "count": len(entries)}


def media(ws: Path, d: Path, recursive: bool, newest: bool, page: int, per: int) -> dict | None:
    """One page of the pictures and videos in a folder (optionally with its subfolders)."""
    if not d.is_dir():
        return None
    found, scanned = [], 0
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__"))
        for f in files:
            scanned += 1
            ext = os.path.splitext(f)[1].lower()
            if ext in MEDIA:
                full = Path(root) / f
                try:
                    st = full.stat()
                except OSError:
                    continue
                found.append({"path": str(full.relative_to(ws)), "name": f, "size": st.st_size, "mtime": st.st_mtime,
                              "video": ext in VIDEOS})
        if not recursive or scanned > MAX_SCAN:
            break
    found.sort(key=(lambda x: -x["mtime"]) if newest else (lambda x: x["path"].lower()))
    per = max(1, min(per, 200))
    pages = max(1, -(-len(found) // per))
    page = max(1, min(page, pages))
    return {"path": str(d.relative_to(ws)) if d != ws else "", "total": len(found), "page": page, "pages": pages,
            "per": per, "items": found[(page - 1) * per: page * per], "cut": scanned > MAX_SCAN}


def kind_of(f: Path) -> str:
    return MEDIA.get(f.suffix.lower()) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"


def preview(f: Path) -> dict | None:
    if not f.is_file():
        return None
    size = f.stat().st_size
    ext = f.suffix.lower()
    if ext in MEDIA:
        return {"size": size, "image": ext in IMAGES, "video": ext in VIDEOS}
    with open(f, "rb") as h:
        head = h.read(MAX_PREVIEW)
    if b"\0" in head[:8000]:
        return {"size": size, "binary": True}
    return {"size": size, "text": head.decode("utf-8", errors="replace"), "cut": size > MAX_PREVIEW}


def read_token(home: Path) -> str:
    """The access token, made on first use. Whoever has it can run commands as this user."""
    path = home / "web-token"
    try:
        token = path.read_text().strip()
    except OSError:
        token = ""
    if len(token) < 16:
        token = secrets.token_urlsafe(18)
        home.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    return token


def start_task(root: Path, launch: list[str], body: dict) -> tuple[int, dict]:
    goal = str(body.get("goal", "")).strip()
    raw = str(body.get("workspace", "")).strip()
    if not goal:
        return 400, {"error": "The goal is empty."}
    if not raw or "\0" in raw:
        return 400, {"error": "The working directory is missing."}
    ws = Path(raw).expanduser()
    if not ws.is_absolute():
        ws = Path.home() / ws
    try:
        ws.mkdir(parents=True, exist_ok=True)
        ws = ws.resolve(strict=True)
    except OSError as e:
        return 400, {"error": f"The working directory cannot be used: {e.strerror or e}"}
    for t in tasks(root):
        if t.get("status") == "running" and t.get("workspace") == str(ws):
            return 409, {"error": "A task is already running in that directory. Stop it first, or use another directory."}
    cmd = launch + ["-C", str(ws)]
    if body.get("restart"):
        cmd.append("--restart")
    iters = body.get("max_iters")
    if isinstance(iters, int) and iters > 0:
        cmd += ["--max-iters", str(iters)]
    cmd += ["--", goal]  # after "--" a goal that begins with a dash is still the goal
    logs = root.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.log"
    try:
        with open(log, "w") as out:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=ws,
                                    start_new_session=True)
    except OSError as e:
        return 500, {"error": f"The loop could not be started: {e}"}
    try:
        rc = proc.wait(timeout=2)  # a refusal (another goal already in that folder, no codex) shows at once
    except subprocess.TimeoutExpired:
        return 200, {"ok": True, "pid": proc.pid, "workspace": str(ws), "log": str(log)}
    return 409, {"error": (read(log, 2000).strip() or f"The loop ended at once (exit {rc}).")}


def stop_task(root: Path, name: str) -> tuple[int, dict]:
    t = load_task(root / f"{name}.json") if NAME.match(name) else None
    if t is None:
        return 404, {"error": "No such task."}
    if t.get("status") != "running":
        return 409, {"error": "This task is not running."}
    pid = int(t["pid"])
    try:  # the pid must still be a loop, not some later process that got the same number
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if not any(a.endswith(b"codex_loop.py") for a in cmdline):
            return 409, {"error": "The recorded process is not a loop any more."}
    except OSError:
        pass  # no /proc: trust the record
    try:
        os.kill(pid, signal.SIGTERM)  # the loop ends its Codex conversation and records "interrupted"
    except OSError as e:
        return 500, {"error": str(e)}
    return 200, {"ok": True}


class Handler(http.server.BaseHTTPRequestHandler):
    root: Path
    token: str = ""  # empty: started with --web-open, no token asked
    launch: list[str] = []

    def own_host(self) -> bool:
        """True when the browser addressed this machine by an IP address or by its own name. A page on
        another site can point its own domain at this machine; that name is not ours and is refused."""
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]").lower()
        me = socket.gethostname().lower()
        return bool(re.fullmatch(r"[0-9.]+|[0-9a-f:]+", host)) or host in ("localhost", me) or host.startswith(me + ".")

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
            # `/?task=NAME&dir=FOLDER&view=gallery` opens that task at that folder, with the gallery showing.
            ts = tasks(self.root)
            want = q.get("task", "")
            first = want if any(t["name"] == want for t in ts) else ts[0]["name"] if ts else ""
            boot = {"tasks": ts, "name": first, "open": not self.token, "task": detail(self.root, first) if first else None,
                    "files": None, "media": None}
            where = inside(self.root, first, q.get("dir", "")) if first else None
            if where:
                boot["files"] = listing(*where)
                if q.get("view") == "gallery":
                    boot["media"] = media(*where, False, False, 1, 48)
            blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
            return self.send(PAGE.replace("/*BOOT*/null", blob).encode(), "text/html; charset=utf-8")
        if url.path == "/api/tasks":
            data = tasks(self.root)
        elif url.path == "/api/task" and NAME.match(name):
            data = detail(self.root, name)
        elif url.path == "/api/run" and NAME.match(name):
            data = run(self.root, name, q.get("tag", ""))
        elif url.path in ("/api/files", "/api/file", "/api/media", "/raw"):
            where = inside(self.root, name, q.get("path", ""))
            if where and url.path == "/raw":
                return self.raw(where[1])
            if where and url.path == "/api/media":
                num = lambda k, d: int(q[k]) if q.get(k, "").isdigit() else d  # noqa: E731
                data = media(*where, q.get("recursive") == "1", q.get("sort") == "newest", num("page", 1), num("per", 48))
            elif where:
                data = listing(*where) if url.path == "/api/files" else preview(where[1])
        if data is None:
            return self.send(b'{"error": "not found"}', "application/json", 404)
        self.send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")


    def raw(self, f: Path) -> None:
        if not f.is_file():
            return self.send(b"not found", "text/plain", 404)
        kind, total = kind_of(f), f.stat().st_size
        start, end = 0, total - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        partial = bool(m and (m.group(1) or m.group(2)))
        if partial:  # videos are played by asking for byte ranges
            if m.group(1):
                start, end = int(m.group(1)), min(int(m.group(2) or end), end)
            else:
                start = max(0, total - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        # Files come from the agent and from uploads: never let one run as a page of this site.
        self.send_header("Content-Type", kind if kind in INLINE else "application/octet-stream")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox")
        disp = "inline" if kind in INLINE else "attachment"
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8\'\'{urllib.parse.quote(f.name)}")
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        try:
            with open(f, "rb") as h:
                h.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := h.read(min(left, 1 << 20))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            pass

    def do_POST(self) -> None:
        """Start a task, stop one, or upload a file (the request body) into a task's working directory."""
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        fail = lambda code, msg: self.send(json.dumps({"error": msg}).encode(), "application/json", code)  # noqa: E731
        # A page on another site cannot set this header, so it cannot act through someone's browser.
        if url.path not in ("/api/upload", "/api/start", "/api/stop", "/api/token") \
                or self.headers.get("X-Codex-Loop") != "1" or not self.own_host():
            return fail(404, "not found")
        if self.token and not hmac.compare_digest(self.headers.get("X-Token", "").encode(), self.token.encode()):
            return fail(403, "The access token is missing or wrong.")
        if url.path == "/api/token":
            return self.send(b'{"ok": true}', "application/json")
        if url.path in ("/api/start", "/api/stop"):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size)) if 0 < size <= 1_000_000 else {}
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return fail(400, "bad request")
            code, out = start_task(self.root, self.launch, body) if url.path == "/api/start" \
                else stop_task(self.root, str(body.get("name", "")))
            return self.send(json.dumps(out, ensure_ascii=False).encode(), "application/json", code)
        fname = q.get("filename", "")
        where = inside(self.root, q.get("name", ""), q.get("path", ""))
        if where is None or not where[1].is_dir():
            return fail(404, "no such directory in the working directory")
        if not fname or fname in (".", "..") or "/" in fname or "\\" in fname or "\0" in fname:
            return fail(400, "bad file name")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return fail(411, "Content-Length is required")
        if size > MAX_UPLOAD:
            return fail(413, f"larger than {MAX_UPLOAD // 1024 ** 3} GB")
        dest = where[1] / fname
        if dest.is_dir() or dest.is_symlink():
            return fail(409, "a directory or link of that name is in the way")
        replaced = dest.exists()
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(dir=where[1], prefix=".upload-", delete=False) as h:
                tmp = Path(h.name)
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        raise OSError("the upload stopped early")
                    h.write(chunk)
                    left -= len(chunk)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        except OSError as e:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            return fail(500, str(e))
        self.send(json.dumps({"ok": True, "name": fname, "size": size, "replaced": replaced}).encode(), "application/json")


def serve(root: Path, host: str, port: int, launch: list[str], open_access: bool = False) -> int:
    """`launch` is the command that starts a loop; `-C DIR`, options and the goal are appended to it."""
    Handler.root, Handler.launch = root, launch
    Handler.token = "" if open_access else read_token(root.parent)
    try:
        srv = http.server.ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"codex-loop web UI: cannot listen on {host}:{port} ({e.strerror}). Choose another with --web-port.")
        return 1
    srv.daemon_threads = True
    shown = socket.gethostname() if host in ("0.0.0.0", "::") else host
    print(f"codex-loop web UI: http://{shown}:{port}/   (Ctrl+C to stop)", flush=True)
    local = host in ("127.0.0.1", "localhost", "::1")
    if open_access:
        print("No access token (--web-open): " + ("any program on this machine" if local else
              "anyone who can reach this address") + " can start tasks, which means running commands as you.",
              flush=True)
    else:
        print(f"To start or stop tasks and to upload, open it once with the access token:\n"
              f"  http://{shown}:{port}/?token={Handler.token}\n"
              f"The token is kept in {root.parent / 'web-token'}. Whoever has it can run commands as you.", flush=True)
    if not local:
        print("Reading needs no token: anyone who can reach this address can read the goals, logs, command output "
              "and the working directories' files.", flush=True)
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
aside h1 { font-size:15px; margin:0; padding:14px 16px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; justify-content:space-between; }
form.new { max-width:760px; display:grid; gap:14px; }
form.new label { display:grid; gap:4px; font-weight:600; }
form.new .hint { font-weight:400; color:var(--dim); font-size:13px; }
form.new textarea, form.new input[type=text], form.new input[type=number], form.new input[type=password] {
  font:inherit; color:var(--text); background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:8px 10px; width:100%; }
form.new textarea { min-height:9em; resize:vertical; }
form.new .check { display:flex; gap:8px; align-items:center; font-weight:400; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
button.danger { color:var(--bad); }
.warn { border:1px solid var(--line); border-left:3px solid var(--run); border-radius:6px; padding:8px 12px; font-size:13px; }
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
.bar { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin-bottom:8px; }
.crumbs { overflow-wrap:anywhere; flex:1 1 240px; }
.crumbs a, td a { color:var(--accent); cursor:pointer; text-decoration:none; }
button, .btn { font:inherit; padding:4px 12px; border:1px solid var(--line); border-radius:6px; background:var(--panel);
       color:var(--text); cursor:pointer; }
button:hover { border-color:var(--accent); }
#files.drag .drop { border-color:var(--accent); background:var(--panel); }
.drop { border:1px dashed var(--line); border-radius:6px; padding:8px 12px; color:var(--dim); font-size:13px; margin-top:8px; }
td.name { white-space:normal; overflow-wrap:anywhere; }
.pv img { max-width:100%; border:1px solid var(--line); border-radius:6px; }
.pv pre { max-height:60vh; overflow:auto; }
.pv video { max-width:100%; max-height:70vh; }
button.on { border-color:var(--accent); color:var(--accent); }
button:disabled { opacity:.45; cursor:default; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:10px; }
.cell { background:var(--panel); border:1px solid var(--line); border-radius:6px; overflow:hidden; cursor:pointer; }
.cell:hover { border-color:var(--accent); }
.thumb { aspect-ratio:1; background:var(--code); display:flex; align-items:center; justify-content:center; position:relative; }
.thumb img, .thumb video { width:100%; height:100%; object-fit:cover; display:block; }
.thumb .play { position:absolute; right:6px; bottom:6px; background:#000a; color:#fff; border-radius:4px; padding:0 6px; font-size:12px; }
.cap { padding:4px 8px; font-size:12px; color:var(--dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.pager { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:10px 0; }
.pager select, .pager label { font:inherit; color:var(--text); }
#box { position:fixed; inset:0; background:#000d; display:none; flex-direction:column; align-items:center; justify-content:center; z-index:10; }
#box.open { display:flex; }
#box img, #box video { max-width:94vw; max-height:84vh; }
#box .t { color:#eee; margin-top:10px; max-width:94vw; overflow-wrap:anywhere; text-align:center; }
#box .t a { color:#9cc0ff; }
#box button { position:fixed; top:50%; transform:translateY(-50%); background:#0008; color:#fff; border-color:#fff5; font-size:22px; padding:10px 14px; }
#box .prev { left:10px; } #box .next { right:10px; } #box .close { top:26px; right:10px; font-size:16px; }
@media (max-width: 760px) { .app { grid-template-columns:1fr; height:auto; } aside { max-height:40vh; } }
</style></head><body>
<div class="app">
  <aside><h1><span>codex-loop tasks</span><button id="new">New task</button></h1><div id="list"></div></aside>
  <main id="main"><div id="detail"><div class="empty">No task selected.</div></div><div id="files"></div></main>
<div id="box"><button class="close">Close</button><button class="prev" aria-label="Previous">‹</button><button class="next" aria-label="Next">›</button><div class="m"></div><div class="t"></div></div>
</div>
<script>
const oops = m => { const d = document.createElement("div"); d.className = "fail"; d.textContent = "Page error: " + m; document.getElementById("main").prepend(d); };
window.addEventListener("error", e => oops(e.message + " (line " + e.lineno + ")"));
window.addEventListener("unhandledrejection", e => oops(e.reason && e.reason.message || e.reason));
const $ = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; return e; };
const ago = s => { s = Math.max(0, Math.round(s)); if (s < 90) return s + " s"; if (s < 5400) return Math.round(s/60) + " min";
  if (s < 129600) return Math.round(s/3600) + " h"; return Math.round(s/86400) + " d"; };
const num = n => (n || 0).toLocaleString("en-US");
const when = t => new Date(t * 1000).toLocaleString();
const QS = new URLSearchParams(location.search);
let selected = QS.get("task") || location.hash.slice(1), openRun = "", runCache = {}, mode = "task", known = [];
// The access token arrives once in the link that `codex-loop --web` prints, and stays in this browser.
if (QS.get("token")) { localStorage.setItem("codex-loop-token", QS.get("token")); QS.delete("token");
  history.replaceState(null, "", location.pathname + (QS.toString() ? "?" + QS : "") + location.hash); }
const token = () => localStorage.getItem("codex-loop-token") || "";
async function post(url, body, raw) {
  try {
    const r = await fetch(url, { method: "POST", body: raw ? body : JSON.stringify(body || {}),
      headers: { "X-Codex-Loop": "1", "X-Token": token() } });
    const j = await r.json().catch(() => ({}));
    if (r.status === 403) return { error: "This needs the access token. Open the link that `codex-loop --web` printed (it is also in ~/.codex-loop/web-token on the machine), or paste the token under “New task”.", denied: true };
    return r.ok ? j : { error: j.error || "Request failed (" + r.status + ")." };
  } catch (e) { return { error: "The server cannot be reached: " + e.message }; }
}

async function get(url) { const r = await fetch(url); return r.ok ? r.json() : null; }

function statusBadge(t) { return $("span", "badge " + t.status, t.status === "done" ? "goal met" : t.status); }

async function refreshList() { renderList(await get("/api/tasks") || []); }
function renderList(tasks) {
  known = tasks;
  const list = document.getElementById("list"); list.replaceChildren();
  if (!tasks.length) list.append($("div", "task meta", "No tasks yet. Start one with: codex-loop \"goal\""));
  if (!selected && tasks.length && mode === "task") { selected = tasks[0].name; refreshDetail(); refreshFiles(); }
  for (const t of tasks) {
    const d = $("div", "task" + (t.name === selected && mode === "task" ? " sel" : ""));
    d.append(statusBadge(t), $("span", "meta", "updated " + ago(Date.now()/1000 - t.updated) + " ago"));
    d.append($("div", "g", t.goal || "(no goal)"), $("div", "meta", t.workspace));
    d.onclick = () => { mode = "task"; selected = t.name; location.hash = t.name; openRun = ""; dir = ""; shown = "";
      refreshList(); refreshDetail(); refreshFiles(); };
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

async function refreshDetail() { if (selected && mode === "task") { const t = await get("/api/task?name=" + selected); if (mode === "task") renderDetail(t); } }
function renderDetail(t) {
  const main = document.getElementById("detail");
  if (!t) { main.replaceChildren($("div", "empty", "This task's record is gone.")); return; }
  const liveBox = main.querySelector(".live");
  const keepScroll = main.scrollTop, stick = !liveBox || liveBox.scrollTop + liveBox.clientHeight >= liveBox.scrollHeight - 8;
  const out = [];
  const head = $("div", "bar"); head.append(statusBadge(t));
  if (t.ended) head.append($("span", "meta", t.ended));
  if (t.status === "running") { const st = $("button", "danger", "Stop this task"); st.style.marginLeft = "auto";
    st.onclick = async () => { if (!confirm("Stop this task? It can be resumed later from its directory.")) return;
      st.disabled = true; const r = await post("/api/stop", { name: t.name }); if (r.error) alert(r.error); refreshList(); refreshDetail(); };
    head.append(st); }
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
// ---- the working directory: browse, preview, upload ----
let dir = QS.get("dir") || "", shown = "", note = "", view = QS.get("view") === "gallery" ? "gallery" : "list", gpage = 1, grec = false, gsort = "name", gitems = [], gat = -1;
const size = n => n < 1024 ? n + " B" : n < 1048576 ? (n/1024).toFixed(1) + " KB" : n < 1073741824 ? (n/1048576).toFixed(1) + " MB" : (n/1073741824).toFixed(2) + " GB";
const join = (a, b) => a ? a + "/" + b : b;
const fq = (path) => `name=${encodeURIComponent(selected)}&path=${encodeURIComponent(path)}`;

async function refreshFiles(first) {
  const box = document.getElementById("files");
  if (!selected || mode !== "task") { box.replaceChildren(); return; }
  const task = selected, l = first || await get("/api/files?" + fq(dir));
  if (task !== selected) return;
  const out = [section("Working directory")];
  if (!l) { if (dir) { dir = ""; return refreshFiles(); }
    out.push($("div", "meta", "The working directory cannot be read.")); box.replaceChildren(...out); return; }
  const bar = $("div", "bar"), crumbs = $("div", "crumbs");
  const go = (p) => { dir = p; shown = ""; note = ""; gpage = 1; refreshFiles(); };
  const root = $("a", "", "workspace"); root.onclick = () => go(""); crumbs.append(root);
  let acc = ""; for (const part of l.path ? l.path.split("/") : []) { acc = join(acc, part); const p = acc;
    const a = $("a", "", part); a.onclick = () => go(p); crumbs.append(" / ", a); }
  const pick = $("input"); pick.type = "file"; pick.multiple = true; pick.hidden = true;
  pick.onchange = () => upload(pick.files);
  const up = $("button", "", "Upload files here"); up.onclick = () => pick.click();
  const vl = $("button", view === "list" ? "on" : "", "List"), vg = $("button", view === "gallery" ? "on" : "", "Gallery");
  vl.onclick = () => { view = "list"; refreshFiles(); }; vg.onclick = () => { view = "gallery"; shown = ""; refreshFiles(); };
  bar.append(crumbs, vl, vg, up, pick); out.push(bar);
  if (note) out.push($("div", "meta", note));
  if (view === "gallery") { box.replaceChildren(...out, await gallery(first && BOOT.media)); return; }
  const wrap = $("div", "tablewrap"), tb = $("table");
  const hr = $("tr"); hr.append($("th", "", "name"), $("th", "num", "size"), $("th", "", "modified")); tb.append(hr);
  for (const e of l.entries) {
    const tr = $("tr"), td = $("td", "name"), a = $("a", "", e.name + (e.dir ? "/" : ""));
    a.onclick = () => { if (e.dir) go(join(l.path, e.name)); else { shown = shown === e.name ? "" : e.name; refreshFiles(); } };
    td.append(a); tr.append(td, $("td", "num", e.dir ? "" : size(e.size)), $("td", "", e.mtime ? when(e.mtime) : ""));
    tb.append(tr);
    if (!e.dir && shown === e.name) { const x = $("tr"), c = $("td", "pv"); c.colSpan = 3; c.style.whiteSpace = "normal";
      x.append(c); tb.append(x); showFile(join(l.path, e.name), c); }
  }
  if (!l.entries.length) { const tr = $("tr"), td = $("td", "meta", "This folder is empty."); td.colSpan = 3; tr.append(td); tb.append(tr); }
  wrap.append(tb); out.push(wrap);
  if (l.count > l.entries.length) out.push($("div", "meta", `Showing ${l.entries.length} of ${l.count} entries.`));
  out.push($("div", "drop", "Drop files anywhere on this section to upload them into the folder shown. A file of the same name is replaced."));
  box.replaceChildren(...out);
}
async function gallery(first) {
  const wrap = $("div"), task = selected;
  const g = first || await get(`/api/media?${fq(dir)}&page=${gpage}&per=48&sort=${gsort}&recursive=${grec ? 1 : 0}`);
  if (!g || task !== selected) { wrap.append($("div", "meta", "This folder cannot be read.")); return wrap; }
  gpage = g.page; gitems = g.items;
  const pager = () => { const p = $("div", "pager");
    const b = (label, to, off) => { const x = $("button", "", label); x.disabled = off; x.onclick = () => { gpage = to; refreshFiles(); }; return x; };
    p.append(b("« First", 1, g.page <= 1), b("‹ Previous", g.page - 1, g.page <= 1),
      $("span", "", `Page ${g.page} of ${g.pages}`), b("Next ›", g.page + 1, g.page >= g.pages), b("Last »", g.pages, g.page >= g.pages),
      $("span", "meta", `${num(g.total)} pictures and videos`));
    return p; };
  const opts = $("div", "pager"), sel = $("select"), lab = $("label"), cb = $("input"), rf = $("button", "", "Refresh");
  for (const [v, t] of [["name", "Sort by name"], ["newest", "Newest first"]]) { const o = $("option", "", t); o.value = v; o.selected = gsort === v; sel.append(o); }
  sel.onchange = () => { gsort = sel.value; gpage = 1; refreshFiles(); };
  cb.type = "checkbox"; cb.checked = grec; cb.onchange = () => { grec = cb.checked; gpage = 1; refreshFiles(); };
  lab.append(cb, " Include subfolders"); rf.onclick = () => refreshFiles();
  opts.append(sel, lab, rf); wrap.append(opts);
  if (!g.total) { wrap.append($("div", "meta", grec ? "No pictures or videos here." : "No pictures or videos in this folder. Try “Include subfolders”.")); return wrap; }
  wrap.append(pager());
  const grid = $("div", "grid");
  g.items.forEach((it, i) => {
    const cell = $("div", "cell"), th = $("div", "thumb"), src = "/raw?" + fq(it.path);
    if (it.video) { const v = $("video"); v.src = src + "#t=0.1"; v.preload = "metadata"; v.muted = true; th.append(v, $("span", "play", "▶ video")); }
    else { const im = $("img"); im.src = src; im.loading = "lazy"; im.alt = it.name; th.append(im); }
    cell.title = it.path; cell.append(th, $("div", "cap", it.name)); cell.onclick = () => openBox(i);
    grid.append(cell);
  });
  wrap.append(grid, pager());
  if (g.cut) wrap.append($("div", "meta", "This folder tree is very large; only part of it was scanned."));
  return wrap;
}
const lb = document.getElementById("box");
function openBox(i) {
  if (i < 0 || i >= gitems.length) return;
  gat = i; const it = gitems[i], src = "/raw?" + fq(it.path), m = lb.querySelector(".m"), t = lb.querySelector(".t");
  let el; if (it.video) { el = $("video"); el.controls = true; el.autoplay = true; } else { el = $("img"); el.alt = it.name; }
  el.src = src; m.replaceChildren(el);
  const dl = $("a", "", "Download"); dl.href = src; dl.download = "";
  t.replaceChildren(`${it.path} · ${size(it.size)} · ${(gpage - 1) * 48 + i + 1} of the gallery · `, dl);
  lb.querySelector(".prev").disabled = i === 0; lb.querySelector(".next").disabled = i === gitems.length - 1;
  lb.classList.add("open");
}
function closeBox() { lb.classList.remove("open"); lb.querySelector(".m").replaceChildren(); gat = -1; }
lb.querySelector(".close").onclick = closeBox;
lb.querySelector(".prev").onclick = e => { e.stopPropagation(); openBox(gat - 1); };
lb.querySelector(".next").onclick = e => { e.stopPropagation(); openBox(gat + 1); };
lb.onclick = e => { if (e.target === lb) closeBox(); };
document.addEventListener("keydown", e => { if (gat < 0) return;
  if (e.key === "Escape") closeBox(); else if (e.key === "ArrowLeft") openBox(gat - 1); else if (e.key === "ArrowRight") openBox(gat + 1); });

async function showFile(path, cell) {
  const raw = "/raw?" + fq(path), p = await get("/api/file?" + fq(path));
  cell.replaceChildren();
  const dl = $("a", "", "Download"); dl.href = raw; dl.download = "";
  if (!p) { cell.append($("span", "meta", "This file is gone.")); return; }
  if (p.image) { const img = $("img"); img.src = raw; img.alt = path; cell.append(img, $("div"), dl); return; }
  if (p.video) { const v = $("video"); v.src = raw; v.controls = true; v.preload = "metadata"; cell.append(v, $("div"), dl); return; }
  if (p.binary) { cell.append($("span", "meta", `Binary file, ${size(p.size)}. `), dl); return; }
  cell.append($("pre", "", p.text), $("div", "meta", p.cut ? `Only the first part of ${size(p.size)} is shown. ` : ""), dl);
}
async function upload(files) {
  const target = dir, task = selected; let done = 0, failed = [];
  for (const f of files) {
    note = `Uploading ${f.name} (${done + 1} of ${files.length}) …`; refreshFiles();
    try {
      const r = await post(`/api/upload?${fq(target)}&filename=${encodeURIComponent(f.name)}`, f, true);
      if (!r.error) done++; else { failed.push(f.name + ": " + r.error); if (r.denied) break; }
    } catch (e) { failed.push(f.name + ": " + e.message); }
  }
  if (task !== selected) return;
  note = `Uploaded ${done} of ${files.length} file(s) into ${target || "the workspace"}.` + (failed.length ? " Failed: " + failed.join("; ") : "");
  refreshFiles();
}
const fb = document.getElementById("files");
fb.addEventListener("dragover", e => { e.preventDefault(); fb.classList.add("drag"); });
fb.addEventListener("dragleave", () => fb.classList.remove("drag"));
fb.addEventListener("drop", e => { e.preventDefault(); fb.classList.remove("drag"); if (e.dataTransfer.files.length) upload(e.dataTransfer.files); });

// ---- a new task ----
function newTask() {
  mode = "new"; renderList(known); document.getElementById("files").replaceChildren();
  const f = $("form", "new"), goal = $("textarea"), ws = $("input"), dl = $("datalist"), re = $("input"), it = $("input"), tk = $("input");
  const msg = $("div", "fail"), go = $("button", "primary", "Start the task"), cancel = $("button", "", "Cancel");
  const field = (title, hint, el) => { const l = $("label"); l.append(title, $("span", "hint", hint), el); return l; };
  goal.required = true; goal.placeholder = "What should be true when the work is finished?";
  ws.type = "text"; ws.required = true; ws.setAttribute("list", "dirs"); dl.id = "dirs";
  ws.placeholder = "~/work/my-project"; ws.value = localStorage.getItem("codex-loop-dir") || "";
  for (const w of [...new Set(known.map(t => t.workspace))]) { const o = $("option"); o.value = w; dl.append(o); }
  re.type = "checkbox"; it.type = "number"; it.min = "1"; it.placeholder = "no limit";
  tk.type = "password"; tk.autocomplete = "off"; tk.placeholder = token() ? "saved in this browser" : "paste the token";
  const chk = $("label", "check"); chk.append(re, "Replace the earlier task of this directory (its loop state is discarded; the files stay)");
  const row = $("div", "bar"); go.type = "submit"; cancel.type = "button"; row.append(go, cancel);
  f.append($("h2", "", "New task"),
    field("Goal", "The loop plans completion criteria from this, then works until an independent evaluation passes all of them.", goal),
    field("Working directory", "On the machine that runs the loop. It is created if it does not exist; a path without a leading / is taken from the home folder.", ws), dl,
    chk,
    field("Stop after this many iterations", "Leave empty to run until the goal is met.", it),
    ...(BOOT && BOOT.open ? [] : [field("Access token", "Needed once per browser. It is printed by `codex-loop --web` and kept in ~/.codex-loop/web-token.", tk)]),
    $("div", "warn", "The agent runs without approvals and, by default, without a sandbox: its commands have the full rights of the user who started this web UI."),
    msg, row);
  cancel.onclick = () => { mode = "task"; renderList(known); refreshDetail(); refreshFiles(); };
  f.onsubmit = async e => { e.preventDefault(); msg.textContent = ""; go.disabled = true; go.textContent = "Starting …";
    if (tk.value.trim()) localStorage.setItem("codex-loop-token", tk.value.trim());
    localStorage.setItem("codex-loop-dir", ws.value.trim());
    const r = await post("/api/start", { goal: goal.value, workspace: ws.value.trim(), restart: re.checked, max_iters: parseInt(it.value) || 0 });
    if (r.error) { msg.textContent = r.error; go.disabled = false; go.textContent = "Start the task"; return; }
    // The loop writes its record within a moment; open it as soon as it is there.
    for (let i = 0; i < 20; i++) { await new Promise(x => setTimeout(x, 500));
      const t = (await get("/api/tasks") || []).find(x => x.pid === r.pid && x.status === "running");
      if (t) { mode = "task"; selected = t.name; location.hash = t.name; openRun = ""; dir = ""; shown = ""; view = "list";
        refreshList(); refreshDetail(); refreshFiles(); return; } }
    msg.textContent = "The loop was started (process " + r.pid + ") but its record has not appeared. Its output is in " + r.log + " on the machine.";
    go.disabled = false; go.textContent = "Start the task"; };
  document.getElementById("detail").replaceChildren(f); goal.focus();
}
document.getElementById("new").onclick = newTask;

const BOOT = /*BOOT*/null;
if (BOOT && BOOT.name && (!selected || selected === BOOT.name)) {
  selected = BOOT.name; renderList(BOOT.tasks); renderDetail(BOOT.task);
  refreshFiles(BOOT.files);
} else { refreshList(); refreshDetail(); refreshFiles(); }
setInterval(() => { if (!document.hidden && mode === "task" && !shown && view === "list" && !note.startsWith("Uploading") && !window.getSelection().toString()) refreshFiles(); }, 6000);
setInterval(refreshList, 5000);
setInterval(() => { if (!document.hidden && !window.getSelection().toString()) refreshDetail(); }, 2000);
</script></body></html>
"""

#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Responses API in front of a chat-completions server.

Codex speaks the OpenAI Responses API; servers such as YASAKANI (`yasakani serve`) offer only
`/v1/chat/completions`.  This adapter listens where codex-loop expects its model server and
translates both ways, streaming:

    yasakani serve --port 8766 --model-dir ... --dll ...
    python3 responses_adapter.py --upstream http://127.0.0.1:8766/v1 --port 8080
    codex-loop "goal"                      # LOOP_URL defaults to http://127.0.0.1:8080/v1

Only the Python standard library is used.  One request is forwarded at a time (the engine
generates one answer at a time); others wait in line.
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid

EFFORTS = ("low", "medium", "high", "xhigh")


def text_of(content) -> str:
    """The text of a Responses `content` (a string or a list of parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for part in content if isinstance(content, list) else [content]:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            if isinstance(part.get("text"), str):
                out.append(part["text"])
            elif part.get("type") in ("input_image", "image_url"):
                out.append("[an image was attached; this model cannot see images]")
    return "".join(out) if len(out) < 2 else "\n".join(out)


def output_text(output) -> str:
    """The text of a function_call_output `output` (a string, an object or a list of parts)."""
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        if isinstance(output.get("content"), (str, list)):
            return text_of(output["content"])
        return json.dumps(output, ensure_ascii=False)
    if isinstance(output, list):
        return text_of(output)
    return "" if output is None else str(output)


def convert_tools(tools):
    """Responses tools -> (chat tools, {name: kind}).  Kinds: function, custom.  Others are left out."""
    chat, kinds = [], {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        kind, name = t.get("type"), t.get("name")
        if kind == "function" and name:
            chat.append({"type": "function", "function": {"name": name, "description": t.get("description") or "",
                                                           "parameters": t.get("parameters") or {"type": "object", "properties": {}}}})
            kinds[name] = "function"
        elif kind == "custom" and name:          # a free-form tool (e.g. apply_patch): offered as a function with one string argument
            chat.append({"type": "function", "function": {
                "name": name, "description": (t.get("description") or "") + "\nPass the whole input as the string argument `input`.",
                "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}}})
            kinds[name] = "custom"
    return chat, kinds


def convert_input(body) -> list:
    """Responses `instructions` + `input` -> chat messages."""
    system = [body["instructions"]] if isinstance(body.get("instructions"), str) and body["instructions"].strip() else []
    msgs: list = []
    open_calls: set = set()
    items = body.get("input")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        kind = it.get("type") or ("message" if "role" in it else "")
        if kind == "message":
            role, text = it.get("role") or "user", text_of(it.get("content"))
            if not text.strip():
                continue
            if role in ("system", "developer"):
                if not msgs:
                    system.append(text)              # the chat template wants one system message, first
                else:
                    msgs.append({"role": "user", "content": text})
            elif role == "assistant":
                if msgs and msgs[-1]["role"] == "assistant" and not msgs[-1].get("tool_calls"):
                    msgs[-1]["content"] = (msgs[-1].get("content") or "") + "\n" + text
                else:
                    msgs.append({"role": "assistant", "content": text})
            else:
                msgs.append({"role": "user", "content": text})
        elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
            cid = it.get("call_id") or it.get("id") or ("call_" + uuid.uuid4().hex[:12])
            if kind == "custom_tool_call":
                args = json.dumps({"input": it.get("input") or ""}, ensure_ascii=False)
            elif kind == "local_shell_call":
                args = json.dumps(it.get("action") or {}, ensure_ascii=False)
            else:
                args = it.get("arguments") if isinstance(it.get("arguments"), str) else json.dumps(it.get("arguments") or {})
            call = {"id": cid, "type": "function", "function": {"name": it.get("name") or "shell", "arguments": args}}
            if msgs and msgs[-1]["role"] == "assistant":
                msgs[-1].setdefault("tool_calls", []).append(call)
            else:
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call]})
            open_calls.add(cid)
        elif kind in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            cid, text = it.get("call_id"), output_text(it.get("output"))
            if cid in open_calls:
                open_calls.discard(cid)
                msgs.append({"role": "tool", "tool_call_id": cid, "content": text})
            else:                                    # its call was compacted away: keep the text, as a user message
                msgs.append({"role": "user", "content": "[tool output]\n" + text})
        # reasoning items and anything unknown are left out
    # a tool call whose output never came (interrupted turn) would break the template: drop such calls
    if open_calls:
        for m in msgs:
            if m.get("tool_calls"):
                m["tool_calls"] = [c for c in m["tool_calls"] if c["id"] not in open_calls]
                if not m["tool_calls"]:
                    del m["tool_calls"]
        msgs = [m for m in msgs if m.get("tool_calls") or (m.get("content") or "").strip() or m["role"] == "tool"]
    # merge neighbouring user messages
    out: list = []
    for m in msgs:
        if out and m["role"] == "user" and out[-1]["role"] == "user":
            out[-1]["content"] += "\n\n" + m["content"]
        else:
            out.append(m)
    if system:
        out.insert(0, {"role": "system", "content": "\n\n".join(system)})
    if not any(m["role"] == "user" for m in out):
        out.append({"role": "user", "content": "Continue."})
    return out


def chat_request(body, cfg) -> tuple:
    tools, kinds = convert_tools(body.get("tools"))
    req = {"messages": convert_input(body), "stream": True, "stream_options": {"include_usage": True}}
    if tools and body.get("tool_choice") != "none":
        req["tools"] = tools
    mt = body.get("max_output_tokens") or cfg.max_tokens
    if mt:
        req["max_tokens"] = int(mt)
    for k in ("temperature", "top_p"):
        if isinstance(body.get(k), (int, float)):
            req[k] = body[k]
    effort = cfg.effort or ((body.get("reasoning") or {}).get("effort") if isinstance(body.get("reasoning"), dict) else None)
    if effort == "none" or cfg.effort == "off":
        req["chat_template_kwargs"] = {"enable_thinking": False}
    elif effort in EFFORTS:
        req["reasoning_effort"] = effort
    elif effort == "minimal":
        req["reasoning_effort"] = "low"
    if cfg.thinking_budget:
        req["thinking_budget"] = cfg.thinking_budget
    return req, kinds


class Adapter(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg):
        self.cfg = cfg
        self.up = urllib.parse.urlsplit(cfg.upstream)
        self.gate = threading.Lock()                 # the engine writes one answer at a time
        super().__init__(addr, Handler)

    def upstream(self, method, path, body=None, timeout=3600):
        conn = http.client.HTTPConnection(self.up.netloc, timeout=timeout)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if self.cfg.upstream_key:
            headers["Authorization"] = "Bearer " + self.cfg.upstream_key
        conn.request(method, self.up.path.rstrip("/") + path, body=body, headers=headers)
        return conn, conn.getresponse()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if self.server.cfg.verbose:
            sys.stderr.write("[adapter] " + fmt % a + "\n")

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            conn, resp = self.server.upstream("GET", path[len(self.server.up.path.rstrip("/")):] if path.startswith(self.server.up.path.rstrip("/")) else path, timeout=10)
            data = resp.read()
            conn.close()
        except OSError as e:
            return self.send_json(502, {"error": {"message": "model server unreachable: %s" % e}})
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path.endswith("/chat/completions"):       # already chat: pass through
            return self.passthrough(raw)
        if not path.endswith("/responses"):
            return self.send_json(404, {"error": {"message": "unknown path " + path}})
        try:
            body = json.loads(raw or b"{}")
            req, kinds = chat_request(body, self.server.cfg)
        except (ValueError, TypeError, KeyError) as e:
            return self.send_json(400, {"error": {"message": "bad request: %s" % e, "type": "invalid_request_error"}})
        if self.server.cfg.dump:
            with open(self.server.cfg.dump, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "responses": body, "chat": req}, ensure_ascii=False) + "\n")
        with self.server.gate:
            self.respond(body, req, kinds)

    def passthrough(self, raw):
        with self.server.gate:
            try:
                conn, resp = self.server.upstream("POST", "/chat/completions", raw)
            except OSError as e:
                return self.send_json(502, {"error": {"message": "model server unreachable: %s" % e}})
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type") or "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError:
                pass
            conn.close()
            self.close_connection = True

    # ---- the translation of one answer ---------------------------------------------------------------------------------------
    def respond(self, body, req, kinds):
        stream = bool(body.get("stream", True))
        try:
            conn, resp = self.server.upstream("POST", "/chat/completions", json.dumps(req, ensure_ascii=False).encode())
        except OSError as e:
            return self.send_json(502, {"error": {"message": "model server unreachable: %s" % e}})
        if resp.status != 200:
            data = resp.read()
            conn.close()
            try:
                err = json.loads(data)
            except ValueError:
                err = {"error": {"message": data.decode("utf-8", "replace")[:2000]}}
            return self.send_json(resp.status, err)

        rid = "resp_" + uuid.uuid4().hex
        model = body.get("model") or "yasakani"
        seq = [0]
        items: list = []                              # finished output items
        cur = {"kind": None}                          # the item being written: reasoning or message
        usage = {}
        finish = [None]

        def base(status):
            return {"id": rid, "object": "response", "created_at": int(time.time()), "status": status, "model": model, "output": [],
                    "parallel_tool_calls": True, "tools": body.get("tools") or [], "tool_choice": body.get("tool_choice", "auto")}

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def emit(kind, **payload):
            if not stream:
                return
            payload = dict(type=kind, sequence_number=seq[0], **payload)
            seq[0] += 1
            self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, json.dumps(payload, ensure_ascii=False))).encode())
            self.wfile.flush()

        def close_item():
            k = cur["kind"]
            if k == "reasoning":
                item = {"id": cur["id"], "type": "reasoning", "summary": [{"type": "summary_text", "text": cur["text"]}]}
                emit("response.reasoning_summary_text.done", item_id=cur["id"], output_index=len(items), summary_index=0, text=cur["text"])
                emit("response.reasoning_summary_part.done", item_id=cur["id"], output_index=len(items), summary_index=0,
                     part={"type": "summary_text", "text": cur["text"]})
                emit("response.output_item.done", output_index=len(items), item=item)
                items.append(item)
            elif k == "message":
                part = {"type": "output_text", "text": cur["text"], "annotations": []}
                item = {"id": cur["id"], "type": "message", "role": "assistant", "status": "completed", "content": [part]}
                emit("response.output_text.done", item_id=cur["id"], output_index=len(items), content_index=0, text=cur["text"])
                emit("response.content_part.done", item_id=cur["id"], output_index=len(items), content_index=0, part=part)
                emit("response.output_item.done", output_index=len(items), item=item)
                items.append(item)
            cur.clear()
            cur["kind"] = None

        def feed(kind, text):
            if cur["kind"] != kind:
                close_item()
                cur.update(kind=kind, text="", id=("rs_" if kind == "reasoning" else "msg_") + uuid.uuid4().hex[:24])
                if kind == "reasoning":
                    emit("response.output_item.added", output_index=len(items), item={"id": cur["id"], "type": "reasoning", "summary": []})
                    emit("response.reasoning_summary_part.added", item_id=cur["id"], output_index=len(items), summary_index=0,
                         part={"type": "summary_text", "text": ""})
                else:
                    emit("response.output_item.added", output_index=len(items),
                         item={"id": cur["id"], "type": "message", "role": "assistant", "status": "in_progress", "content": []})
                    emit("response.content_part.added", item_id=cur["id"], output_index=len(items), content_index=0,
                         part={"type": "output_text", "text": "", "annotations": []})
            cur["text"] += text
            if kind == "reasoning":
                emit("response.reasoning_summary_text.delta", item_id=cur["id"], output_index=len(items), summary_index=0, delta=text)
            else:
                emit("response.output_text.delta", item_id=cur["id"], output_index=len(items), content_index=0, delta=text)

        def tool_call(tc):
            close_item()
            fn = tc.get("function") or {}
            name, args = fn.get("name") or "", fn.get("arguments") or "{}"
            cid = tc.get("id") or ("call_" + uuid.uuid4().hex[:24])
            iid = "fc_" + uuid.uuid4().hex[:24]
            if kinds.get(name) == "custom":
                try:
                    inp = json.loads(args).get("input", "")
                except (ValueError, AttributeError):
                    inp = args
                item = {"id": iid, "type": "custom_tool_call", "status": "completed", "call_id": cid, "name": name, "input": inp if isinstance(inp, str) else json.dumps(inp)}
                emit("response.output_item.added", output_index=len(items), item=dict(item, status="in_progress", input=""))
                emit("response.custom_tool_call_input.delta", item_id=iid, output_index=len(items), delta=item["input"])
            else:
                item = {"id": iid, "type": "function_call", "status": "completed", "call_id": cid, "name": name, "arguments": args}
                emit("response.output_item.added", output_index=len(items), item=dict(item, status="in_progress", arguments=""))
                emit("response.function_call_arguments.delta", item_id=iid, output_index=len(items), delta=args)
                emit("response.function_call_arguments.done", item_id=iid, output_index=len(items), arguments=args)
            emit("response.output_item.done", output_index=len(items), item=item)
            items.append(item)

        emit("response.created", response=base("in_progress"))
        emit("response.in_progress", response=base("in_progress"))
        pending: dict = {}                            # tool calls that arrive in pieces (other servers), by index
        failed = None
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if isinstance(ev.get("error"), dict):
                    failed = ev["error"]
                    break
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("reasoning_content"):
                        feed("reasoning", d["reasoning_content"])
                    if d.get("content"):
                        feed("message", d["content"])
                    for tc in d.get("tool_calls") or []:
                        i = tc.get("index", 0)
                        p = pending.setdefault(i, {"id": None, "function": {"name": "", "arguments": ""}})
                        p["id"] = tc.get("id") or p["id"]
                        f = tc.get("function") or {}
                        p["function"]["name"] += f.get("name") or ""
                        p["function"]["arguments"] += f.get("arguments") or ""
                    if ch.get("finish_reason"):
                        finish[0] = ch["finish_reason"]
        except OSError as e:
            failed = {"message": "the model server stopped answering: %s" % e}
        finally:
            conn.close()
        try:
            close_item()
            for i in sorted(pending):
                tool_call(pending[i])
            u = {"input_tokens": int(usage.get("prompt_tokens", 0)),
                 "input_tokens_details": {"cached_tokens": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))},
                 "output_tokens": int(usage.get("completion_tokens", 0)),
                 "output_tokens_details": {"reasoning_tokens": int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))},
                 "total_tokens": int(usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)))}
            final = base("completed")
            final.update(output=items, usage=u)
            if failed:
                final.update(status="failed", error={"code": failed.get("code") or "server_error", "message": failed.get("message") or "error"})
                emit("response.failed", response=final)
            else:
                if finish[0] == "length":
                    final.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
                emit("response.completed", response=final)
            if not stream:
                self.send_json(200, final)
        except OSError:
            pass                                      # the client went away


def main():
    ap = argparse.ArgumentParser(description="OpenAI Responses API in front of a chat-completions server (YASAKANI)")
    ap.add_argument("--upstream", default=os.environ.get("ADAPTER_UPSTREAM", "http://127.0.0.1:8766/v1"), help="the chat-completions server")
    ap.add_argument("--upstream-key", default=os.environ.get("ADAPTER_UPSTREAM_KEY", ""))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--effort", default=os.environ.get("ADAPTER_EFFORT", ""),
                    help="thinking: off, low, medium, high or xhigh for every request (default: what the client asks for, else the server's default)")
    ap.add_argument("--thinking-budget", type=int, default=int(os.environ.get("ADAPTER_THINKING_BUDGET", "0")),
                    help="close the thinking after this many tokens (0 = no limit)")
    ap.add_argument("--max-tokens", type=int, default=int(os.environ.get("ADAPTER_MAX_TOKENS", "0")), help="output limit when the client gives none")
    ap.add_argument("--dump", default="", help="append every request (both forms) to this JSONL file")
    ap.add_argument("--verbose", action="store_true")
    cfg = ap.parse_args()
    srv = Adapter((cfg.host, cfg.port), cfg)
    print("responses adapter: http://%s:%d/v1 -> %s" % (cfg.host, cfg.port, cfg.upstream), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

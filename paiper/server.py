"""The paiper server: one local server for every document below the configured roots.

  /                          all documents, with open review items
  /d/<root>/<path>/          a document (rendered on every request, so it is always current)
  /d/<root>/<path>/<file>    files next to it (photos, PDF, ...)
  /_paiper/rt/<file>         runtime (css, js)
  /_paiper/themes/<name>/... theme files
  /api/comments?doc=<id>     GET list, POST add;  /api/comments/<id>  PUT, DELETE
  /api/edit?doc=<id>         POST {cid, before, after};  /api/edit/<id>  DELETE = undo
Security: binds to 127.0.0.1 only; refuses requests whose Host is not localhost (DNS rebinding);
writes need a JSON body and, when the browser sends one, a localhost Origin (no cross-site writes);
files are served only from inside document folders, never hidden files; a Content-Security-Policy keeps
pages from loading or sending anything outside this machine (config "remote_content": true lifts that).
"""
import html
import json
import mimetypes
import os
import re
import sys
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import config, store
from .doc import Doc

mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/otf", ".otf")
mimetypes.add_type("text/javascript", ".js")


def doc_url(docid):
    return "/d/" + quote(docid) + "/"


def home_page(cfg):
    rows = {}
    for d in config.find_docs(cfg):
        try:
            doc = Doc(d, cfg)
        except Exception as e:  # noqa: BLE001
            print("skip", d, e, file=sys.stderr)
            continue
        group = (os.path.dirname(doc.id.split("/", 1)[1]) if "/" in doc.id else "") or doc.id.split("/", 1)[0]
        try:
            mtime = max(os.path.getmtime(doc.content_path), os.path.getmtime(os.path.join(d, "paiper.json")))
        except OSError:
            mtime = 0
        rows.setdefault(group, []).append((doc, mtime))
    esc = html.escape
    out = []
    for g in sorted(rows):
        out.append(f'<h2>{esc(g)}</h2><div class="list">')
        for doc, mt in sorted(rows[g], key=lambda x: -x[1]):
            n = doc.open_count()
            out.append(f'<a class="item" href="{doc_url(doc.id)}"><span class="t">{esc(doc.title)}</span>'
                       f'<span class="ty">{esc(doc.type)} · {esc(doc.theme.name)}</span>'
                       f'<span class="c{" on" if n else ""}">{n if n else ""}{" open" if n else ""}</span>'
                       f'<span class="d">{time.strftime("%d %b %Y", time.localtime(mt)) if mt else ""}</span></a>')
        out.append("</div>")
    if not rows:
        out.append('<p class="none">No documents yet. A document is any folder with a paiper.json below: '
                   + ", ".join(esc(v) for v in cfg["roots"].values()) + "</p>")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>paiper</title><link rel="stylesheet" href="/_paiper/rt/ui.css"><link rel="stylesheet" href="/_paiper/rt/home.css"></head>
<body><header><b>p<span>ai</span>per</b><span>documents</span></header><main>{''.join(out)}</main></body></html>"""


# Documents are free HTML and may run scripts. This keeps a document from fetching code from, or sending
# what it can read to, another machine. Inline scripts and styles stay allowed: that is what documents are.
CSP_LOCAL = ("default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; object-src 'none'; base-uri 'self'; "
             "form-action 'self'; frame-ancestors 'self'")
CSP_OPEN = "object-src 'none'; base-uri 'self'; frame-ancestors 'self'"

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _hostname(value):
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def in_doc_folder(path, root):
    """True when `path` lies inside a folder (or below one) that has a paiper.json, within `root`."""
    d = os.path.dirname(path)
    while d == root or d.startswith(root + os.sep):
        if os.path.isfile(os.path.join(d, "paiper.json")):
            return True
        if d == root:
            break
        d = os.path.dirname(d)
    return False


class Handler(SimpleHTTPRequestHandler):
    cfg = None
    server_version = "paiper"
    sys_version = ""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP_OPEN if (self.cfg or {}).get("remote_content") else CSP_LOCAL)
        super().end_headers()

    def _host_ok(self):
        if _hostname(self.headers.get("Host")) in LOCAL_HOSTS:
            return True
        self._send(403, "paiper only answers on localhost", "text/plain")
        return False

    def _write_ok(self):
        """Block cross-site writes: JSON only (forces a CORS preflight, which we never grant) and localhost Origin."""
        if not self._host_ok():
            return False
        origin = self.headers.get("Origin")
        if origin and _hostname(urlparse(origin).netloc) not in LOCAL_HOSTS:
            self._json(403, {"error": "cross-origin request refused"})
            return False
        if self.command in ("POST", "PUT") and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json(415, {"error": "application/json required"})
            return False
        return True

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]):
            sys.stderr.write("[api] " + fmt % args + "\n")

    # ---------------------------------------------------------------- helpers
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _file(self, p):
        if not os.path.isfile(p):
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        size = os.path.getsize(p)
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        with open(p, "rb") as f:
            if m and (m.group(1) or m.group(2)) and size:   # video seeking
                if m.group(1):
                    a, b = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
                else:                                       # "bytes=-N": the last N bytes
                    a, b = max(0, size - int(m.group(2))), size - 1
                if a > b:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                f.seek(a)
                data = f.read(b - a + 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            else:
                data = f.read()
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _body(self):
        try:
            n = max(0, min(int(self.headers.get("Content-Length") or 0), 5_000_000))
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    def _doc_from_query(self, q):
        docid = (q.get("doc") or [""])[0]
        p = config.resolve_id(docid, self.cfg) if docid else None
        if not p or not os.path.isfile(os.path.join(p, "paiper.json")):
            return None
        return Doc(p, self.cfg)

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        if not self._host_ok():
            return
        Handler.cfg = config.load()
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        if path == "/":
            return self._send(200, home_page(self.cfg))
        if path.startswith("/_paiper/rt/"):
            return self._file(os.path.join(config.RUNTIME, os.path.basename(path)))
        if path.startswith("/_paiper/themes/"):
            name, _, rest = path[len("/_paiper/themes/"):].partition("/")
            try:
                tdir = config.find_theme(name, self.cfg)
            except FileNotFoundError:
                return self._send(404, "no theme", "text/plain")
            tdir = os.path.realpath(tdir)
            p = os.path.realpath(os.path.join(tdir, rest))
            return self._file(p) if p.startswith(tdir + os.sep) else self._send(403, "no", "text/plain")
        if path == "/api/comments":
            doc = self._doc_from_query(q)
            return self._json(200, store.load(doc.dir)) if doc else self._json(404, {"error": "no such document"})
        if path.startswith("/d/"):
            p = config.resolve_id(path[3:].rstrip("/"), self.cfg)
            if not p:
                return self._send(404, "not found", "text/plain")
            root = self.cfg["roots"][path[3:].split("/", 1)[0]]
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, "paiper.json")):
                if not path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", quote(path) + "/")
                    self.end_headers()
                    return
                try:
                    return self._send(200, Doc(p, self.cfg).render("server"))
                except Exception as e:  # noqa: BLE001
                    return self._send(500, f"<pre>{html.escape(repr(e))}</pre>")
            hidden = any(part.startswith(".") for part in os.path.relpath(p, root).split(os.sep))      # .env, .git, ...
            if hidden or not in_doc_folder(p, root) or os.path.basename(p) in ("comments.json", "paiper.json"):
                return self._send(404, "not found", "text/plain")
            return self._file(p)
        return self._send(404, "not found", "text/plain")

    # ---------------------------------------------------------------- writes
    def do_POST(self):
        if not self._write_ok():
            return
        Handler.cfg = config.load()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        doc = self._doc_from_query(q)
        if not doc:
            return self._json(404, {"error": "no such document"})
        d = self._body()
        if u.path == "/api/comments":
            text = (d.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "empty"})
            c = store.add(doc.dir, kind="comment", cid=d.get("cid") or "", section=d.get("section") or "",
                          quote=d.get("quote") or "", text=text, author=d.get("author") or self.cfg.get("author", ""))
            return self._json(201, c)
        if u.path == "/api/edit":
            try:
                with store.LOCK:
                    new_cid, before, after = doc.apply_edit(d.get("cid", ""), d.get("before", ""), d.get("after", ""))
                    c = store.add(doc.dir, kind="edit", cid=new_cid, before=before, after=after,
                                  quote=after[:200], author=d.get("author") or self.cfg.get("author", ""),
                                  status="open" if doc.built else "applied")
            except (LookupError, ValueError) as e:
                return self._json(409, {"error": str(e)})
            return self._json(201, {"cid": new_cid, "after": after, "item": c})
        return self._json(404, {"error": "not found"})

    def do_PUT(self):
        if not self._write_ok():
            return
        Handler.cfg = config.load()
        u = urlparse(self.path)
        doc = self._doc_from_query(parse_qs(u.query))
        if not doc or not u.path.startswith("/api/comments/"):
            return self._json(404, {"error": "not found"})
        d = self._body()
        c = store.update(doc.dir, u.path.rsplit("/", 1)[-1], **{k: v for k, v in d.items() if k in ("text",)})
        return self._json(200 if c else 404, c or {"error": "not found"})

    def do_DELETE(self):
        if not self._write_ok():
            return
        Handler.cfg = config.load()
        u = urlparse(self.path)
        doc = self._doc_from_query(parse_qs(u.query))
        if not doc:
            return self._json(404, {"error": "no such document"})
        iid = u.path.rsplit("/", 1)[-1]
        if u.path.startswith("/api/comments/"):
            c = store.get(doc.dir, iid)
            if c and c["status"] == "open" and c["kind"] == "comment":
                store.remove(doc.dir, iid)
            return self._json(200, {"ok": True})
        if u.path.startswith("/api/edit/"):
            with store.LOCK:
                c = store.get(doc.dir, iid)
                if not c or c["kind"] != "edit" or c["status"] == "processed":
                    return self._json(409, {"error": "this edit is already carried into the source"})
                if not doc.revert_edit(c):
                    return self._json(409, {"error": "the text changed since; ask Claude to undo it"})
                store.remove(doc.dir, iid)
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


def serve(port=None):
    cfg = config.load()
    Handler.cfg = cfg
    port = port or cfg["port"]
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"paiper on http://localhost:{port}/  roots: {', '.join(cfg['roots'].values()) or '(none configured)'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

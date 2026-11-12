"""paiper MCP server (stdio, no dependencies), so an agent (Claude Code, Claude desktop, Codex, Cursor, ...) can work
with paiper documents: find them, read what the reviewer left, answer it, write content, build and export.

Register:  claude mcp add --scope user paiper -- python3 <repo>/bin/paiper mcp
Desktop:   ~/Library/Application Support/Claude/claude_desktop_config.json, "mcpServers": {"paiper": {...}}
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

from . import __version__, cli, config, store
from .doc import Doc

DOC = {"type": "string", "description": "Document folder (absolute path) or its id as shown by list_documents, e.g. 'docs/projects/report'"}
TOOLS = [
    {"name": "list_documents", "description": "All paiper documents (reports) on this machine with their folder, local URL and number of open review items.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "review_items", "description": "Comments and in-browser text edits the reviewer left on a document. Open items are the ones to process; "
     "edits to generated documents (status open) must be carried into the build script before the next build.",
     "inputSchema": {"type": "object", "properties": {"doc": DOC, "all": {"type": "boolean", "description": "include processed items"}}, "required": ["doc"]}},
    {"name": "reply_item", "description": "Answer a review item and mark it processed (after the change is made). The reply shows in the document's review panel.",
     "inputSchema": {"type": "object", "properties": {"doc": DOC, "id": {"type": "string"}, "text": {"type": "string"},
                                                      "keep_open": {"type": "boolean"}}, "required": ["doc", "id", "text"]}},
    {"name": "read_document", "description": "paiper.json and the content HTML of a document.",
     "inputSchema": {"type": "object", "properties": {"doc": DOC}, "required": ["doc"]}},
    {"name": "write_content", "description": "Replace the content HTML of a hand-written document (one without a build script). Free HTML: "
     "<section>s with <h1 id> / <h2 id>, paragraphs, tables, figures, any SVG or illustration.",
     "inputSchema": {"type": "object", "properties": {"doc": DOC, "html": {"type": "string"}}, "required": ["doc", "html"]}},
    {"name": "new_document", "description": "Create a new report folder with paiper.json and a starter content.html.",
     "inputSchema": {"type": "object", "properties": {"folder": {"type": "string", "description": "absolute path of the new folder"},
                                                      "title": {"type": "string"}, "theme": {"type": "string", "description": "theme name, see list_themes; default 'default'"},
                                                      "subtitle": {"type": "string"}, "kicker": {"type": "string"},
                                                      "org": {"type": "string"}, "classification": {"type": "string"},
                                                      "page": {"type": "string", "enum": ["a4", "letter"], "description": "page size; default from the config"}},
                     "required": ["folder", "title"]}},
    {"name": "build", "description": "Run a generated document's build script. Refuses while browser edits are open (carry them in first).",
     "inputSchema": {"type": "object", "properties": {"doc": DOC}, "required": ["doc"]}},
    {"name": "export_pdf", "description": "Write the document's PDF next to it (WeasyPrint). Returns the path.",
     "inputSchema": {"type": "object", "properties": {"doc": DOC}, "required": ["doc"]}},
    {"name": "export_docx", "description": "Write the document as a native Word file (.docx) next to it: theme styles, real lists and tables, "
     "embedded pictures and charts, cover, contents field, header and footer. Returns the path.",
     "inputSchema": {"type": "object", "properties": {"doc": DOC, "out": {"type": "string", "description": "optional output path (.docx)"}}, "required": ["doc"]}},
    {"name": "list_themes", "description": "Available house styles (built-in and private).",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _folder(ref):
    if os.path.isabs(ref):
        return ref
    p = config.resolve_id(ref)
    if not p:
        raise ValueError(f"unknown document {ref!r}")
    return p


def _cli(*argv):
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            cli.main(list(argv))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise RuntimeError(str(e.code) + "\n" + buf.getvalue()) from None
    return buf.getvalue().strip()


def call(name, a):
    cfg = config.load()
    if name == "list_documents":
        return _cli("list")
    if name == "list_themes":
        return _cli("themes")
    if name == "new_document":
        args = ["new", a["folder"], "--title", a["title"], "--theme", a.get("theme") or "default"]
        for k in ("subtitle", "kicker", "org", "classification", "page"):
            if a.get(k):
                args += [f"--{k}", a[k]]
        return _cli(*args)
    d = Doc(_folder(a["doc"]), cfg)
    if name == "review_items":
        items = store.load(d.dir) if a.get("all") else store.open_items(d.dir)
        return json.dumps(items, indent=1, ensure_ascii=False) if items else "nothing open"
    if name == "reply_item":
        c = store.update(d.dir, a["id"], reply=a["text"], status="open" if a.get("keep_open") else "processed")
        return "ok" if c else f"no item {a['id']}"
    if name == "read_document":
        return "paiper.json:\n" + json.dumps(d.meta, indent=1, ensure_ascii=False) + "\n\ncontent:\n" + d.content()
    if name == "write_content":
        if d.built:
            raise RuntimeError("generated document: change its build script instead, then build")
        with open(d.content_path, "w", encoding="utf-8") as f:
            f.write(a["html"])
        return "written; " + _cli("url", d.dir)
    if name == "build":
        return _cli("build", d.dir)
    if name == "export_pdf":
        return _cli("pdf", d.dir)
    if name == "export_docx":
        return _cli("docx", d.dir, *([a["out"]] if a.get("out") else []))
    raise ValueError(f"unknown tool {name}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue                                   # notifications
        if method == "initialize":
            res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "paiper", "version": __version__},
                   "instructions": f"paiper documents: reports reviewed in the browser at http://localhost:{config.load()['port']}/. "
                                   "The user reviews (comments, direct text edits); you process: review_items, change content "
                                   "or the build script, build, reply_item. Content is free HTML: draw any chart or illustration you need."}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params", {})
            try:
                res = {"content": [{"type": "text", "text": call(p.get("name"), p.get("arguments") or {})}]}
            except Exception as e:  # noqa: BLE001
                res = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        elif method == "ping":
            res = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

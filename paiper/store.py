"""Review items of a document, in <doc>/comments.json.

Each item:
  id, kind ("comment" | "edit"), cid (block anchor), section, quote, text, author, created,
  status: "open"      waiting for Claude (a comment, or an edit to a generated document that still
                      has to be carried into its build script)
          "applied"   an edit to a hand-written document: already in the source, nothing to do
          "processed" handled by Claude; `reply` says what was done
  reply, and for edits: before, after (inner HTML of the block)
Older files without "kind" are comments.
"""
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager

try:
    import fcntl
except ImportError:          # no flock here: the in-process lock still holds
    fcntl = None

LOCK = threading.RLock()


@contextmanager
def locked(folder):
    """One writer at a time, also across processes: the server and an agent's `paiper reply` share this file."""
    with LOCK:
        if not fcntl:
            yield
            return
        name = "paiper-" + hashlib.md5(os.path.realpath(folder).encode()).hexdigest()[:16] + ".lock"
        with open(os.path.join(tempfile.gettempdir(), name), "w") as lf:          # not in the folder: no litter in your repo
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def path(folder):
    return os.path.join(folder, "comments.json")


def load(folder):
    try:
        with open(path(folder), encoding="utf-8") as f:
            items = json.load(f)
    except FileNotFoundError:
        return []
    for c in items:
        c.setdefault("kind", "comment")
        c.setdefault("reply", "")
        c.setdefault("author", "")
    return items


def save(folder, items):
    tmp = path(folder) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path(folder))


def add(folder, **fields):
    c = {"id": uuid.uuid4().hex[:8], "kind": "comment", "cid": "", "section": "", "quote": "", "text": "",
         "author": "", "created": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "open", "reply": ""}
    c.update({k: v for k, v in fields.items() if v is not None})
    c["quote"] = (c.get("quote") or "")[:400]
    with locked(folder):
        items = load(folder)
        items.append(c)
        save(folder, items)
    return c


def get(folder, cid):
    return next((c for c in load(folder) if c["id"] == cid), None)


def update(folder, cid, **fields):
    with locked(folder):
        items = load(folder)
        for c in items:
            if c["id"] == cid:
                c.update(fields)
                save(folder, items)
                return c
    return None


def remove(folder, cid):
    with locked(folder):
        items = load(folder)
        keep = [c for c in items if c["id"] != cid]
        save(folder, keep)
        return len(items) - len(keep)


def open_items(folder):
    return [c for c in load(folder) if c["status"] == "open"]

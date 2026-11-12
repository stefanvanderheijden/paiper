"""paiper configuration: ~/.config/paiper/config.json

{
  "port": 8760,
  "author": "Your name",
  "roots": {"notes": "/Users/me/Documents/notes"},          # folders scanned for documents (paiper.json)
  "theme_paths": ["/Users/me/Documents/notes/paiper-themes"],          # extra themes, e.g. private house styles
  "remote_content": false,                                  # true: documents may load things from the internet
  "page": "a4"                                              # default page size: "a4" or "letter" (a document can set its own)
}
"""
import copy
import json
import os
import re
import subprocess
import sys

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
RUNTIME = os.path.join(PKG, "runtime")
BUILTIN_THEMES = os.path.join(PKG, "themes")
CONFIG_PATH = os.path.expanduser(os.environ.get("PAIPER_CONFIG", "~/.config/paiper/config.json"))
DEFAULTS = {"port": 8760, "author": "", "roots": {}, "theme_paths": [], "remote_content": False, "page": "a4"}
SKIP_DIRS = {"node_modules", "__pycache__", "_kit", "_paiper", "venv", ".venv"}


PAGES = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}          # mm


def locale_page():
    """'letter' where US Letter is the usual paper (US, Canada, Mexico, ...), else 'a4'. Used once, by setup."""
    names = [os.environ.get(k, "") for k in ("LC_PAPER", "LC_ALL", "LANG")]
    if sys.platform == "darwin":
        try:
            names.insert(0, subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True, timeout=5).stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    for n in names:
        m = re.match(r"[a-z]{2,3}[_-]([A-Z]{2})", n or "")
        if m:
            return "letter" if m.group(1) in ("US", "CA", "MX", "PH", "CL", "CO", "VE", "GT", "CR", "PA", "DO", "PR") else "a4"
    return "a4"


def raw():
    """The config file as written (paths not expanded), over the defaults."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def load():
    cfg = raw()
    cfg["roots"] = {k: os.path.realpath(os.path.expanduser(v)) for k, v in cfg.get("roots", {}).items()}
    cfg["theme_paths"] = [os.path.realpath(os.path.expanduser(p)) for p in cfg.get("theme_paths", [])]
    return cfg


def save(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def theme_dirs(cfg=None):
    cfg = cfg or load()
    return [BUILTIN_THEMES] + cfg["theme_paths"]


def find_theme(name, cfg=None):
    for base in reversed(theme_dirs(cfg)):          # private themes win over built-in ones of the same name
        d = os.path.join(base, name)
        if os.path.isfile(os.path.join(d, "theme.json")):
            return d
    raise FileNotFoundError(f"theme '{name}' not found in {theme_dirs(cfg)}")


def list_themes(cfg=None):
    out = {}
    for base in theme_dirs(cfg):
        if os.path.isdir(base):
            for n in sorted(os.listdir(base)):
                if os.path.isfile(os.path.join(base, n, "theme.json")):
                    out[n] = os.path.join(base, n)
    return out


def doc_id(folder, cfg=None):
    """'<root name>/<path inside root>' for a document folder, or None when it is outside every root."""
    cfg = cfg or load()
    folder = os.path.realpath(folder)
    for name, root in cfg["roots"].items():
        if folder == root or folder.startswith(root + os.sep):
            rel = os.path.relpath(folder, root)
            return name if rel == "." else f"{name}/{rel}"
    return None


def resolve_id(docid, cfg=None):
    cfg = cfg or load()
    name, _, rel = docid.partition("/")
    root = cfg["roots"].get(name)
    if not root:
        return None
    p = os.path.realpath(os.path.join(root, rel))
    return p if (p == root or p.startswith(root + os.sep)) else None


def find_docs(cfg=None):
    """Every folder with a paiper.json below the configured roots."""
    cfg = cfg or load()
    out = []
    for name, root in cfg["roots"].items():
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS]
            if "paiper.json" in files:
                out.append(d)
    return out

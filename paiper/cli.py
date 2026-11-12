"""paiper command line. Meant to be run by an AI agent on the user's instructions (see AGENTS.md).

  paiper list                              all documents, open review items
  paiper new <folder> --title T [--theme default] [--org O] [--classification C] [--page a4|letter]
  paiper url <folder>                      the address of a document on the local server
  paiper review <folder> [--all] [--json]  open comments and edits (what to process)
  paiper reply <folder> <id> <text>        mark an item processed with a reply
  paiper build <folder> [--force]          run the document's build script (refuses while edits are open)
  paiper pdf <folder> [out.pdf]            PDF (A4 or Letter) via WeasyPrint
  paiper docx <folder> [out.docx] [--fast] native Word document (--fast: skip the LibreOffice pass for TOC page numbers)
  paiper export <folder> <outdir>          self-contained static copy (for sharing or publishing)
  paiper themes                            available themes
  paiper mcp                               MCP server on stdio (Claude Code / Claude desktop app)
  paiper setup [--root N=P] [--author A]   one-time setup after cloning (idempotent); --no-service, --no-agents
  paiper doctor                            what is installed and working (exit 1 on a problem)
  paiper update                            git pull this checkout and restart the server
  paiper uninstall                         undo setup (service, launcher, agent registrations); documents stay
  paiper serve [--port N]                  run the server in the foreground
  paiper service install|uninstall|status|restart   always-on server (launchd on macOS, systemd on Linux)
  paiper config [--root NAME=PATH] [--remove-root NAME] [--theme-path P] [--author A] [--port N] [--remote-content on|off] [--page a4|letter]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape as xml_escape

from . import config, store
from .doc import Doc, strip_tags

LABEL = "com.paiper.server"
PY = shutil.which("python3") or sys.executable          # stable path (survives Python upgrades), not the versioned binary


def launch_cmd():
    """How to start paiper: the checkout's launcher when run from a clone, else the installed module."""
    launcher = os.path.join(config.REPO, "bin", "paiper")
    if os.path.exists(launcher):
        return [PY, launcher]
    return [sys.executable, "-m", "paiper"]
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _url(doc, cfg):
    from .server import doc_url
    if not doc.id:
        return f"(outside the configured roots: add one with `paiper config --root NAME={os.path.dirname(doc.dir)}`)"
    return f"http://localhost:{cfg['port']}{doc_url(doc.id)}"


def cmd_list(a, cfg):
    for d in sorted(config.find_docs(cfg)):
        try:
            doc = Doc(d, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"!! {d}: {e}")
            continue
        n = doc.open_count()
        print(f"{doc.title}  [{doc.type}, {doc.theme.name}]{f'  {n} open' if n else ''}\n    {d}\n    {_url(doc, cfg)}")


def cmd_new(a, cfg):
    os.makedirs(a.folder, exist_ok=True)
    pj = os.path.join(a.folder, "paiper.json")
    if os.path.exists(pj):
        sys.exit(f"{pj} exists")
    title = a.title or os.path.basename(os.path.abspath(a.folder))
    meta = {"type": "report", "title": title, "theme": a.theme, "content": "content.html", "toc": True,
            "cover": {"kicker": a.kicker or "", "title": title, "subtitle": a.subtitle or ""},
            "meta": [["Version", "0.1"], ["Date", __import__("time").strftime("%d-%m-%Y")]]}
    if cfg.get("author"):
        meta["meta"].insert(0, ["Prepared by", cfg["author"]])
    if a.org:
        meta["org"] = a.org
    if a.classification:
        meta["classification"] = a.classification
    if a.page:
        meta["page"] = a.page
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    cp = os.path.join(a.folder, "content.html")
    if not os.path.exists(cp):
        with open(cp, "w", encoding="utf-8") as f:
            f.write('<section>\n<h1 id="summary"><span class="n">1.</span>Summary</h1>\n<p>Start writing here.</p>\n</section>\n')
    doc = Doc(a.folder, cfg)
    print(f"created {pj}\n{_url(doc, cfg)}")


def cmd_url(a, cfg):
    print(_url(Doc(a.folder, cfg), cfg))


def cmd_review(a, cfg):
    doc = Doc(a.folder, cfg)
    items = store.load(doc.dir) if a.all else store.open_items(doc.dir)
    if a.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return
    if not items:
        print("nothing open")
    for c in items:
        head = f"[{c['id']}] {c['kind'].upper()} {c['status']}  {c.get('author') or ''} {c['created']}  cid={c.get('cid')}"
        if c["kind"] == "edit":
            body = f"   before: {strip_tags(c['before'])}\n   after:  {strip_tags(c['after'])}"
        else:
            body = (f"   on: \"{c['quote']}\"\n" if c.get("quote") else "") + f"   {c['text']}"
        print(head + "\n" + body + (f"\n   reply: {c['reply']}" if c.get("reply") else "") + "\n")


def cmd_reply(a, cfg):
    doc = Doc(a.folder, cfg)
    c = store.update(doc.dir, a.id, reply=a.text, status="open" if a.keep_open else "processed")
    print("ok" if c else f"no item {a.id}")


def cmd_build(a, cfg):
    doc = Doc(a.folder, cfg)
    if not doc.built:
        print("hand-written document: nothing to build (the server renders content.html on every request)")
        return
    edits = [c for c in store.open_items(doc.dir) if c["kind"] == "edit"]
    if edits and not a.force:
        sys.exit(f"{len(edits)} open edit(s) made in the browser are not in the build script yet; carry them over "
                 f"(paiper review), mark them processed (paiper reply), then build. --force discards them.")
    env = dict(os.environ, PYTHONPATH=config.REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run(doc.meta["build"], shell=True, cwd=doc.dir, env=env)
    if r.returncode:
        sys.exit(r.returncode)
    print(_url(doc, cfg))


def _pkg_manager():
    return next((m for m in ("brew", "apt-get", "dnf", "pacman", "zypper") if shutil.which(m)), None)


# optional tools: what they are for, and the package that provides them per package manager
TOOLS = {
    "weasyprint": ("PDF export", {"brew": "weasyprint", "apt-get": "weasyprint", "dnf": "weasyprint", "pacman": "python-weasyprint", "zypper": "python3-weasyprint"}),
    "rsvg-convert": ("charts in Word export (without it they become a placeholder)",
                     {"brew": "librsvg", "apt-get": "librsvg2-bin", "dnf": "librsvg2-tools", "pacman": "librsvg", "zypper": "rsvg-convert"}),
    "soffice": ("exact contents page numbers in Word export (estimated without it)",
                {"brew": "--cask libreoffice", "apt-get": "libreoffice-writer", "dnf": "libreoffice-writer", "pacman": "libreoffice-fresh", "zypper": "libreoffice-writer"}),
    "pdfinfo": ("same: reads the page numbers LibreOffice produced",
                {"brew": "poppler", "apt-get": "poppler-utils", "dnf": "poppler-utils", "pacman": "poppler", "zypper": "poppler-tools"}),
}


def find_tool(name):
    if name == "weasyprint":
        return weasyprint()
    if name == "soffice":
        return shutil.which("soffice") or shutil.which("libreoffice") or next(
            (p for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",) if os.path.exists(p)), None)
    return shutil.which(name)


def install_hint(names):
    """One command that installs the given optional tools on this machine, or None when we cannot tell."""
    pm = _pkg_manager()
    if not pm or not names:
        return "pipx install weasyprint" if names == ["weasyprint"] else None
    pkgs = [TOOLS[n][1][pm] for n in names]
    if pm == "brew":
        casks = [p.split()[-1] for p in pkgs if p.startswith("--cask")]
        plain = [p for p in pkgs if not p.startswith("--cask")]
        return " && ".join(x for x in (("brew install " + " ".join(plain)) if plain else "", ("brew install --cask " + " ".join(casks)) if casks else "") if x)
    verb = {"apt-get": "sudo apt-get install -y", "dnf": "sudo dnf install -y", "pacman": "sudo pacman -S --noconfirm", "zypper": "sudo zypper install -y"}[pm]
    return verb + " " + " ".join(pkgs)


def missing_tools():
    return [n for n in TOOLS if not find_tool(n)]


def weasyprint():
    return (os.environ.get("WEASYPRINT") or shutil.which("weasyprint")
            or next((p for p in ("/opt/homebrew/bin/weasyprint", "/usr/local/bin/weasyprint") if os.path.exists(p)), None))


def cmd_pdf(a, cfg):
    doc = Doc(a.folder, cfg)
    wp = weasyprint()
    if not wp:
        sys.exit("PDF export needs WeasyPrint. Install it with: " + (install_hint(["weasyprint"]) or "pipx install weasyprint"))
    tmp = os.path.join(doc.dir, ".paiper-print.html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc.render("print"))
    out = a.out or os.path.join(doc.dir, doc.pdf_name())
    try:
        r = subprocess.run([wp, "-q", tmp, out], capture_output=True, text=True)
    finally:
        os.remove(tmp)
    if r.returncode:
        sys.exit(r.stderr[-2000:])
    print(out)


def cmd_docx(a, cfg):
    from .docx import export
    out, notes = export(a.folder, a.out, cfg, paginate=not a.fast)
    for n in notes:
        print("note: " + n, file=sys.stderr)
    print(out)


def cmd_export(a, cfg):
    doc = Doc(a.folder, cfg)
    out = os.path.abspath(a.outdir)
    os.makedirs(out, exist_ok=True)
    for n in os.listdir(doc.dir):                     # the document's own files
        if n.startswith(".") or n in ("comments.json", "paiper.json", "__pycache__"):
            continue
        s, t = os.path.join(doc.dir, n), os.path.join(out, n)
        if os.path.isdir(s):
            shutil.copytree(s, t, dirs_exist_ok=True)
        elif not n.endswith((".py", ".csv")) or a.all:
            shutil.copy2(s, t)
    shutil.copytree(config.RUNTIME, os.path.join(out, "_paiper", "rt"), dirs_exist_ok=True)
    shutil.copytree(doc.theme.dir, os.path.join(out, "_paiper", "theme"), dirs_exist_ok=True)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(doc.render("export", editable=False))
    print(os.path.join(out, "index.html"))


def cmd_themes(a, cfg):
    for n, d in config.list_themes(cfg).items():
        try:
            with open(os.path.join(d, "theme.json"), encoding="utf-8") as f:
                desc = json.load(f).get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        print(f"{n:16} {d}\n{'':16} {desc}")


def cmd_serve(a, cfg):
    from .server import serve
    serve(a.port)


UNIT = os.path.expanduser("~/.config/systemd/user/paiper.service")


def cmd_service(a, cfg):
    """Keep the server running: launchd on macOS, a systemd user unit on Linux."""
    if sys.platform == "darwin":
        _launchd(a.action, launch_cmd(), cfg)
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        _systemd(a.action, launch_cmd(), cfg)
    else:
        print("no service manager supported here; run `paiper serve` (e.g. from your startup programs)")


def _launchd(action, launcher, cfg):
    if action == "install":
        log = os.path.expanduser("~/Library/Logs/paiper.log")
        args = "".join("<string>%s</string>" % xml_escape(x) for x in launcher + ["serve"])
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
<key>StandardOutPath</key><string>{xml_escape(log)}</string><key>StandardErrorPath</key><string>{xml_escape(log)}</string>
</dict></plist>
"""
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
        with open(PLIST, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", "-w", PLIST], check=True)
        print(f"installed; http://localhost:{cfg['port']}/  (log: {log})")
    elif action == "uninstall":
        subprocess.run(["launchctl", "unload", "-w", PLIST], capture_output=True)
        if os.path.exists(PLIST):
            os.remove(PLIST)
        print("service removed")
    elif action == "restart":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], check=False)
        print("restarted")
    else:
        r = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
        print(r.stdout or "not installed")


def _systemd(action, launcher, cfg):
    sc = ["systemctl", "--user"]
    if action == "install":
        os.makedirs(os.path.dirname(UNIT), exist_ok=True)
        exe = " ".join('"%s"' % x.replace('"', '\\"') for x in launcher)          # paths may hold spaces
        with open(UNIT, "w") as f:
            f.write(f"[Unit]\nDescription=paiper document server\n\n[Service]\nExecStart={exe} serve\n"
                    f"Restart=on-failure\n\n[Install]\nWantedBy=default.target\n")
        subprocess.run(sc + ["daemon-reload"], check=False)
        subprocess.run(sc + ["enable", "--now", "paiper.service"], check=False)
        subprocess.run(sc + ["restart", "paiper.service"], check=False)
        print(f"installed; http://localhost:{cfg['port']}/  (journalctl --user -u paiper)")
    elif action == "uninstall":
        subprocess.run(sc + ["disable", "--now", "paiper.service"], check=False)
        if os.path.exists(UNIT):
            os.remove(UNIT)
        print("service removed")
    elif action == "restart":
        subprocess.run(sc + ["restart", "paiper.service"], check=False)
        print("restarted")
    else:
        subprocess.run(sc + ["status", "--no-pager", "paiper.service"], check=False)


def server_up(cfg, wait=0.0):
    """True when a paiper server answers on the configured port (waits up to `wait` seconds for it)."""
    import time
    import urllib.error
    import urllib.request
    end = time.time() + wait
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cfg['port']}/", timeout=2) as r:
                return "paiper" in (r.headers.get("Server") or "")
        except urllib.error.HTTPError:
            return False                                   # something else has the port
        except OSError:
            if time.time() >= end:
                return False
            time.sleep(0.3)


# ------------------------------------------------------------------ agents: where each one keeps its MCP servers
SKILL_SRC = os.path.join(config.REPO, "skill", "paiper")
JSON_AGENTS = (("Claude desktop app", "~/Library/Application Support/Claude/claude_desktop_config.json" if sys.platform == "darwin"
                else "~/.config/Claude/claude_desktop_config.json"),
               ("Cursor", "~/.cursor/mcp.json"), ("Gemini CLI", "~/.gemini/settings.json"))
SKILL_LINKS = ("~/.claude/skills/paiper", "~/.codex/skills/paiper")
CODEX_BLOCK = re.compile(r"\n*\[mcp_servers\.paiper\]\n(?:(?!\[)[^\n]*\n?)*")


def _json_entry(path, entry):
    """Set (or with entry=None remove) mcpServers.paiper in an agent's JSON config, leaving the rest alone.
    Returns a short result; a file that is not plain JSON is left untouched."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except ValueError:
        return "skipped, the file is not plain JSON (add the server by hand)"
    if not isinstance(data, dict):
        return "skipped, unexpected file content"
    servers = data.setdefault("mcpServers", {})
    if entry is None:
        if "paiper" not in servers:
            return None
        del servers["paiper"]
    elif servers.get("paiper") == entry:
        return "already registered"
    else:
        servers["paiper"] = entry
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return "removed" if entry is None else "registered"


def _link(src, dst):
    """Symlink dst -> src unless something that is not our link is already there."""
    dst = os.path.expanduser(dst)
    if not os.path.exists(src) or (os.path.exists(dst) and not os.path.islink(dst)):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.islink(dst):
        os.remove(dst)
    os.symlink(src, dst)
    return True


def register_agents(run, remove=False):
    """Register (or remove) the MCP server and the skill with every agent found on this machine."""
    done = []
    entry = None if remove else {"command": run[0], "args": run[1:] + ["mcp"]}
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", "--scope", "user", "paiper"], capture_output=True)
        if remove:
            done.append("Claude Code: MCP removed")
        else:
            r = subprocess.run([claude, "mcp", "add", "--scope", "user", "paiper", "--"] + run + ["mcp"], capture_output=True, text=True)
            done.append("Claude Code: MCP " + ("registered (user scope)" if r.returncode == 0 else "failed: " + (r.stderr or r.stdout).strip()[:200]))
    for name, path in JSON_AGENTS:
        path = os.path.expanduser(path)
        if os.path.isdir(os.path.dirname(path)):
            res = _json_entry(path, entry)
            if res:
                done.append(f"{name}: MCP {res} ({path})")
    toml = os.path.expanduser("~/.codex/config.toml")
    if os.path.isdir(os.path.dirname(toml)):
        cur = ""
        if os.path.exists(toml):
            with open(toml, encoding="utf-8") as f:
                cur = f.read()
        new = CODEX_BLOCK.sub("\n", cur)
        if not remove:
            new = new.rstrip("\n") + f"\n\n[mcp_servers.paiper]\ncommand = {json.dumps(run[0])}\nargs = {json.dumps(run[1:] + ['mcp'])}\n"
        if new.strip() != cur.strip():
            with open(toml + ".tmp", "w", encoding="utf-8") as f:
                f.write(new.lstrip("\n"))
            os.replace(toml + ".tmp", toml)
            done.append(f"Codex: MCP {'removed' if remove else 'registered'} ({toml})")
        elif not remove:
            done.append("Codex: MCP already registered")
    for dst in SKILL_LINKS:
        full = os.path.expanduser(dst)
        if remove:
            if os.path.islink(full):
                os.remove(full)
                done.append(f"skill removed: {dst}")
        elif os.path.isdir(os.path.dirname(os.path.dirname(full))) and _link(SKILL_SRC, dst):
            done.append(f"skill: {dst}")
    return done


def cmd_setup(a, cfg):
    """Everything to do after cloning, idempotent: launcher on PATH, config, server at login, and the MCP server
    and skill for the agents found here. Each step reports; one failing step does not stop the others."""
    launcher = os.path.join(config.REPO, "bin", "paiper")
    done = []

    def step(name, fn):
        try:
            r = fn()
            done.extend(r if isinstance(r, list) else [r] if r else [])
        except Exception as e:  # noqa: BLE001
            done.append(f"!! {name} failed: {e}")

    def link_launcher():
        if _link(launcher, "~/.local/bin/paiper"):
            on_path = os.path.expanduser("~/.local/bin") in os.environ.get("PATH", "").split(os.pathsep)
            return "launcher: ~/.local/bin/paiper" + ("" if on_path else f"  (not on PATH: call it as {launcher})")

    def write_config():
        raw = config.raw()
        for r in a.root or []:
            k, _, v = r.partition("=")
            if not v:
                raise ValueError(f"--root wants NAME=PATH, got {r!r}")
            raw["roots"][k] = os.path.expanduser(v)
        ex = os.path.realpath(os.path.join(config.REPO, "examples"))
        roots = [os.path.realpath(os.path.expanduser(r)) for r in raw["roots"].values()]
        if os.path.isdir(ex) and not any(ex == r or ex.startswith(r + os.sep) for r in roots):
            raw["roots"]["paiper-examples"] = ex               # only when no other root already contains the examples
        for p_ in a.theme_path or []:
            p_ = os.path.expanduser(p_)
            if p_ not in raw["theme_paths"]:
                raw["theme_paths"].append(p_)
        if a.author:
            raw["author"] = a.author
        if a.page:
            raw["page"] = a.page
        elif not os.path.exists(config.CONFIG_PATH):
            raw["page"] = config.locale_page()                 # first run: Letter in North America, A4 elsewhere
        config.save(raw)
        c = config.load()
        missing = [v for v in c["roots"].values() if not os.path.isdir(v)]
        return ([f"config: {config.CONFIG_PATH}  page: {c['page']}  roots: {', '.join(f'{k}={v}' for k, v in c['roots'].items())}"]
                + [f"!! root does not exist: {m}" for m in missing])

    def service():
        c = config.load()
        if server_up(c) and a.no_service:
            return f"server: already running, http://localhost:{c['port']}/"
        if a.no_service:
            return "server: not installed (--no-service); start it with `paiper serve`"
        cmd_service(argparse.Namespace(action="install"), c)
        if server_up(c, wait=8):
            return f"server: running, http://localhost:{c['port']}/"
        return (f"!! server: installed but not answering on port {c['port']}. Another program may hold the port "
                f"(`paiper config --port N`, then `paiper service install`), or see the log.")

    step("launcher", link_launcher)
    step("config", write_config)
    step("server", service)
    if not a.no_agents:
        step("agents", lambda: register_agents(launch_cmd()) or ["agents: none found; point yours at AGENTS.md, or run `paiper mcp` as an MCP server"])
    miss = missing_tools()
    if miss:
        done.append("optional tools missing: " + "; ".join(f"{n} ({TOOLS[n][0]})" for n in miss))
        done.append("  install them with: " + (install_hint(miss) or "your package manager (see README)"))
    else:
        done.append("optional tools: all present (PDF and Word export at full quality)")
    print("\n".join(done))
    if any(d.startswith("!!") for d in done):
        sys.exit(1)


def cmd_uninstall(a, cfg):
    """Undo setup: stop the server, remove the login service, the launcher link and the agent registrations.
    Documents, comments, themes and the config file are yours and stay."""
    cmd_service(argparse.Namespace(action="uninstall"), cfg)
    for line in register_agents(launch_cmd(), remove=True):
        print(line)
    link = os.path.expanduser("~/.local/bin/paiper")
    if os.path.islink(link):
        os.remove(link)
        print("launcher removed: ~/.local/bin/paiper")
    print(f"left in place: your documents, {config.CONFIG_PATH} and this checkout ({config.REPO}). Delete the last two to remove every trace.")


def cmd_doctor(a, cfg):
    """What is installed and working, for an agent to check after setup or when something is off. Exit 1 on a problem."""
    from . import __version__
    bad = []

    def line(ok, label, text):
        print(f"{'ok ' if ok else '!! '} {label:12} {text}")
        if not ok:
            bad.append(label)

    line(sys.version_info >= (3, 9), "python", f"{sys.version.split()[0]}  ({sys.executable})")
    line(True, "paiper", f"{__version__}  ({config.REPO})")
    line(os.path.exists(config.CONFIG_PATH), "config", config.CONFIG_PATH + ("" if os.path.exists(config.CONFIG_PATH) else "  missing: run `paiper setup`"))
    for k, v in cfg["roots"].items():
        line(os.path.isdir(v), "root", f"{k} = {v}" + ("" if os.path.isdir(v) else "  does not exist"))
    if not cfg["roots"]:
        line(False, "root", "none configured: `paiper config --root NAME=PATH`")
    line(True, "documents", str(len(config.find_docs(cfg))))
    line(True, "themes", ", ".join(config.list_themes(cfg)))
    up = server_up(cfg)
    line(up, "server", f"http://localhost:{cfg['port']}/" + ("" if up else "  not answering: `paiper service install` (or `paiper serve`)"))
    line(True, "remote", "documents may load from the internet" if cfg.get("remote_content") else "documents cannot load from or send to the internet (default)")
    line(True, "page", cfg["page"] + "  (default page size; a document can set its own)")
    for n, (what, _) in TOOLS.items():
        t = find_tool(n)
        print(f"{'ok ' if t else '-- '} {n:12} {t or 'not installed, optional: ' + what}")
    miss = missing_tools()
    if miss:
        print("\nInstall the optional tools with:\n  " + (install_hint(miss) or "your package manager: " + ", ".join(miss)))
    sys.exit(1 if bad else 0)


def cmd_update(a, cfg):
    """Pull the latest version of this checkout and restart the server."""
    if not os.path.isdir(os.path.join(config.REPO, ".git")):
        sys.exit("not a git checkout; update it the way you installed it")
    r = subprocess.run(["git", "-C", config.REPO, "pull", "--ff-only"], capture_output=True, text=True)
    print((r.stdout + r.stderr).strip())
    if r.returncode:
        sys.exit(r.returncode)
    cmd_service(argparse.Namespace(action="restart"), cfg)


def cmd_mcp(a, cfg):
    from .mcp import main as mcp_main
    mcp_main()


def cmd_config(a, cfg):
    raw = config.raw()
    for r in a.root or []:
        k, _, v = r.partition("=")
        if not v:
            sys.exit(f"--root wants NAME=PATH, got {r!r}")
        raw["roots"][k] = os.path.expanduser(v)
    for k in a.remove_root or []:
        raw["roots"].pop(k, None)
    for p in a.theme_path or []:
        p = os.path.expanduser(p)
        if p not in raw["theme_paths"]:
            raw["theme_paths"].append(p)
    if a.author is not None:
        raw["author"] = a.author
    if a.port:
        raw["port"] = a.port
    if a.remote_content:
        raw["remote_content"] = a.remote_content == "on"
    if a.page:
        raw["page"] = a.page
    if a.root or a.remove_root or a.theme_path or a.author is not None or a.port or a.remote_content or a.page:
        config.save(raw)
        if a.port:
            print("port changed: run `paiper service install` to restart the server on it")
    print(config.CONFIG_PATH)
    print(json.dumps(raw, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="paiper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, *args, **flags):
        """A subcommand with positional `args` and `flags` ({"--name": {argparse kwargs}})."""
        x = sp.add_parser(name)
        for arg in args:
            if arg.endswith("?"):
                x.add_argument(arg[:-1], nargs="?")
            else:
                x.add_argument(arg)
        for flag, kw in flags.items():
            x.add_argument(flag, **kw)
        return x

    store_true = {"action": "store_true"}
    many = {"action": "append"}
    cmd("list")
    page = {"choices": sorted(config.PAGES)}
    cmd("new", "folder", **{"--title": {}, "--theme": {"default": "default"}, "--kicker": {}, "--subtitle": {}, "--org": {}, "--classification": {}, "--page": page})
    cmd("url", "folder")
    cmd("review", "folder", **{"--all": store_true, "--json": store_true})
    cmd("reply", "folder", "id", "text", **{"--keep-open": store_true})
    cmd("build", "folder", **{"--force": store_true})
    cmd("pdf", "folder", "out?")
    cmd("docx", "folder", "out?", **{"--fast": store_true})
    cmd("export", "folder", "outdir", **{"--all": store_true})
    cmd("themes")
    cmd("mcp")
    cmd("setup", **{"--root": many, "--theme-path": many, "--author": {}, "--no-service": store_true, "--no-agents": store_true, "--page": page})
    cmd("uninstall")
    cmd("doctor")
    cmd("update")
    cmd("serve", **{"--port": {"type": int}})
    cmd("service", **{}).add_argument("action", choices=["install", "uninstall", "status", "restart"])
    cmd("config", **{"--root": many, "--remove-root": many, "--theme-path": many, "--author": {}, "--port": {"type": int},
                     "--remote-content": {"choices": ["on", "off"]}, "--page": page})
    a = p.parse_args(argv)
    cfg = config.load()
    globals()["cmd_" + a.cmd](a, cfg)


if __name__ == "__main__":
    main()

"""A paiper document: a folder with paiper.json and a content file.

paiper.json
{
  "type": "report",                       # report; deck and handout follow
  "page": "a4",                           # optional: "a4" or "letter" (default: the config's "page")
  "title": "Short name (top bar, home page, PDF name)",
  "theme": "default",                     # a theme name (built-in, or from config theme_paths)
  "content": "content.html",              # the body: free HTML, <section>s with <h1>/<h2>; any SVG/JS/img
  "build": "python3 build.py",            # optional: script that writes the content (generated documents)
  "css": "style.css",                     # optional: extra stylesheet for this document only
  "pdf": "Name.pdf",                      # optional: PDF file name
  "toc": true,                            # contents page after the cover
  "keep_headings": true,                  # a heading never starts in the bottom part of a page (false: let it flow)
                                          # in the content: data-running="text" on a heading puts that text in the page header from there on
  "cover": {"kicker": "", "title": "", "subtitle": ""},
  "meta": [["Version", "1.0"], ["Date", "..."]],     # rows on the cover
  "org": "", "classification": ""         # used in footer text and the cover
}
"""
import hashlib
import html
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from . import config, store
from .css import css_rules

TAGGED = ("h1", "h2", "h3", "p", "li", "table", "figure", "blockquote", "pre")
EDITABLE = ("h1", "h2", "h3", "p", "li")
OPEN_RE = re.compile(r"<(" + "|".join(TAGGED) + r")\b([^>]*)>", re.I)
RAW_RE = re.compile(r"<!--.*?-->|<(script|style|textarea)\b.*?</\1\s*>", re.S | re.I)      # text that only looks like markup


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def find_close(src, tag, pos):
    """Index of the '</tag>' that closes the element whose open tag ends at pos (nesting-aware)."""
    pat = re.compile(r"<(/?)" + tag + r"\b[^>]*>", re.I)
    depth = 1
    for m in pat.finditer(src, pos):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return m.start()
    return -1


def scan_blocks(src):
    """Blocks that get a comment anchor: list of dicts tag, cid, open_start, open_end, inner_end, editable."""
    blocks, seen = [], {}
    raw = [m.span() for m in RAW_RE.finditer(src)]          # never touch scripts, styles or comments
    for m in OPEN_RE.finditer(src):
        if any(a <= m.start() < b for a, b in raw):
            continue
        tag = m.group(1).lower()
        attrs = m.group(2)
        if attrs.rstrip().endswith("/"):
            continue
        end = find_close(src, tag, m.end())
        if end < 0:
            continue
        preset = re.search(r'data-cid="([^"]+)"', attrs)      # anchors written by a build script are kept
        if preset:
            cid = preset.group(1)
        else:
            key = strip_tags(src[m.end():end])[:160] or f"{tag}@{m.start()}"
            h = hashlib.md5(f"{tag}:{key}".encode()).hexdigest()[:6]
            seen[h] = seen.get(h, 0) + 1
            cid = h if seen[h] == 1 else f"{h}-{seen[h]}"
        blocks.append({"tag": tag, "cid": cid, "open_start": m.start(), "open_end": m.end(), "inner_end": end,
                       "editable": tag in EDITABLE, "preset": bool(preset)})
    return blocks


def tag_content(src):
    out, last = [], 0
    for b in scan_blocks(src):
        out.append(src[last:b["open_end"] - 1])
        out.append(("" if b["preset"] else f' data-cid="{b["cid"]}"') + (" data-editable" if b["editable"] else "") + ">")
        last = b["open_end"]
    out.append(src[last:])
    return "".join(out)


class _Clean(HTMLParser):
    """Keep inline formatting from contenteditable, drop everything else (styles, spans from paste, ...)."""
    KEEP = {"strong", "b", "em", "i", "u", "br", "a", "code", "sub", "sup", "span", "small", "del", "ins", "mark"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out, self.skip, self.stack = [], 0, []

    def handle_starttag(self, tag, attrs):
        if tag in ("button",):
            self.skip += 1
            return
        if self.skip or tag not in self.KEEP:
            return
        a = dict(attrs)
        if tag == "span" and a.get("class") not in ("n", "tag", "muted", "small"):
            self.stack.append(False)          # a bare span (from paste or the browser): keep its text, drop the tag
            return
        if tag == "span":
            self.stack.append(True)
        keep = []
        href = (a.get("href") or "").strip()
        if tag == "a" and re.match(r"^(https?:|mailto:|#|/|\.{0,2}/?[\w-])", href, re.I) and not re.match(r"^\s*javascript:", href, re.I):
            keep.append(f' href="{html.escape(href)}"')
        if tag == "span" and a.get("class") in ("n", "tag", "muted", "small"):
            keep.append(f' class="{a["class"]}"')
        self.out.append(f"<{tag}{''.join(keep)}>" if tag != "br" else "<br>")

    def handle_endtag(self, tag):
        if tag == "button":
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in self.KEEP or tag == "br":
            return
        if tag == "span" and not (self.stack.pop() if self.stack else False):
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)

    def handle_entityref(self, n):
        if not self.skip:
            self.out.append(" " if n == "nbsp" else f"&{n};")

    def handle_charref(self, n):
        if not self.skip:
            self.out.append(f"&#{n};")


def clean_inline(s):
    p = _Clean()
    p.feed(s.replace(" ", " "))
    p.close()
    out = "".join(p.out)
    out = re.sub(r"(<br>\s*)+$", "", out).strip()
    return out


def write_atomic(path, text):
    """Write through a temporary file, so a reader (or a crash) never sees half a document."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _svg(d):
    return ('<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{d}</svg>')


ICONS = {
    "minus": _svg('<path d="M6 12h12"/>'),
    "plus": _svg('<path d="M12 6v12M6 12h12"/>'),
    "fit": _svg('<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/>'),
    "comment": _svg('<path d="M20 12a8 8 0 0 1-11.5 7.2L4 20l.9-4.2A8 8 0 1 1 20 12z"/>'),
    "pencil": _svg('<path d="M15.5 5.5l3 3M4 20l1-4L16.5 4.5a2.1 2.1 0 0 1 3 3L8 19l-4 1z"/>'),
    "panel": _svg('<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M14.5 4.5v15"/>'),
    "download": _svg('<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14"/>'),
    "copy": _svg('<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>'),
}


class Theme:
    def __init__(self, name, cfg=None):
        self.dir = config.find_theme(name, cfg)
        self.name = os.path.basename(self.dir)
        with open(os.path.join(self.dir, "theme.json"), encoding="utf-8") as f:
            self.meta = json.load(f)
        cp = os.path.join(self.dir, "cover.html")
        self.cover = ""
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                self.cover = f.read()


class Doc:
    def __init__(self, folder, cfg=None):
        self.cfg = cfg or config.load()
        self.dir = os.path.realpath(folder)
        with open(os.path.join(self.dir, "paiper.json"), encoding="utf-8") as f:
            self.meta = json.load(f)
        self.type = self.meta.get("type", "report")
        self.title = self.meta.get("title") or os.path.basename(self.dir)
        self.theme = Theme(self.meta.get("theme", "default"), self.cfg)
        self.id = config.doc_id(self.dir, self.cfg)
        self.page = str(self.meta.get("page") or self.cfg.get("page") or "a4").lower()
        if self.page not in config.PAGES:
            self.page = "a4"

    # ------------------------------------------------------------------ content
    @property
    def content_path(self):
        return os.path.join(self.dir, self.meta.get("content", "content.html"))

    def content(self):
        try:
            with open(self.content_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return "<section><h1>Empty document</h1><p>No content yet.</p></section>"

    @property
    def built(self):
        return bool(self.meta.get("build"))

    def pdf_name(self):
        return os.path.basename(self.meta.get("pdf") or re.sub(r'[\\/:*?"<>|]+', "-", self.title) + ".pdf")

    def field(self, k):
        return self.meta.get(k) or self.theme.meta.get(k) or ""

    def fill(self, s):
        return (s.replace("{org}", self.field("org")).replace("{classification}", self.field("classification").upper())
                .replace("{title}", self.title).replace("{website}", self.field("website")))

    # ------------------------------------------------------------------ edits
    def apply_edit(self, cid, before, after):
        """Replace the inner HTML of block `cid` in the content file. Returns (new_cid, before_html_in_file)."""
        src = self.content()
        blk = next((b for b in scan_blocks(src) if b["cid"] == cid), None)
        if not blk or not blk["editable"]:
            raise LookupError("block not found; the document changed since the page was loaded, reload and try again")
        cur = src[blk["open_end"]:blk["inner_end"]]
        if strip_tags(cur) != strip_tags(before):
            raise ValueError("this block changed since the page was loaded; reload and try again")
        new_inner = clean_inline(after)
        if not strip_tags(new_inner):
            raise ValueError("empty text; to remove a block, leave a comment instead")
        src2 = src[:blk["open_end"]] + new_inner + src[blk["inner_end"]:]
        write_atomic(self.content_path, src2)
        # the new anchor of the block that was just edited (same position in the file)
        pos = blk["open_start"]
        nb = next(b for b in scan_blocks(src2) if b["open_start"] == pos)
        return nb["cid"], cur, new_inner

    def revert_edit(self, item):
        src = self.content()
        for b in scan_blocks(src):
            inner = src[b["open_end"]:b["inner_end"]]
            if b["editable"] and strip_tags(inner) == strip_tags(item["after"]):
                src2 = src[:b["open_end"]] + item["before"] + src[b["inner_end"]:]
                write_atomic(self.content_path, src2)
                return True
        return False

    # ------------------------------------------------------------------ render
    def toc(self, body):
        items = []
        for m in re.finditer(r"<h([12])\b([^>]*)>(.*?)</h\1>", body, re.S | re.I):
            idm = re.search(r'\bid="?([^"\s>]+)', m.group(2))
            if not idm:
                continue
            inner = m.group(3)
            nm = re.search(r'<span class="?n"?>(.*?)</span>', inner)
            num = strip_tags(nm.group(1)) if nm else ""
            text = strip_tags(inner[nm.end():] if nm else inner)
            items.append((int(m.group(1)), idm.group(1), num, text))
        return items

    def cover_html(self, logo_url):
        """The theme's cover filled in for this document."""
        th, esc = self.theme, html.escape
        cv = self.meta.get("cover", {})
        meta_rows = "".join(f"<tr><td>{esc(str(k))}</td><td>{v}</td></tr>" for k, v in self.meta.get("meta", []))
        cover = (th.cover.replace("{{logo}}", f'<img class="logo" src="{logo_url}" alt="">' if logo_url else "")
                 .replace("{{kicker}}", cv.get("kicker", "")).replace("{{title}}", cv.get("title", self.title))
                 .replace("{{subtitle}}", cv.get("subtitle", ""))
                 .replace("{{meta}}", f'<table class="meta">{meta_rows}</table>' if meta_rows else "")
                 .replace("{{org}}", esc(self.field("org"))).replace("{{classification}}", esc(self.field("classification"))))
        cover = re.sub(r"\{\{(\w+)\}\}", lambda m: esc(str(self.field(m.group(1)) or cv.get(m.group(1), ""))), cover)
        return cover

    def render_cover(self):
        """A page with only the cover, at page size: the main page shows it scaled down as a miniature."""
        tu = f"/_paiper/themes/{self.theme.name}/"
        logo = self.theme.meta.get("logo")
        pw, ph = config.PAGES[self.page]
        css = f'<link rel="stylesheet" href="{self.meta["css"]}">' if self.meta.get("css") else ""
        base = html.escape("/d/" + quote(self.id or "") + "/")
        return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><base href="{base}"><link rel="stylesheet" href="/_paiper/rt/report.css">'
                f'<link rel="stylesheet" href="{tu}theme.css">{css}'
                f'<style>:root {{ --page-w: {pw}mm; --page-h: {ph}mm; }} html, body {{ margin: 0; overflow: hidden; background: #fff; }} '
                f'.cover {{ width: {pw}mm; height: {ph}mm; position: relative; overflow: hidden; font-size: 9.6pt; }}</style></head>'
                f'<body class="paiper report theme-{self.theme.name}"><div class="cover">{self.cover_html((tu + logo) if logo else None)}</div></body></html>')

    def render(self, mode="server", editable=True):
        """mode: 'server' (viewer with comments/edits), 'print' (WeasyPrint, file URLs), 'export' (static folder)."""
        th = self.theme
        if mode == "server":
            rt, tu = "/_paiper/rt/", f"/_paiper/themes/{th.name}/"
        elif mode == "print":
            rt, tu = Path(config.RUNTIME).as_uri() + "/", Path(th.dir).as_uri() + "/"
        else:
            rt, tu = "_paiper/rt/", "_paiper/theme/"
        body = self.content()
        if mode != "print":
            body = tag_content(body)
        else:
            body = self.resolve_svg_vars(body)
            if self.meta.get("keep_headings", True):          # a heading never starts in the bottom part of a page
                body = re.sub(r"<h([123])\b(?![^>]*\bpb\b)", lambda m: f'<div class="keep-room{" h3" if m.group(1) == "3" else ""}"></div><h{m.group(1)}', body)
        toc = self.toc(body)
        esc = html.escape
        logo = th.meta.get("logo")
        logo_url = (tu + logo) if logo else None

        cover = self.cover_html(logo_url)
        tocpage = ""
        if self.meta.get("toc", True) and toc:
            tt = esc(self.meta.get("toc_title", "Contents"))
            tocpage = f'<div class="tocpage"><h1{f" data-running={chr(34)}{tt}{chr(34)}" if "data-running" in body else ""}>{tt}</h1>' + "".join(
                f'<a class="l{lv}" href="#{i}"><span class="n">{esc(n)}</span>{esc(t)}</a>' for lv, i, n, t in toc) + "</div>"
        nav = '<div class="tt">Contents</div>' + "".join(
            f'<a class="l{lv}" href="#{i}">{esc(n + " ") if n else ""}{esc(t)}</a>' for lv, i, n, t in toc)

        foot = th.meta.get("footer", {"left": "{org}", "center": "{classification}", "right": "page {page}"})
        font = th.meta.get("font_family", "sans-serif")
        extra_css = f'<link rel="stylesheet" href="{self.meta["css"]}">' if self.meta.get("css") else ""

        def css_str(s):
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

        def margin_box(pos, txt):
            if not txt:
                return f"@{pos} {{ content: none; }}"
            parts = re.split(r"(\{pages?\})", txt)
            content = " ".join("counter(page)" if p == "{page}" else "counter(pages)" if p == "{pages}" else css_str(self.fill(p))
                               for p in parts if p)
            return (f"@{pos} {{ content: {content}; font-family: '{font}'; font-size: 7.5pt; color: #808080; "
                    f"vertical-align: top; padding-top: 6pt; }}")
        pw, ph = config.PAGES[self.page]
        size = "A4" if self.page == "a4" else "Letter"
        page_css = (f":root {{ --page-w: {pw}mm; --page-h: {ph}mm; }} "
                    f"@media print {{ @page {{ size: {size}; margin: 2.7cm 2cm 1.8cm 2cm; "
                    "@top-left { content: element(page-logo); vertical-align: top; } "
                    + (f"@top-right {{ content: string(running); font-family: '{font}'; font-size: 7.5pt; color: #808080; vertical-align: top; "
                       "padding-top: 1.18cm; text-align: right; } " if "data-running" in body else "")
                    + margin_box("bottom-left", foot.get("left", "")) + " "
                    + margin_box("bottom-center", foot.get("center", "")) + " "
                    + margin_box("bottom-right", foot.get("right", "")).replace("vertical-align: top;", "vertical-align: top; text-align: right;")
                    + " } @page cover { size: " + size + "; margin: 0; @top-left { content: none; } @bottom-left { content: none; } "
                    "@bottom-center { content: none; } @bottom-right { content: none; } @top-right { content: none; } } "
                    "[data-running] { string-set: running attr(data-running); } }")
        running = (f'<img class="running-logo-img" src="{logo_url}" alt="">' if logo_url else esc(self.title))

        cfgjs = {
            "doc": self.id, "api": "/api", "title": self.title, "logo": logo_url,
            "footer": {k: self.fill(v) for k, v in foot.items()},
            "editable": bool(editable and mode == "server"), "built": self.built,
            "comments": mode == "server", "author": self.cfg.get("author", ""),
            "palette": th.meta.get("palette", {}), "page": self.page,
        }
        cfg_json = json.dumps(cfgjs).replace("</", "<\\/")          # a title cannot close the script element
        pre, post = "", body
        if self.meta.get("toc_after_first") and "</section>" in body:      # e.g. an executive summary before the contents
            i = body.index("</section>") + len("</section>")
            pre, post = body[:i] + "\n", body[i:]
        pdf = self.pdf_name()
        ic = ICONS
        has_pdf = os.path.exists(os.path.join(self.dir, pdf))
        zoom = ('<div class="tb-seg" role="group" aria-label="Zoom">'
                f'<button class="tb-ic" id="zout" data-tip="Zoom out" data-kbd="&minus;" aria-label="Zoom out">{ic["minus"]}</button>'
                '<button class="tb-zval" id="zlabel" data-tip="Actual size" data-kbd="0">100%</button>'
                f'<button class="tb-ic" id="zin" data-tip="Zoom in" data-kbd="+" aria-label="Zoom in">{ic["plus"]}</button>'
                f'<button class="tb-ic" id="zfit" data-tip="Fit to window" aria-label="Fit to window">{ic["fit"]}</button></div>')
        mode_seg = ('<div class="tb-seg tb-mode" role="radiogroup" aria-label="Mode">'
                    f'<button id="modecomment" class="on" role="radio" aria-checked="true" data-tip="Comment on any block or selection">{ic["comment"]}<span>Comment</span></button>'
                    f'<button id="editbtn" role="radio" aria-checked="false" data-tip="Change wording in place" data-kbd="E">{ic["pencil"]}<span>Edit</span></button></div>'
                    ) if mode == "server" else ""
        panel_btn = (f'<button class="tb-btn" id="panelbtn" data-tip="Review panel" aria-label="Review panel">{ic["panel"]}'
                     '<span class="tb-badge" id="opencount" hidden></span></button>') if mode == "server" else ""
        pdf_btn = (f'<a class="tb-btn tb-pdf" href="{esc(pdf)}" download data-tip="Download PDF">{ic["download"]}<span>PDF</span></a>'
                   if has_pdf else "")
        right = f'<div class="tb-right">{zoom}<span class="tb-div"></span>{mode_seg}{panel_btn}{pdf_btn}</div>'
        home = ('<a class="home" href="/" title="All documents">p<b>ai</b>per</a>' if mode == "server"
                else '<span class="home">p<b>ai</b>per</span>')
        scripts = "" if mode == "print" else (
            f'<script src="{rt}viewer.js"></script>' + (f'<script src="{rt}review.js"></script>' if mode == "server" else ""))

        return f"""<!DOCTYPE html>
<html lang="{self.meta.get('lang', 'en')}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(self.title)}</title>
<link rel="stylesheet" href="{rt}ui.css"><link rel="stylesheet" href="{rt}report.css"><link rel="stylesheet" href="{tu}theme.css">{extra_css}
<style>{page_css}</style>
<script type="application/json" id="paiper-config">{cfg_json}</script>
</head>
<body class="paiper report theme-{th.name}{' tables-ruled' if th.meta.get('tables') == 'ruled' else ''}">
<div class="running-logo-wrap">{running}</div>
<header class="topbar"><div class="tb-left">{home}<span class="tb-title">{esc(self.title)}</span>{f'<span class="tb-ver">v{esc(self.meta_value("Version"))}</span>' if self.meta_value("Version") else ""}</div>
{right}</header>
<div class="layout">
<nav class="toc">{nav}</nav>
<main class="doc">
<div class="cover">{cover}</div>
{pre}{tocpage}
{post}
</main>
<aside class="panel"></aside>
</div>
{scripts}
</body></html>"""

    def css_vars(self):
        """CSS custom properties in effect for this document (runtime defaults, theme, document css)."""
        css = []
        for p in (os.path.join(config.RUNTIME, "report.css"), os.path.join(self.theme.dir, "theme.css"),
                  os.path.join(self.dir, self.meta["css"]) if self.meta.get("css") else ""):
            if p and os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    css.append(f.read())
        out = {}
        for sel, props in css_rules("\n".join(css)):
            if sel == ":root":
                out.update({k[2:]: v for k, v in props.items() if k.startswith("--")})
        return out

    def resolve_svg_vars(self, body):
        """WeasyPrint does not apply var(--x) inside inline SVG: put the theme's values in for the PDF."""
        if "<svg" not in body or "var(" not in body:
            return body
        vs = self.css_vars()

        def val(name, fallback, depth=0):
            v = vs.get(name, fallback or "")
            while "var(" in v and depth < 8:
                v = re.sub(r"var\(\s*--([\w-]+)\s*(?:,\s*([^()]*))?\)", lambda m: vs.get(m.group(1), m.group(2) or ""), v)
                depth += 1
            return v.strip()

        def fix_svg(m):
            return re.sub(r"var\(\s*--([\w-]+)\s*(?:,\s*([^()]*))?\)", lambda v: val(v.group(1), v.group(2)), m.group(0))
        return re.sub(r"<svg\b.*?</svg>", fix_svg, body, flags=re.S | re.I)

    def meta_value(self, key):
        for k, v in self.meta.get("meta", []):
            if str(k).lower() == key.lower():
                return strip_tags(str(v))
        return ""

    def open_count(self):
        return len(store.open_items(self.dir))

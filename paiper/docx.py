"""Word export: a paiper document as a native .docx, with the Python standard library only.

The result is meant to look made in Word, not converted from HTML: built-in styles (Normal, Title, Subtitle,
Heading 1-4, Caption, List Paragraph, Quote, TOC 1-3, Header, Footer) carry the theme's font and colours,
lists use numbering.xml, tables are real tables (header row repeats, rows never split), photos and charts are
embedded pictures, inline SVG is converted to PNG with rsvg-convert, the cover is built from the theme's
cover.html/theme.css, the contents page is a real TOC field (pre-filled, page numbers included), the header
carries the logo and the footer the theme's footer text with PAGE/NUMPAGES fields. TrueType fonts listed by
the theme are embedded (obfuscated as Word does, ECMA-376 Part 1, 17.8.1).

theme.json:  "docx": {"font": "Satoshi", "embed": ["fonts/Satoshi-Regular.ttf", ...]}
"""
import base64
import datetime
import html
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
import zipfile
import zlib
from html.entities import name2codepoint
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.sax.saxutils import escape

from . import config
from .css import css_rules
from .doc import Doc, strip_tags

# ---------------------------------------------------------------------------------------------- geometry
PAGE_W, PAGE_H = 11906, 16838                 # A4 in twips
MARGIN = 1134                                 # 2 cm
TOP_LOGO = 1531                               # 2.7 cm when the header carries a logo
TEXT_W = PAGE_W - 2 * MARGIN                  # 9638 twips = 17 cm
EMU_TW = 635                                  # EMU per twip
EMU_PT = 12700
TW_MM = 1440 / 25.4

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CT_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml."

_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def xt(s):
    """Text for an XML text node or attribute."""
    return escape(_BAD_XML.sub("", str(s)), {'"': "&quot;"})


def _len(v, em=10.0):
    """A CSS length in points (None when it is not one)."""
    m = re.match(r"\s*(-?[\d.]+)\s*(pt|px|mm|cm|in|em|rem)?\s*$", v or "")
    if not m:
        return None
    return float(m.group(1)) * {"pt": 1, "px": .75, "mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72, "em": em, "rem": em}[m.group(2) or "px"]


NAMED = {"white": "FFFFFF", "black": "000000", "red": "FF0000", "grey": "808080", "gray": "808080", "yellow": "FFFF00"}


def _hex(v):
    v = (v or "").strip().lower()
    m = re.match(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", v)
    if m:
        h = m.group(1)
        return (h if len(h) == 6 else "".join(c * 2 for c in h)).upper()
    m = re.match(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)", v)
    if m:
        return "%02X%02X%02X" % tuple(min(255, int(x)) for x in m.groups())
    return NAMED.get(v)


# ---------------------------------------------------------------------------------------------- HTML tree
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr", "x-svg"}
P_CLOSERS = {"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "figure", "figcaption", "footer", "form",
             "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul", "details"}
SCOPE = {"td", "th", "li", "div", "section", "blockquote", "figure", "table", "article", "root", "dd", "aside", "main"}


class El:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.kids, self.parent = tag, attrs or {}, [], parent

    @property
    def cls(self):
        return set((self.attrs.get("class") or "").split())

    def els(self):
        return [k for k in self.kids if isinstance(k, El)]

    def text(self):
        return "".join(k if isinstance(k, str) else k.text() for k in self.kids)

    def find(self, tag=None, cls=None):
        for k in self.els():
            if (tag is None or k.tag == tag) and (cls is None or cls in k.cls):
                return k
            f = k.find(tag, cls)
            if f is not None:
                return f
        return None

    def has_block(self):
        return any(k.tag in BLOCK or k.has_block() for k in self.els())


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("root")
        self.cur = self.root

    def _close(self, tags, stop):
        n = self.cur
        while n is not self.root and n.tag not in stop:
            if n.tag in tags:
                self.cur = n.parent
                return
            n = n.parent

    def _implicit(self, tag):
        if tag in P_CLOSERS:
            self._close({"p"}, SCOPE)
        if tag == "li":
            self._close({"li"}, {"ul", "ol", "root"})
        elif tag in ("dt", "dd"):
            self._close({"dt", "dd"}, {"dl", "root"})
        elif tag == "tr":
            self._close({"tr"}, {"table", "thead", "tbody", "tfoot", "root"})
        elif tag in ("td", "th"):
            self._close({"td", "th"}, {"tr", "table", "root"})
        elif tag in ("thead", "tbody", "tfoot"):
            self._close({"thead", "tbody", "tfoot"}, {"table", "root"})

    def handle_starttag(self, tag, attrs):
        self._implicit(tag)
        el = El(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.kids.append(el)
        if tag not in VOID:
            self.cur = el

    def handle_startendtag(self, tag, attrs):
        self._implicit(tag)
        self.cur.kids.append(El(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n is not self.root:
            self.cur = n.parent

    def handle_data(self, d):
        k = self.cur.kids
        if k and isinstance(k[-1], str):
            k[-1] += d
        else:
            k.append(d)


def _svg_spans(src):
    """(start, end) of each top-level <svg>...</svg> (nesting-aware)."""
    out, pos = [], 0
    pat = re.compile(r"<(/?)svg\b[^>]*?(/?)>", re.I)
    while True:
        m = pat.search(src, pos)
        if not m:
            return out
        if m.group(1):
            pos = m.end()
            continue
        if m.group(2):
            out.append((m.start(), m.end()))
            pos = m.end()
            continue
        depth, j = 1, m.end()
        for n in pat.finditer(src, m.end()):
            if n.group(2):
                continue
            depth += -1 if n.group(1) else 1
            if depth == 0:
                j = n.end()
                break
        else:
            j = len(src)
        out.append((m.start(), j))
        pos = j


def parse_html(src):
    """Tree of the content; every inline <svg> becomes an <x-svg data-i> leaf, its source kept in a list."""
    svgs, parts, last = [], [], 0
    for a, b in _svg_spans(src):
        parts.append(src[last:a])
        parts.append(f'<x-svg data-i="{len(svgs)}"></x-svg>')
        svgs.append(src[a:b])
        last = b
    parts.append(src[last:])
    t = _Tree()
    t.feed("".join(parts))
    t.close()
    return t.root, svgs


BLOCK = {"address", "article", "aside", "blockquote", "div", "dl", "dt", "dd", "fieldset", "figure", "figcaption", "footer",
         "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "ul", "li", "p", "pre", "section",
         "table", "details", "summary", "canvas", "video", "iframe", "object", "embed", "audio", "center", "caption", "body", "html"}
SKIP = {"script", "style", "template", "noscript", "button", "input", "select", "textarea", "head", "title", "meta", "link",
        "colgroup", "col", "source", "track", "map", "area", "dialog"}
NOT_RENDERED = {"canvas", "video", "iframe", "object", "embed", "audio"}
INLINE_FMT = {"strong": {"b": True}, "b": {"b": True}, "em": {"i": True}, "i": {"i": True}, "cite": {"i": True}, "var": {"i": True},
              "dfn": {"i": True}, "u": {"u": True}, "ins": {"u": True}, "s": {"s": True}, "del": {"s": True}, "strike": {"s": True},
              "sub": {"va": "subscript"}, "sup": {"va": "superscript"}, "code": {"code": True}, "kbd": {"code": True},
              "samp": {"code": True}, "tt": {"code": True}}


# ---------------------------------------------------------------------------------------------- images
def image_size(data):
    """(kind, width px, height px) from the file header, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return "gif", w, h
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", 0, 0
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 1 if marker == 0xFF else 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpeg", w, h
            i += 2 + seg
        return None
    head = data[:600].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4000].lower()):
        return "svg", 0, 0
    return None


def png_bytes(w, h, rows):
    """A PNG (RGBA) from a list of h row byte-strings of 4*w bytes."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def shape_png(color, ellipse, w, h):
    """A solid rectangle or an anti-aliased ellipse, for the decorative shapes of a cover."""
    r_, g_, b_ = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    if not ellipse:
        return png_bytes(4, 4, [bytes((r_, g_, b_, 255)) * 4] * 4)
    rows, rx, ry = [], w / 2.0, h / 2.0
    for y in range(h):
        dy = (y + .5 - ry) / ry
        row = bytearray(4 * w)
        if abs(dy) < 1:
            half = rx * math.sqrt(1 - dy * dy)
            x0, x1 = rx - half, rx + half
            for x in range(max(0, int(x0)), min(w, int(math.ceil(x1)))):
                cov = max(0.0, min(1.0, x + 1 - x0) if x < x0 + 1 else 1.0)
                cov = min(cov, max(0.0, min(1.0, x1 - x)))
                row[4 * x:4 * x + 4] = bytes((r_, g_, b_, int(255 * cov)))
        rows.append(bytes(row))
    return png_bytes(w, h, rows)


def _tool(name):
    return shutil.which(name) or next((p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}") if os.path.exists(p)), None)


# ---------------------------------------------------------------------------------------------- fonts
def font_info(data):
    """(family, bold, italic) of a TrueType/OpenType font, from its name and OS/2 tables."""
    num = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for i in range(num):
        tag, _, off, ln = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
        tables[tag] = (off, ln)
    fam, sel = None, 0
    if b"OS/2" in tables:
        o = tables[b"OS/2"][0]
        sel = struct.unpack(">H", data[o + 62:o + 64])[0]
    if b"name" in tables:
        o = tables[b"name"][0]
        _, cnt, so = struct.unpack(">HHH", data[o:o + 6])
        for i in range(cnt):
            pid, eid, lid, nid, ln, off = struct.unpack(">6H", data[o + 6 + 12 * i:o + 18 + 12 * i])
            if nid == 1 and pid == 3 and lid in (0x409, 0):
                fam = data[o + so + off:o + so + off + ln].decode("utf-16-be", "replace")
                break
    return fam, bool(sel & 0x20), bool(sel & 0x01)


def obfuscate(data, key):
    """ECMA-376 Part 1, 17.8.1: XOR the first 32 bytes with the GUID's bytes in reverse order."""
    k = bytes.fromhex(key.strip("{}").replace("-", ""))[::-1]
    return bytes(b ^ k[i % 16] for i, b in enumerate(data[:32])) + data[32:]


# ---------------------------------------------------------------------------------------------- paragraphs
class P:
    """A paragraph being built: properties and a list of runs (text, breaks, tabs, raw run XML)."""

    def __init__(self, style=None, **kw):
        self.style = style
        self.items = []
        self.space = True
        self.page_break = kw.pop("page_break", False)
        self.keep_next = kw.pop("keep_next", False)
        self.keep_lines = kw.pop("keep_lines", False)
        self.num = kw.pop("num", None)
        self.ind = kw.pop("ind", None)
        self.jc = kw.pop("jc", None)
        self.before = kw.pop("before", None)
        self.after = kw.pop("after", None)
        self.line = kw.pop("line", None)
        self.shd = kw.pop("shd", None)
        self.pbdr = kw.pop("pbdr", None)
        self.tabs = kw.pop("tabs", None)
        self.mark = kw.pop("mark", None)
        self.bookmark = kw.pop("bookmark", None)
        self.contextual = kw.pop("contextual", False)
        self.pre = kw.pop("pre", False)

    def text(self, s, fmt):
        if not s:
            return
        if not self.pre:
            s = re.sub(r"[ \t\r\n\f]+", " ", s)
            if self.space:
                s = s.lstrip(" ")
            if not s:
                return
            self.space = s.endswith(" ")
        self.items.append(("t", s, fmt))

    def br(self, fmt=None):
        self.items.append(("br", None, fmt or {}))
        self.space = True

    def tab(self, fmt=None):
        self.items.append(("tab", None, fmt or {}))
        self.space = True

    def raw(self, xml, fmt=None):
        self.items.append(("raw", xml, fmt or {}))
        self.space = False

    def field(self, instr, result, fmt=None):
        self.items.append(("fld", (instr, result), fmt or {}))
        self.space = False

    def has_content(self):
        return any(k != "t" or v.strip() for k, v, _ in self.items)

    def plain(self):
        return "".join(v for k, v, _ in self.items if k == "t")


def rpr(f):
    if not f:
        return ""
    o = []
    style = "Hyperlink" if f.get("link") else ("InlineCode" if f.get("code") else None)
    if style:
        o.append(f'<w:rStyle w:val="{style}"/>')
    if f.get("font"):
        n = xt(f["font"])
        o.append(f'<w:rFonts w:ascii="{n}" w:hAnsi="{n}" w:cs="{n}"/>')
    for k, tag in (("b", "b"), ("i", "i")):
        if f.get(k) is True:
            o.append(f"<w:{tag}/><w:{tag}Cs/>")
        elif f.get(k) is False:
            o.append(f'<w:{tag} w:val="0"/><w:{tag}Cs w:val="0"/>')
    if f.get("caps"):
        o.append("<w:caps/>")
    if f.get("s"):
        o.append("<w:strike/>")
    if f.get("color") and not f.get("link"):
        o.append(f'<w:color w:val="{f["color"]}"/>')
    if f.get("spacing"):
        o.append(f'<w:spacing w:val="{int(f["spacing"])}"/>')
    if f.get("size"):
        o.append(f'<w:sz w:val="{int(f["size"])}"/><w:szCs w:val="{int(f["size"])}"/>')
    if f.get("u"):
        o.append('<w:u w:val="single"/>')
    if f.get("bdr"):
        o.append(f'<w:bdr w:val="single" w:sz="6" w:space="0" w:color="{f["bdr"]}"/>')
    if f.get("shd"):
        o.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{f["shd"]}"/>')
    if f.get("va"):
        o.append(f'<w:vertAlign w:val="{f["va"]}"/>')
    return "<w:rPr>" + "".join(o) + "</w:rPr>" if o else ""


def run(text, f=None):
    sp = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return f"<w:r>{rpr(f)}<w:t{sp}>{xt(text)}</w:t></w:r>"


def fld_runs(instr, result, f=None):
    r = rpr(f)
    return (f'<w:r>{r}<w:fldChar w:fldCharType="begin"/></w:r><w:r>{r}<w:instrText xml:space="preserve"> {xt(instr)} </w:instrText></w:r>'
            f'<w:r>{r}<w:fldChar w:fldCharType="separate"/></w:r>{run(result, f)}<w:r>{r}<w:fldChar w:fldCharType="end"/></w:r>')


def ppr(p):
    o = []
    if p.style:
        o.append(f'<w:pStyle w:val="{p.style}"/>')
    if p.keep_next:
        o.append("<w:keepNext/>")
    if p.keep_lines:
        o.append("<w:keepLines/>")
    if p.page_break:
        o.append("<w:pageBreakBefore/>")
    if p.num:
        o.append(f'<w:numPr><w:ilvl w:val="{p.num[1]}"/><w:numId w:val="{p.num[0]}"/></w:numPr>')
    if p.pbdr:
        o.append(f"<w:pBdr>{p.pbdr}</w:pBdr>")
    if p.shd:
        o.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{p.shd}"/>')
    if p.tabs:
        o.append("<w:tabs>" + "".join(f'<w:tab w:val="{v}"{lead} w:pos="{int(pos)}"/>' for v, pos, lead in p.tabs) + "</w:tabs>")
    sp = []
    if p.before is not None:
        sp.append(f'w:before="{int(p.before)}"')
    if p.after is not None:
        sp.append(f'w:after="{int(p.after)}"')
    if p.line is not None:
        sp.append(f'w:line="{int(p.line)}" w:lineRule="auto"')
    if sp:
        o.append(f'<w:spacing {" ".join(sp)}/>')
    if p.ind:
        o.append("<w:ind " + " ".join(f'w:{k}="{int(v)}"' for k, v in p.ind.items()) + "/>")
    if p.contextual:
        o.append("<w:contextualSpacing/>")
    if p.jc:
        o.append(f'<w:jc w:val="{p.jc}"/>')
    if p.mark:
        o.append(rpr(p.mark))
    return "<w:pPr>" + "".join(o) + "</w:pPr>" if o else ""


def p_xml(p, link_xml):
    """Paragraph XML. link_xml(link) -> (open, close) for a hyperlink wrapper."""
    items = list(p.items)
    while items and items[-1][0] == "t" and not p.pre:          # trailing whitespace
        k, v, f = items[-1]
        v = v.rstrip(" ")
        if v:
            items[-1] = (k, v, f)
            break
        items.pop()
    out, cur_link, buf = [], None, []

    def flush():
        if not buf:
            return
        if cur_link:
            o, c = link_xml(cur_link)
            out.append(o + "".join(buf) + c)
        else:
            out.extend(buf)
        buf.clear()
    for k, v, f in items:
        lk = f.get("link")
        if lk != cur_link:
            flush()
            cur_link = lk
        if k == "t":
            buf.append(run(v, f))
        elif k == "br":
            buf.append(f"<w:r>{rpr(f)}<w:br/></w:r>")
        elif k == "tab":
            buf.append(f"<w:r>{rpr(f)}<w:tab/></w:r>")
        elif k == "fld":
            buf.append(fld_runs(v[0], v[1], f))
        else:
            buf.append(v)
    flush()
    body = "".join(out)
    if p.bookmark:
        name, bid = p.bookmark
        body = f'<w:bookmarkStart w:id="{bid}" w:name="{xt(name)}"/>{body}<w:bookmarkEnd w:id="{bid}"/>'
    return f"<w:p>{ppr(p)}{body}</w:p>"


class Ctx:
    """Where blocks go: available width, paragraph defaults, base run format."""

    def __init__(self, width=None, **kw):
        self.width = TEXT_W if width is None else width
        self.style = kw.get("style")
        self.fmt = kw.get("fmt", {})
        self.cell = kw.get("cell", False)
        self.jc = kw.get("jc")
        self.after = kw.get("after")
        self.keep = kw.get("keep", False)
        self.size = kw.get("size", 20)
        self.indent = kw.get("indent", 0)

    def but(self, **kw):
        c = Ctx(self.width)
        c.__dict__.update(self.__dict__)
        c.__dict__.update(kw)
        return c


def tbl_borders(spec, tag="tblBorders"):
    """spec: {side: (val, sz, color)}; missing sides are nil."""
    o = []
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        if tag == "tcBorders" and side.startswith("inside") and side not in spec:
            continue
        v = spec.get(side)
        o.append(f'<w:{side} w:val="{v[0]}" w:sz="{v[1]}" w:space="0" w:color="{v[2]}"/>' if v else f'<w:{side} w:val="nil"/>')
    return f"<w:{tag}>{''.join(o)}</w:{tag}>"


def cell_mar(top, left, bottom, right, tag="tblCellMar"):
    return (f'<w:{tag}><w:top w:w="{top}" w:type="dxa"/><w:left w:w="{left}" w:type="dxa"/>'
            f'<w:bottom w:w="{bottom}" w:type="dxa"/><w:right w:w="{right}" w:type="dxa"/></w:{tag}>')


def ensure_p(blocks):
    """A table cell must end with a paragraph."""
    if not blocks or not blocks[-1].startswith("<w:p>") and not blocks[-1].startswith("<w:p "):
        blocks = blocks + ['<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="120" w:lineRule="auto"/><w:rPr><w:sz w:val="4"/></w:rPr></w:pPr></w:p>']
    return blocks


class Rels:
    def __init__(self):
        self.items, self.index = [], {}

    def add(self, typ, target, external=False):
        key = (typ, target)
        if key in self.index:
            return self.index[key]
        rid = f"rId{len(self.items) + 1}"
        self.items.append((rid, typ, target, external))
        self.index[key] = rid
        return rid

    def xml(self):
        return (DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
            f'<Relationship Id="{rid}" Type="{RT}{t}" Target="{xt(tg)}"{" TargetMode=" + chr(34) + "External" + chr(34) if ext else ""}/>'
            for rid, t, tg, ext in self.items) + "</Relationships>")


# ---------------------------------------------------------------------------------------------- the writer
class Word:
    def __init__(self, doc, paginate=True):
        self.doc, self.paginate = doc, paginate
        th = doc.theme
        self.tdir = th.dir
        self.dx = th.meta.get("docx") or {}
        rules = css_rules(_read(os.path.join(config.RUNTIME, "report.css"))) + css_rules(_read(os.path.join(th.dir, "theme.css")))
        doc_css = _read(os.path.join(doc.dir, doc.meta["css"])) if doc.meta.get("css") else ""
        self.doc_css = doc_css
        rules += css_rules(doc_css)
        self.rules = rules
        self.vars = {}
        for sel, props in rules:
            if sel == ":root":
                self.vars.update({k[2:]: v for k, v in props.items() if k.startswith("--")})
        pal = th.meta.get("palette") or {}
        for k, v in pal.items():
            if isinstance(v, str):
                self.vars.setdefault(k, v)

        def col(name, fb):
            return _hex(self.var(f"var(--{name})")) or _hex(pal.get(name, "")) or fb
        self.c = {k: col(k, fb) for k, fb in (("ink", "1B2430"), ("strong", "1B2430"), ("accent", "2F5D8A"), ("accent-ink", "FFFFFF"),
                                                ("tint", "E8EFF6"), ("light", "F3F5F7"), ("rule", "DBE0E6"), ("grey", "6B7684"),
                                                ("muted", "59636F"), ("h1", "2F5D8A"), ("h2", "1B2430"), ("th-bg", "2F5D8A"))}
        self.c["th-ink"] = _hex(self.var("var(--th-ink)")) or self.c["accent-ink"]       # header text, when a theme sets it
        self.ruled = doc.theme.meta.get("tables") == "ruled"       # horizontal rules instead of filled cells
        if self.ruled:
            self.c["th-ink"] = _hex(self.var("var(--th-ink)")) or self.c["strong"]
        self.font = self.dx.get("font") or "Calibri"
        self.mono = self.dx.get("mono") or "Consolas"
        self.justify = (self.var("var(--align)") or "").strip() == "justify"
        self.logo = th.meta.get("logo")
        self.top = TOP_LOGO if self.logo else MARGIN + 340
        self.rels = Rels()
        self.media = {}            # key -> (part name, w, h)
        self.media_parts = {}      # part name -> bytes
        self.parts = {}
        self.part_rels = {}
        self.docpr = 0
        self.bm = 0
        self.fig_n = self.tbl_n = 0
        self.nums = []             # (numId, abstract, ilvl, start)
        self.headings = []         # (level, bookmark, number, text)
        self.pending_break = False
        self.notes = []
        self.tmp = tempfile.mkdtemp(prefix="paiper-docx-")
        self.rsvg = _tool("rsvg-convert")
        self.sips = _tool("sips")
        self._fc = None
        self.class_rules = []
        for sel, props in rules:
            m = re.match(r"^([a-z0-9]*)((?:\.[\w-]+)+)$", sel)
            if m:
                self.class_rules.append((m.group(1) or None, set(m.group(2)[1:].split(".")), props))

    # ------------------------------------------------------------------ helpers
    def var(self, v, depth=0):
        if not v or "var(" not in v or depth > 8:
            return v

        def sub(m):
            got = self.vars.get(m.group(1))
            return got if got is not None else (m.group(2) or "")
        return self.var(re.sub(r"var\(\s*--([\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)", sub, v), depth + 1)

    def color(self, v):
        return _hex(self.var(v))

    def rule(self, selector):
        out = {}
        for sel, props in self.rules:
            if sel == selector:
                out.update(props)
        return out

    def cls_props(self, el):
        out = {}
        cls = el.cls
        if not cls:
            return out
        for tag, need, props in self.class_rules:
            if (tag is None or tag == el.tag) and need <= cls:
                out.update(props)
        return out

    def props_fmt(self, props, size=20):
        f = {}
        w = props.get("font-weight", "")
        if w in ("bold", "bolder") or (w.isdigit() and int(w) >= 600):
            f["b"] = True
        elif w in ("normal", "400"):
            f["b"] = False
        st = props.get("font-style")
        if st == "italic":
            f["i"] = True
        elif st == "normal":
            f["i"] = False
        if props.get("color"):
            c = self.color(props["color"])
            if c:
                f["color"] = c
        if props.get("text-transform") == "uppercase":
            f["caps"] = True
        fs = props.get("font-size")
        if fs:
            pt = _len(fs, size / 2)
            if pt is None and fs.endswith("%"):
                pt = float(fs[:-1]) / 100 * size / 2
            if pt:
                f["size"] = round(pt * 2)
        ls = props.get("letter-spacing")
        if ls:
            pt = _len(ls, (f.get("size") or size) / 2)
            if pt:
                f["spacing"] = round(pt * 20)
        return f

    def border_of(self, selector, side, default_color, default_sz=24):
        v = self.rule(selector).get(f"border-{side}", "")
        if not v:
            return default_sz, default_color, "single"
        w = re.search(r"([\d.]+)(px|pt)", v)
        sz = round((float(w.group(1)) * (.75 if w.group(2) == "px" else 1)) * 8) if w else default_sz
        c = self.color(re.sub(r"[\d.]+(px|pt)|solid|dashed|dotted", "", v).strip()) or default_color
        return max(4, min(sz, 96)), c, "dashed" if "dashed" in v else "single"

    def next_id(self):
        self.docpr += 1
        return self.docpr

    def bookmark(self, name):
        self.bm += 1
        return (name, self.bm)

    def link_xml(self, link):
        kind, val = link
        if kind == "a":
            return f'<w:hyperlink w:anchor="{xt(val)}" w:history="1">', "</w:hyperlink>"
        return f'<w:hyperlink r:id="{val}" w:history="1">', "</w:hyperlink>"

    def emit_p(self, p, ctx):
        if self.pending_break and not ctx.cell and p.has_content():
            p.page_break = True
            self.pending_break = False
        return p_xml(p, self.link_xml)

    def new_p(self, ctx, **kw):
        kw.setdefault("jc", ctx.jc)
        kw.setdefault("after", ctx.after)
        kw.setdefault("keep_next", ctx.keep)
        if ctx.indent and "ind" not in kw:
            kw["ind"] = {"left": ctx.indent}
        return P(kw.pop("style", ctx.style), **kw)

    def note_p(self, ctx, text):
        p = self.new_p(ctx)
        p.text(text, {"i": True, "color": self.c["grey"]})
        return self.emit_p(p, ctx)

    def page_break_p(self):
        """An empty paragraph that starts a new page (before a table, which cannot carry the break itself)."""
        self.pending_break = False
        return ('<w:p><w:pPr><w:pageBreakBefore/><w:spacing w:before="0" w:after="0" w:line="120" w:lineRule="auto"/>'
                '<w:rPr><w:sz w:val="4"/></w:rPr></w:pPr></w:p>')

    # ------------------------------------------------------------------ flow
    def flow(self, el, ctx):
        out, p = [], None

        def flush():
            nonlocal p
            if p is not None and p.has_content():
                out.append(self.emit_p(p, ctx))
            p = None
        for k in el.kids:
            if isinstance(k, str):
                if p is None:
                    if not k.strip():
                        continue
                    p = self.new_p(ctx)
                p.text(k, ctx.fmt)
            elif k.tag in SKIP:
                continue
            elif k.tag in BLOCK or k.has_block():
                flush()
                out += self.block(k, ctx)
            else:
                if p is None:
                    p = self.new_p(ctx)
                self.inline(k, p, ctx.fmt, ctx)
        flush()
        return out

    def block(self, el, ctx):
        tag, cls = el.tag, el.cls
        style = (el.attrs.get("style") or "").replace(" ", "").lower()
        if "break-before:page" in style or "page-break-before:always" in style:
            if not ctx.cell:
                self.pending_break = True
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            out = self.heading(el, ctx)
        elif tag == "p":
            out = self.paragraph(el, ctx)
        elif tag in ("ul", "ol"):
            out = self.list(el, ctx, 0)
        elif tag == "table":
            out = self.table(el, ctx)
        elif tag == "figure":
            out = self.figure(el, ctx)
        elif tag == "blockquote":
            out = self.flow(el, ctx.but(style="Quote", jc=None))
        elif tag == "pre":
            out = self.pre(el, ctx)
        elif tag == "hr":
            p = self.new_p(ctx, pbdr=f'<w:bottom w:val="single" w:sz="6" w:space="1" w:color="{self.c["rule"]}"/>', after=200)
            p.raw('<w:r><w:rPr><w:sz w:val="8"/></w:rPr><w:t xml:space="preserve"> </w:t></w:r>')
            out = [self.emit_p(p, ctx)]
        elif tag == "dt":
            out = self.flow(el, ctx.but(fmt=dict(ctx.fmt, b=True), keep=True, after=20))
        elif tag == "dd":
            out = self.flow(el, ctx.but(indent=ctx.indent + 400))
        elif tag == "li":                                   # stray li outside a list
            out = self.flow(el, ctx)
        elif tag in NOT_RENDERED:
            label = el.attrs.get("aria-label") or el.attrs.get("title") or strip_tags(el.text())
            out = [self.note_p(ctx, "[figure: see PDF" + (f": {label}" if label else "") + "]")]
        elif tag == "caption" or tag == "figcaption":
            out = self.flow(el, ctx.but(style="Caption"))
        elif tag == "summary":
            out = self.flow(el, ctx.but(fmt=dict(ctx.fmt, b=True), keep=True))
        elif "callout" in cls or "todo" in cls:
            out = self.callout(el, ctx, todo="todo" in cls)
        elif "kpis" in cls:
            out = self.kpis(el, ctx)
        elif "cards" in cls:
            out = self.cards(el, ctx)
        elif "photos2" in cls or "photos3" in cls:
            out = self.photos(el, ctx, 3 if "photos3" in cls else 2)
        else:
            props = self.cls_props(el)
            sub = ctx
            if props:
                f = self.props_fmt(props, ctx.size)
                sub = ctx.but(fmt=dict(ctx.fmt, **f), jc=_jc(props.get("text-align")) or ctx.jc)
            out = self.flow(el, sub)
        if ("break-after:page" in style or "page-break-after:always" in style) and not ctx.cell:
            self.pending_break = True
        return out

    # ------------------------------------------------------------------ inline
    def inline(self, el, p, fmt, ctx):
        tag, cls = el.tag, el.cls
        if tag in SKIP:
            return
        if tag == "br":
            p.br(fmt)
            return
        if tag == "wbr":
            return
        if tag == "img":
            p.raw(self.img_run(el, ctx.width), fmt)
            return
        if tag == "x-svg":
            p.raw(self.svg_run(el, ctx.width), fmt)
            return
        if tag in NOT_RENDERED:
            p.text(" [figure: see PDF] ", dict(fmt, i=True, color=self.c["grey"]))
            return
        f = dict(fmt)
        f.update(INLINE_FMT.get(tag, {}))
        if tag in ("small", "sub", "sup") or "small" in cls:
            if tag == "small" or "small" in cls:
                f["size"] = round((fmt.get("size") or ctx.size) * .85)
        if tag == "mark":
            f["shd"] = self.c["tint"]
        if "muted" in cls:
            f["color"] = self.c["grey"]
        if "tag" in cls:
            f.update(b=True, color=self.c["accent"])
        if tag == "a":
            lk = self.link_target(el.attrs.get("href", ""))
            if lk:
                f["link"] = lk
        chip = "chip" in cls
        if chip:
            f.update(b=True, color=self.c["strong"], bdr=self.c["strong"], size=round((fmt.get("size") or ctx.size) * .82))
        props = self.cls_props(el)
        if props:
            f.update(self.props_fmt(props, f.get("size") or ctx.size))
            bg = props.get("background") or props.get("background-color")
            if bg and self.color(bg):
                f["shd"] = self.color(bg)
            bc = props.get("border-color") or props.get("border")
            if bc and chip:
                c = self.color(re.sub(r"[\d.]+(px|pt)|solid|dashed|dotted", "", bc).strip())
                if c:
                    f["bdr"] = c
        if chip:
            txt = re.sub(r"\s+", " ", el.text()).strip()
            p.text(" " + txt + " ", f)
            return
        if tag == "q":
            p.text("“", f)
        for k in el.kids:
            if isinstance(k, str):
                p.text(k, f)
            elif k.tag in ("p", "div", "li") and p.has_content():
                p.br(f)
                self.inline(k, p, f, ctx)
            else:
                self.inline(k, p, f, ctx)
        if tag == "q":
            p.text("”", f)

    def link_target(self, href):
        href = (href or "").strip()
        if href.startswith("#"):
            bm = self.anchors.get(href[1:])
            return ("a", bm) if bm else None
        if re.match(r"^(https?:|mailto:|tel:)", href, re.I):
            return ("r", self.rels.add("hyperlink", href, external=True))
        return None

    # ------------------------------------------------------------------ headings and paragraphs
    def heading(self, el, ctx):
        lvl = int(el.tag[1])
        style = f"Heading{min(lvl, 4)}"
        if ctx.cell:
            p = self.new_p(ctx, keep_next=True)
            self._heading_runs(el, p, dict(ctx.fmt, b=True), ctx)
            return [self.emit_p(p, ctx)]
        p = P(style, page_break="pb" in el.cls)
        if p.page_break:
            self.pending_break = False
        hid = el.attrs.get("id")
        if lvl <= 3:
            name = self.anchors.get(hid) if hid else None
            if not name:
                self.bm_auto = getattr(self, "bm_auto", 0) + 1
                name = f"_Toc_p{self.bm_auto}"
            p.bookmark = self.bookmark(name)
        num = self._heading_runs(el, p, {}, ctx)
        if lvl <= 3:
            txt = re.sub(r"\s+", " ", p.plain()).strip()
            if num and txt.startswith(num):
                txt = txt[len(num):].strip()
            self.headings.append((lvl, p.bookmark[0], num, txt))
        return [self.emit_p(p, ctx)]

    def _heading_runs(self, el, p, fmt, ctx):
        num = ""
        for k in el.kids:
            if isinstance(k, El) and k.tag == "span" and "n" in k.cls and not num:
                num = re.sub(r"\s+", " ", k.text()).strip()
                nf = dict(fmt)
                c = self.num_color.get(el.tag)
                if c:
                    nf["color"] = c
                p.text(num, nf)
                p.tab(fmt)
            elif isinstance(k, str):
                p.text(k, fmt)
            else:
                self.inline(k, p, fmt, ctx)
        return num

    def paragraph(self, el, ctx):
        cls = el.cls
        kw = {}
        fmt = dict(ctx.fmt)
        style = ctx.style
        if "small" in cls and not ctx.cell:
            style = "SmallText"
        if "muted" in cls:
            fmt["color"] = self.c["grey"]
        if "kwn" in cls:
            kw["keep_next"] = True
        props = self.cls_props(el)
        if props:
            fmt.update(self.props_fmt(props, ctx.size))
            if props.get("text-align"):
                kw["jc"] = _jc(props["text-align"])
            if "margin" in props or "margin-top" in props:
                parts = (props.get("margin") or "").split()
                top = _len(props.get("margin-top") or (parts[0] if parts else ""), ctx.size / 2)
                if top:
                    kw["before"] = round(top * 20)
        st = (el.attrs.get("style") or "").replace(" ", "").lower()
        m = re.search(r"text-align:(left|right|center|justify)", st)
        if m:
            kw["jc"] = _jc(m.group(1))
        if el.has_block():
            return self.flow(el, ctx.but(style=style, fmt=fmt))
        p = self.new_p(ctx, style=style, **kw)
        for k in el.kids:
            if isinstance(k, str):
                p.text(k, fmt)
            else:
                self.inline(k, p, fmt, ctx)
        return [self.emit_p(p, ctx)] if p.has_content() else []

    def pre(self, el, ctx):
        p = self.new_p(ctx, style="CodeBlock", pre=True)
        lines = el.text().strip("\n").split("\n")
        for i, line in enumerate(lines):
            if i:
                p.br()
            if line:
                p.text(line, {})
        return [self.emit_p(p, ctx)]

    # ------------------------------------------------------------------ lists
    def new_num(self, ordered, start, ilvl):
        nid = len(self.nums) + 1
        self.nums.append((nid, 1 if ordered else 0, ilvl, start))
        return nid

    def list(self, el, ctx, depth):
        ordered = el.tag == "ol"
        try:
            start = int(el.attrs.get("start") or 1)
        except ValueError:
            start = 1
        nid = self.new_num(ordered, start, min(depth, 8))
        out = []
        for k in el.kids:
            if isinstance(k, str):
                if k.strip():
                    p = self.new_p(ctx, style="ListParagraph", num=(nid, min(depth, 8)), contextual=True)
                    p.text(k, ctx.fmt)
                    out.append(self.emit_p(p, ctx))
            elif k.tag == "li":
                out += self.list_item(k, ctx, nid, depth)
            elif k.tag in ("ul", "ol"):
                out += self.list(k, ctx, depth + 1)
            elif k.tag not in SKIP:
                out += self.block(k, ctx)
        return out

    def list_item(self, li, ctx, nid, depth):
        lv = min(depth, 8)
        left = LIST_LEFT + LIST_STEP * lv
        out, p, first = [], None, True
        sub = ctx.but(style="ListParagraph", indent=0, jc=ctx.jc if ctx.jc != "both" else None)
        if ctx.cell:
            sub = sub.but(style="ListParagraph")
        props = self.cls_props(li)
        fmt = dict(ctx.fmt, **self.props_fmt(props, ctx.size)) if props else ctx.fmt

        def flush():
            nonlocal p, first
            if p is not None and p.has_content():
                if first:
                    p.num = (nid, lv)
                    first = False
                else:
                    p.ind = {"left": left}
                out.append(self.emit_p(p, sub))
            p = None
        for k in li.kids:
            if isinstance(k, str):
                if p is None:
                    if not k.strip():
                        continue
                    p = self.new_p(sub, contextual=True)
                p.text(k, fmt)
            elif k.tag in ("ul", "ol"):
                flush()
                out += self.list(k, ctx, depth + 1)
            elif k.tag == "p" and not k.has_block():
                flush()
                p = self.new_p(sub, contextual=True)
                for kk in k.kids:
                    if isinstance(kk, str):
                        p.text(kk, fmt)
                    else:
                        self.inline(kk, p, fmt, sub)
                flush()
            elif k.tag in BLOCK or k.has_block():
                flush()
                if first:                                   # a list item that starts with a block: number on an empty line
                    q = self.new_p(sub, contextual=True, num=(nid, lv))
                    q.raw('<w:r><w:t xml:space="preserve"> </w:t></w:r>')
                    out.append(self.emit_p(q, sub))
                    first = False
                out += self.block(k, ctx.but(indent=left, width=ctx.width - left))
            elif k.tag not in SKIP:
                if p is None:
                    p = self.new_p(sub, contextual=True)
                self.inline(k, p, fmt, sub)
        flush()
        return out

    # ------------------------------------------------------------------ tables
    def table(self, el, ctx):
        rows = []                                           # (tr element, header?)
        caption = None
        for k in el.els():
            if k.tag == "caption":
                caption = k
            elif k.tag in ("thead", "tbody", "tfoot"):
                for tr in k.els():
                    if tr.tag == "tr":
                        rows.append((tr, k.tag == "thead"))
            elif k.tag == "tr":
                rows.append((k, False))
        if not rows:
            return self.flow(el, ctx)
        if not any(h for _, h in rows):                     # a first row of only th is a header
            first = [c for c in rows[0][0].els() if c.tag in ("td", "th")]
            if first and all(c.tag == "th" for c in first) and len(rows) > 1:
                rows[0] = (rows[0][0], True)
        # grid placement with colspan/rowspan
        occ = {}
        placed = []                                         # per row: list of (col, cs, rs, cell)
        ncols = 0
        for ri, (tr, _) in enumerate(rows):
            col, row = 0, []
            for c in tr.els():
                if c.tag not in ("td", "th"):
                    continue
                while occ.get((ri, col)):
                    col += 1
                cs = _int(c.attrs.get("colspan"), 1)
                rs = _int(c.attrs.get("rowspan"), 1)
                rs = max(1, min(rs, len(rows) - ri))
                for dr in range(rs):
                    for dc in range(cs):
                        occ[(ri + dr, col + dc)] = (ri, col)
                row.append((col, cs, rs, c))
                col += cs
            ncols = max(ncols, col)
            placed.append(row)
        ncols = max(ncols, max([k[1] + 1 for k in occ] or [1]))
        widths = self.col_widths(el, placed, ncols, ctx.width)
        out = []
        if self.pending_break and not ctx.cell:
            out.append(self.page_break_p())
        long_table = len(rows) > 18
        last_row = len(rows) - 1
        trs = []
        mar_lr = 113
        for ri, (tr, header) in enumerate(rows):
            trc = tr.cls
            total = "total" in trc
            hl = "hl" in trc
            starts = {col: (cs, rs, c) for col, cs, rs, c in placed[ri]}
            tcs, col = [], 0
            keep = (not long_table) or header or ri >= last_row - 1
            if caption is not None and ri == last_row:
                keep = True
            while col < ncols:
                if col in starts:
                    cs, rs, c = starts[col]
                    w = sum(widths[col:col + cs])
                    is_th = header or c.tag == "th"
                    fill = self.c["th-bg"] if is_th else (self.c["tint"] if hl else self.c["light"])
                    if self.ruled:
                        fill = self.c["tint"] if hl and not is_th else None
                    fmt = {}
                    if is_th:
                        fmt = {"b": True, "color": self.c["th-ink"]}
                    if total:
                        fmt["b"] = True
                    ccls = c.cls
                    jc = "right" if ("n" in ccls or (_jc(c.attrs.get("align")) == "right")) else (
                        "center" if "c" in ccls or c.attrs.get("align") == "center" else None)
                    st = (c.attrs.get("style") or "").replace(" ", "").lower()
                    m = re.search(r"text-align:(left|right|center)", st)
                    if m:
                        jc = _jc(m.group(1))
                    sub = Ctx(w - 2 * mar_lr, style="TableText", fmt=fmt, cell=True, jc=jc, size=18,
                              keep=keep and (ri < last_row or caption is not None))
                    blocks = ensure_p(self.flow(c, sub))
                    tcpr = [f'<w:tcW w:w="{w}" w:type="dxa"/>']
                    if cs > 1:
                        tcpr.append(f'<w:gridSpan w:val="{cs}"/>')
                    if rs > 1:
                        tcpr.append('<w:vMerge w:val="restart"/>')
                    bd = {"top": ("single", 8, self.c["ink"])} if total else {}
                    if self.ruled:
                        bd["bottom"] = ("single", 10, self.c["strong"]) if is_th else ("single", 4, self.c["rule"])
                    if bd:
                        tcpr.append(tbl_borders(bd, "tcBorders"))
                    if fill:
                        tcpr.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>')
                    tcpr.append(f'<w:vAlign w:val="{"bottom" if is_th else "top"}"/>')
                    tcs.append(f"<w:tc><w:tcPr>{''.join(tcpr)}</w:tcPr>{''.join(blocks)}</w:tc>")
                    col += cs
                elif (ri, col) in occ:                       # under a rowspan
                    r0, c0 = occ[(ri, col)]
                    cs = next((k[1] for k in placed[r0] if k[0] == c0), 1)
                    w = sum(widths[col:col + cs])
                    hdr = rows[r0][1]
                    fill = "auto" if self.ruled else (self.c["th-bg"] if hdr else self.c["light"])
                    tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>' + (f'<w:gridSpan w:val="{cs}"/>' if cs > 1 else "")
                               + f'<w:vMerge/><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/></w:tcPr>' + ensure_p([])[0] + "</w:tc>")
                    col += cs
                else:                                        # short row: fill the grid
                    tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[col]}" w:type="dxa"/><w:shd w:val="clear" w:color="auto" '
                               f'w:fill="{"auto" if self.ruled else self.c["light"]}"/></w:tcPr>' + ensure_p([])[0] + "</w:tc>")
                    col += 1
            trpr = "<w:cantSplit/>" + ("<w:tblHeader/>" if header else "")
            trs.append(f"<w:tr><w:trPr>{trpr}</w:trPr>{''.join(tcs)}</w:tr>")
        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
        tw = sum(widths)
        ind = f'<w:tblInd w:w="{ctx.indent}" w:type="dxa"/>' if ctx.indent else ""
        tblpr = (f'<w:tblPr><w:tblStyle w:val="PaiperTable"/><w:tblW w:w="{tw}" w:type="dxa"/>{ind}'
                 + tbl_borders({"insideH": ("single", 12, "FFFFFF")})
                 + '<w:tblLayout w:type="fixed"/>' + cell_mar(60, mar_lr, 60, mar_lr)
                 + '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="1" w:noVBand="1"/></w:tblPr>')
        out.append(f"<w:tbl>{tblpr}<w:tblGrid>{grid}</w:tblGrid>{''.join(trs)}</w:tbl>")
        if caption is not None:
            self.tbl_n += 1
            p = P("Caption", ind={"left": ctx.indent} if ctx.indent else None)
            p.text("Table ", {})
            p.field("SEQ Table \\* ARABIC", str(self.tbl_n))
            p.text(": ", {})
            for k in caption.kids:
                if isinstance(k, str):
                    p.text(k, {})
                else:
                    self.inline(k, p, {}, ctx)
            out.append(self.emit_p(p, ctx))
        else:
            out.append(self.spacer(ctx))
        return out

    def spacer(self, ctx, after=0, size=8):
        return f'<w:p><w:pPr><w:spacing w:before="0" w:after="{after}" w:line="240" w:lineRule="auto"/><w:rPr><w:sz w:val="{size}"/></w:rPr></w:pPr></w:p>'

    def col_widths(self, el, placed, ncols, total):
        pct = [None] * ncols
        cg = el.find("colgroup")
        cols = [c for c in (cg.els() if cg is not None else [k for k in el.els() if k.tag == "col"]) if c.tag == "col"]
        i = 0
        for c in cols:
            span = _int(c.attrs.get("span"), 1)
            w = c.attrs.get("width") or ""
            m = re.search(r"width:\s*([\d.]+)(%|px|pt|cm|mm)", c.attrs.get("style") or "") or re.match(r"([\d.]+)(%|px)?$", w.strip())
            for _ in range(span):
                if i < ncols and m:
                    v = float(m.group(1))
                    unit = m.group(2) or "px"
                    pct[i] = v / 100 if unit == "%" else (_len(f"{v}{unit}") or 0) * 20 / total
                i += 1
        if all(p is not None for p in pct) and sum(pct) > 0:
            s = sum(pct)
            ws = [max(300, round(total * p / s)) for p in pct]
        else:
            ch = 100                                        # twips per character at 9 pt
            mn, mx = [400] * ncols, [400] * ncols
            for row in placed:
                for col, cs, rs, c in row:
                    if cs != 1:
                        continue
                    t = re.sub(r"\s+", " ", c.text()).strip()
                    words = [len(x) for x in t.split(" ")] or [0]
                    words += [len(_chip.text().strip()) + 2 for _chip in _find_all(c, "chip")]
                    has_img = c.find("img") is not None or c.find("x-svg") is not None
                    mn[col] = max(mn[col], min(max(words), 22) * ch + 300, 2400 if has_img else 0)
                    mx[col] = max(mx[col], len(t) * ch + 300, mn[col])
            fixed = sum(round(total * p) for p in pct if p is not None)
            free = [i for i in range(ncols) if pct[i] is None]
            room = max(total - fixed, 400 * len(free))
            smn, smx = sum(mn[i] for i in free), sum(mx[i] for i in free)
            ws = [0] * ncols
            for i in range(ncols):
                if pct[i] is not None:
                    ws[i] = round(total * pct[i])
                elif smx <= room:
                    ws[i] = mx[i] + (room - smx) * mx[i] / smx
                elif smn >= room:
                    ws[i] = mn[i] * room / smn
                else:
                    ws[i] = mn[i] + (mx[i] - mn[i]) * (room - smn) / (smx - smn)
            ws = [max(300, round(w)) for w in ws]
        diff = total - sum(ws)                              # exact total
        ws[ws.index(max(ws))] += diff
        return ws

    def box_table(self, cells, widths, ctx, borders=None, mar=(0, 0, 0, 0), tblpr_extra="", rows_keep=True):
        """A layout table: cells = [[(blocks, tcPr extra)]] rows; no visible grid."""
        trs = []
        for row in cells:
            tcs = []
            for i, (blocks, extra) in enumerate(row):
                tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[i]}" w:type="dxa"/>{extra}</w:tcPr>{"".join(ensure_p(blocks))}</w:tc>')
            trs.append("<w:tr><w:trPr><w:cantSplit/></w:trPr>" + "".join(tcs) + "</w:tr>")
        ind = f'<w:tblInd w:w="{ctx.indent}" w:type="dxa"/>' if ctx.indent else ""
        tblpr = (f'<w:tblPr>{tblpr_extra}<w:tblW w:w="{sum(widths)}" w:type="dxa"/>{ind}' + tbl_borders(borders or {})
                 + '<w:tblLayout w:type="fixed"/>' + cell_mar(*mar)
                 + '<w:tblLook w:val="0000" w:firstRow="0" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="1" w:noVBand="1"/></w:tblPr>')
        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
        out = []
        if self.pending_break and not ctx.cell:
            out.append(self.page_break_p())
        out.append(f"<w:tbl>{tblpr}<w:tblGrid>{grid}</w:tblGrid>{''.join(trs)}</w:tbl>")
        return out

    # ------------------------------------------------------------------ callouts, kpis, cards, photos
    def callout(self, el, ctx, todo=False):
        w = ctx.width
        inner = Ctx(w - 480, fmt=dict(ctx.fmt), cell=True, after=80, keep=True, size=ctx.size)
        blocks = []
        rest = El("div")
        for k in el.kids:
            if isinstance(k, El) and "t" in k.cls and not blocks:
                p = self.new_p(inner, keep_next=True, after=40)
                f = {"b": True}
                if todo:
                    f.update(color=self.c["accent"], caps=True, size=15, spacing=30)
                for kk in k.kids:
                    if isinstance(kk, str):
                        p.text(kk, f)
                    else:
                        self.inline(kk, p, f, inner)
                blocks.append(self.emit_p(p, inner))
            else:
                rest.kids.append(k)
        blocks += self.flow(rest, inner)
        if blocks and blocks[-1].startswith("<w:p>"):           # no space under the last paragraph
            blocks[-1] = re.sub(r'w:after="\d+"', 'w:after="0"', blocks[-1], count=1)
        if todo:
            sz, c, _ = self.border_of(".todo", "top", self.c["accent"], 12)
            bd = {s: ("dashed", 12, self.c["accent"]) for s in ("top", "left", "bottom", "right")}
            extra = tbl_borders(bd, "tcBorders")
        else:
            sz, c, _ = self.border_of(".callout", "left", self.c["accent"], 24)
            extra = tbl_borders({"left": ("single", sz, c)}, "tcBorders") + f'<w:shd w:val="clear" w:color="auto" w:fill="{self.c["light"]}"/>'
        out = self.box_table([[(blocks, extra)]], [w], ctx, mar=(150, 280, 150, 200))
        return out + [self.spacer(ctx, after=120)]

    def _tiles(self, items, per_row, ctx, gap=170):
        n = min(per_row, len(items))
        w = ctx.width
        cw = (w - gap * (n - 1)) // n
        widths = []
        for i in range(n):
            widths += [cw] + ([gap] if i < n - 1 else [])
        widths[0] += w - sum(widths)
        rows = []
        for r in range(0, len(items), n):
            chunk = items[r:r + n]
            row = []
            for i in range(n):
                row.append(chunk[i] if i < len(chunk) else ([], ""))
                if i < n - 1:
                    row.append(([], ""))
            rows.append(row)
        return rows, widths, cw

    def kpis(self, el, ctx):
        kp = [k for k in el.els() if "kpi" in k.cls]
        if not kp:
            return self.flow(el, ctx)
        per = 4 if len(kp) == 4 else 3
        _, _, cw = self._tiles(kp, per, ctx)
        tiles = []
        for k in kp:
            hot = "hot" in k.cls
            vc = self.c["accent"] if hot else self.c["strong"]
            inner = Ctx(cw - 400, cell=True, after=0, jc="left", size=18)
            blocks = []
            for part in k.els():
                pc = part.cls
                if "v" in pc:
                    p = P(None, after=40, line=240, jc="left")
                    for kk in part.kids:
                        if isinstance(kk, str):
                            p.text(kk, {"b": True, "size": 44, "color": vc})
                        elif kk.tag == "small":
                            p.text(kk.text(), {"size": 22, "color": self.c["grey"], "b": False})
                        else:
                            self.inline(kk, p, {"b": True, "size": 44, "color": vc}, inner)
                    blocks.append(self.emit_p(p, inner))
                elif "l" in pc:
                    p = P(None, after=20, line=252, jc="left")
                    self._inline_kids(part, p, {"size": 18}, inner)
                    blocks.append(self.emit_p(p, inner))
                elif "s" in pc:
                    p = P(None, after=0, line=252, jc="left")
                    self._inline_kids(part, p, {"size": 16, "color": self.c["grey"]}, inner)
                    blocks.append(self.emit_p(p, inner))
                else:
                    blocks += self.block(part, inner)
            sz, c, _ = self.border_of(".kpi", "top", self.c["strong"], 24)
            if hot:
                c = self.c["accent"]
            extra = tbl_borders({"top": ("single", sz, c)}, "tcBorders") + f'<w:shd w:val="clear" w:color="auto" w:fill="{self.c["light"]}"/>'
            tiles.append((blocks, extra))
        rows, widths, _ = self._tiles(tiles, per, ctx)
        return self.box_table(rows, widths, ctx, mar=(140, 200, 140, 200)) + [self.spacer(ctx, after=160)]

    def _inline_kids(self, el, p, fmt, ctx):
        for k in el.kids:
            if isinstance(k, str):
                p.text(k, fmt)
            else:
                self.inline(k, p, fmt, ctx)

    def cards(self, el, ctx):
        cs = [k for k in el.els() if "card" in k.cls]
        if not cs:
            return self.flow(el, ctx)
        _, _, cw = self._tiles(cs, 2, ctx)
        sz, c, _ = self.border_of(".card", "top", self.c["accent"], 24)
        tiles = []
        for k in cs:
            inner = Ctx(cw - 400, cell=True, after=60, jc="left", size=18, fmt={"size": 18})
            blocks = []
            for kk in k.kids:
                if isinstance(kk, El) and kk.tag in ("h3", "h4", "h5"):
                    p = P(None, after=60, keep_next=True)
                    self._inline_kids(kk, p, {"b": True}, inner)
                    blocks.append(self.emit_p(p, inner))
                else:
                    tmp = El("div")
                    tmp.kids = [kk]
                    blocks += self.flow(tmp, inner)
            extra = tbl_borders({"top": ("single", sz, c)}, "tcBorders") + f'<w:shd w:val="clear" w:color="auto" w:fill="{self.c["light"]}"/>'
            tiles.append((blocks, extra))
        rows, widths, _ = self._tiles(tiles, 2, ctx)
        return self.box_table(rows, widths, ctx, mar=(120, 200, 100, 200)) + [self.spacer(ctx, after=160)]

    def photos(self, el, ctx, per):
        figs = [k for k in el.els() if k.tag in ("figure", "img", "x-svg")]
        if not figs:
            return self.flow(el, ctx)
        n = per
        gap = 200
        cw = (ctx.width - gap * (n - 1)) // n
        box_h = (6.2 if per == 2 else 4.2) * 567
        tiles = []
        for f in figs:
            inner = Ctx(cw, cell=True, size=18)
            tiles.append((self.figure(f, inner, box=(cw, box_h)) if f.tag == "figure" else
                          [p_xml(self._img_p(f, inner, (cw, box_h)), self.link_xml)], ""))
        rows = []
        for r in range(0, len(tiles), n):
            chunk = tiles[r:r + n]
            row = []
            for i in range(n):
                row.append(chunk[i] if i < len(chunk) else ([], ""))
                if i < n - 1:
                    row.append(([], ""))
            rows.append(row)
        widths = []
        for i in range(n):
            widths += [cw] + ([gap] if i < n - 1 else [])
        widths[0] += ctx.width - sum(widths)
        return self.box_table(rows, widths, ctx, mar=(0, 0, 0, 0)) + [self.spacer(ctx, after=120)]

    # ------------------------------------------------------------------ figures and images
    def figure(self, el, ctx, box=None):
        out = []
        cap = None
        media = []
        for k in el.kids:
            if isinstance(k, El) and k.tag == "figcaption":
                cap = k
        nonum = "nonum" in el.cls
        pend = []
        for k in el.kids:
            if isinstance(k, str):
                if k.strip():
                    pend.append(k)
                continue
            if k.tag == "figcaption" or k.tag in SKIP:
                continue
            img = k if k.tag in ("img", "x-svg") else (k.find("img") or k.find("x-svg") if k.tag in ("a", "picture", "div", "p") and not k.text().strip() else None)
            if img is not None:
                media.append(img)
                p = self._img_p(img, ctx, box)
                p.keep_next = cap is not None
                out.append(self.emit_p(p, ctx))
            elif k.tag in NOT_RENDERED:
                out.append(self.note_p(ctx.but(keep=cap is not None), "[figure: see PDF]"))
            else:
                out += self.block(k, ctx) if (k.tag in BLOCK or k.has_block()) else self.flow(_wrap(k), ctx)
        if pend:
            out += self.flow(_wrap(*pend), ctx)
        if not nonum:
            self.fig_n += 1
        if cap is not None:
            p = P("Caption", after=60 if ctx.cell else None, ind={"left": ctx.indent} if ctx.indent else None)
            ft, fd = cap.find(cls="ft"), cap.find(cls="fd")
            if not nonum:
                p.text("Figure ", {})
                p.field("SEQ Figure \\* ARABIC", str(self.fig_n))
                p.text(": ", {})
            if ft is None and fd is None:
                self._inline_kids(cap, p, {}, ctx)
            else:
                if ft is not None:
                    self._inline_kids(ft, p, {}, ctx)
                if fd is not None:
                    if ft is not None:
                        p.br()
                    self._inline_kids(fd, p, {"i": False}, ctx)
            out.append(self.emit_p(p, ctx))
        return [x for x in out if x]

    def _img_p(self, img, ctx, box=None):
        p = self.new_p(ctx, after=80 if not ctx.cell else 60, before=None if ctx.cell else 120, keep_next=True, jc=None)
        p.style = None if not ctx.cell else "TableText"
        if img.tag == "x-svg":
            p.raw(self.svg_run(img, ctx.width, box))
        else:
            p.raw(self.img_run(img, ctx.width, box))
        return p

    def load_image(self, src):
        """(part name, w px, h px) of an image in the document folder (converted when Word cannot show it)."""
        if src in self.media:
            return self.media[src]
        data = None
        try:
            if src.startswith("data:"):
                head, _, b64 = src.partition(",")
                data = base64.b64decode(b64) if ";base64" in head else unquote(b64).encode()
            elif not re.match(r"^[a-z]+:", src, re.I) or src.startswith("file:"):
                path = unquote(src[7:] if src.startswith("file://") else src.split("?")[0].split("#")[0])
                path = path if os.path.isabs(path) else os.path.join(self.doc.dir, path)
                with open(path, "rb") as f:
                    data = f.read()
        except (OSError, ValueError):
            data = None
        info = image_size(data) if data else None
        res = None
        if info:
            kind, w, h = info
            if kind == "svg":
                res = self.svg_to_png(data.decode("utf-8", "replace"), 1600)
                if res:
                    data, w, h = res
                    kind = "png"
                else:
                    info = None
            elif kind == "webp":
                conv = self.convert_with_sips(data)
                if conv:
                    data = conv
                    kind, w, h = image_size(data)
                else:
                    info = None
            if info:
                name = f"media/image{len(self.media_parts) + 1}.{'jpeg' if kind == 'jpeg' else kind}"
                self.media_parts[name] = data
                res = (name, w, h)
        self.media[src] = res
        return res

    def convert_with_sips(self, data):
        if not self.sips:
            return None
        a, b = os.path.join(self.tmp, "in.webp"), os.path.join(self.tmp, "out.png")
        with open(a, "wb") as f:
            f.write(data)
        r = subprocess.run([self.sips, "-s", "format", "png", a, "--out", b], capture_output=True)
        if r.returncode or not os.path.exists(b):
            return None
        with open(b, "rb") as f:
            return f.read()

    def img_run(self, img, max_w, box=None):
        src = img.attrs.get("src") or ""
        got = self.load_image(src) if src else None
        alt = img.attrs.get("alt") or ""
        if not got:
            self.notes.append(f"image not embedded: {src[:80]}")
            return run(f"[image: {alt or os.path.basename(src) or 'see PDF'}]", {"i": True, "color": self.c["grey"]})
        name, w, h = got
        st = (img.attrs.get("style") or "").replace(" ", "").lower()
        photo = "photo" in img.cls or "chart" in img.cls
        if photo:
            cx = max_w
        else:
            aw = _len(img.attrs.get("width") or "")
            cx = min(max_w, round((aw or w * .75) * 20))
        cy = cx * h / max(w, 1)
        crop = None
        if box:
            bw, bh = box
            cx, cy, crop = _fit_box(w, h, bw, bh)
        else:
            max_h = 9 * 567 if photo and "max-height:none" not in st else 21 * 567
            if cy > max_h:
                cy = max_h
                cx = cy * w / max(h, 1)
        return self.drawing(name, cx, cy, alt, crop)

    def svg_run(self, el, max_w, box=None):
        src = self.svgs[int(el.attrs.get("data-i", 0))]
        m = re.match(r"<svg\b[^>]*>", src, re.I)
        head = m.group(0) if m else ""
        vb = re.search(r'viewBox\s*=\s*["\']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', head, re.I)
        wa = re.search(r'\swidth\s*=\s*["\']?\s*([\d.]+)(px|pt|%)?', head)
        ha = re.search(r'\sheight\s*=\s*["\']?\s*([\d.]+)(px|pt|%)?', head)
        if vb:
            vw, vh = float(vb.group(1)), float(vb.group(2))
        elif wa and ha and wa.group(2) != "%" and ha.group(2) != "%":
            vw, vh = float(wa.group(1)), float(ha.group(1))
        else:
            vw, vh = 720.0, 360.0
        chart = 'class="chart' in head or (wa and wa.group(2) == "%") or not wa
        cx = max_w if chart else min(max_w, round(float(wa.group(1)) * (1 if wa.group(2) == "pt" else .75) * 20))
        cy = cx * vh / max(vw, 1)
        crop = None
        if box:
            cx, cy, crop = _fit_box(vw, vh, box[0], box[1], allow_crop=False)
        if cy > 21 * 567:
            cy = 21 * 567
            cx = cy * vw / vh
        px = max(int(vw * 2), int(cx / 1440 * 220))
        res = self.svg_to_png(src, px)
        if not res:
            label = re.search(r'aria-label="([^"]*)"', head)
            self.notes.append("SVG not converted (rsvg-convert missing or failed)")
            return run("[figure: see PDF" + (f": {label.group(1)}" if label else "") + "]", {"i": True, "color": self.c["grey"]})
        data, w, h = res
        name = f"media/image{len(self.media_parts) + 1}.png"
        self.media_parts[name] = data
        label = re.search(r'aria-label="([^"]*)"', head)
        return self.drawing(name, cx, cy, html.unescape(label.group(1)) if label else "", crop)

    def font_stack(self):
        names = []
        fdir = os.path.join(self.tdir, "fonts")
        if os.path.isdir(fdir):
            for n in sorted(os.listdir(fdir)):
                if n.lower().endswith((".ttf", ".otf")):
                    try:
                        with open(os.path.join(fdir, n), "rb") as f:
                            fam = font_info(f.read())[0]
                    except (OSError, struct.error):
                        fam = None
                    if fam and fam not in names:
                        names.append(fam)
        css = [x.strip().strip("'\"") for x in (self.var("var(--font)") or "").split(",") if x.strip()]
        out = []
        for n in names + [self.font] + css:
            if n and n not in out:
                out.append(n)
        return ", ".join(("'" + n + "'") if " " in n else n for n in out)

    def fc_env(self):
        """Environment for rsvg-convert so text renders in the theme's own fonts (fontconfig with the theme's fonts dir)."""
        if self._fc is None:
            conf = os.path.join(self.tmp, "fonts.conf")
            cache = os.path.join(os.path.expanduser("~"), ".cache", "paiper", "fontconfig")
            os.makedirs(cache, exist_ok=True)
            fdir = os.path.join(self.tdir, "fonts")
            inc = "".join(f'<include ignore_missing="yes">{p}</include>' for p in
                          ("/opt/homebrew/etc/fonts/fonts.conf", "/usr/local/etc/fonts/fonts.conf", "/etc/fonts/fonts.conf"))
            with open(conf, "w") as f:
                f.write(f'<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
                        f'{"<dir>" + xt(fdir) + "</dir>" if os.path.isdir(fdir) else ""}{inc}<cachedir>{xt(cache)}</cachedir></fontconfig>')
            self._fc = dict(os.environ, FONTCONFIG_FILE=conf, PANGOCAIRO_BACKEND="fc")
        return self._fc

    def prepare_svg(self, src):
        s = src
        for _ in range(6):
            if "var(" not in s:
                break
            s = self.var(s)
        s = re.sub(r"currentColor", "#" + self.c["ink"], s)
        s = re.sub(r"&([a-zA-Z][a-zA-Z0-9]*);", lambda m: m.group(0) if m.group(1) in ("amp", "lt", "gt", "quot", "apos")
                   else (f"&#{name2codepoint[m.group(1)]};" if m.group(1) in name2codepoint else ""), s)
        m = re.search(r"<svg\b[^>]*>", s, re.I)
        if not m:
            return None
        head = m.group(0)
        new = head
        if "xmlns=" not in head:
            new = new[:4] + ' xmlns="http://www.w3.org/2000/svg"' + new[4:]
        if "xlink:" in s and "xmlns:xlink" not in head:
            new = new[:4] + ' xmlns:xlink="http://www.w3.org/1999/xlink"' + new[4:]
        new = re.sub(r'\s(width|height)\s*=\s*"[\d.]*%"', "", new)
        stack = self.font_stack()
        css = "text, tspan { font-family: %s; }" % stack
        if self.doc_css:
            css += re.sub(r"[<>]", "", self.var(re.sub(r"/\*.*?\*/", "", self.doc_css, flags=re.S)))
        style = f"<style>{css}</style>"
        s = s[:m.start()] + new + style + s[m.end():]

        def inline_href(mm):
            href = mm.group(2)
            if re.match(r"^(data:|#|https?:)", href):
                return mm.group(0)
            path = os.path.join(self.doc.dir, unquote(href))
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                return mm.group(0)
            info = image_size(data)
            mime = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "svg": "image/svg+xml"}.get(info[0] if info else "", "")
            return f'{mm.group(1)}="data:{mime};base64,{base64.b64encode(data).decode()}"' if mime else mm.group(0)
        s = re.sub(r'((?:xlink:)?href)\s*=\s*"([^"]+)"', inline_href, s)
        return s

    def svg_to_png(self, src, px_w):
        if not self.rsvg:
            return None
        s = self.prepare_svg(src)
        if s is None:
            return None
        if not _xml_ok(s):
            s = _repair_svg(s)
        a, b = os.path.join(self.tmp, "fig.svg"), os.path.join(self.tmp, "fig.png")
        with open(a, "w", encoding="utf-8") as f:
            f.write(s)
        if os.path.exists(b):
            os.remove(b)
        r = subprocess.run([self.rsvg, "-w", str(int(px_w)), "-f", "png", "-o", b, a], capture_output=True, env=self.fc_env())
        if r.returncode or not os.path.exists(b):
            return None
        with open(b, "rb") as f:
            data = f.read()
        info = image_size(data)
        if not info:
            return None
        return data, info[1], info[2]

    def drawing(self, name, cx, cy, alt="", crop=None, rels=None, anchor=None):
        """Run XML with an inline (or anchored) picture. cx, cy in twips; anchor = (x, y, behind) in twips from the page."""
        rid = (rels or self.rels).add("image", name)
        i = self.next_id()
        ex, ey = int(cx * EMU_TW), int(cy * EMU_TW)
        src = ""
        if crop:
            src = "<a:srcRect " + " ".join(f'{k}="{int(v)}"' for k, v in crop.items() if v) + "/>"
        graphic = (f'<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
                   f'<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="{i}" name="{xt(os.path.basename(name))}"/><pic:cNvPicPr/></pic:nvPicPr>'
                   f'<pic:blipFill><a:blip r:embed="{rid}"/>{src}<a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                   f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{ex}" cy="{ey}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>')
        docpr = f'<wp:docPr id="{i}" name="Picture {i}"{" descr=" + chr(34) + xt(alt) + chr(34) if alt else ""}/>'
        lock = '<wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        if anchor:
            x, y, behind = anchor
            body = (f'<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="{251658240 + i}" behindDoc="{1 if behind else 0}" '
                    f'locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/>'
                    f'<wp:positionH relativeFrom="page"><wp:posOffset>{int(x * EMU_TW)}</wp:posOffset></wp:positionH>'
                    f'<wp:positionV relativeFrom="page"><wp:posOffset>{int(y * EMU_TW)}</wp:posOffset></wp:positionV>'
                    f'<wp:extent cx="{ex}" cy="{ey}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>{docpr}{lock}{graphic}</wp:anchor>')
        else:
            body = (f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{ex}" cy="{ey}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
                    f'{docpr}{lock}{graphic}</wp:inline>')
        return f"<w:r><w:drawing>{body}</w:drawing></w:r>"

    # ------------------------------------------------------------------ cover
    def cover(self):
        doc, c = self.doc, self.c
        cv = doc.meta.get("cover", {})
        croot, _ = parse_html(doc.theme.cover or "")
        cover_rules = {}
        for sel, props in self.rules:
            if sel.startswith(".cover "):
                cover_rules.setdefault(sel[7:], {}).update(props)

        def rule_for(el):
            out = {}
            for k in sorted(el.cls):
                out.update(cover_rules.get("." + k, {}))
            return out
        fields = {"kicker": cv.get("kicker", ""), "title": cv.get("title", doc.title), "subtitle": cv.get("subtitle", ""),
                  "org": doc.field("org"), "classification": doc.field("classification"), "website": doc.field("website"),
                  "client": doc.field("client") or cv.get("client", "")}

        def fill(s):
            return re.sub(r"\{\{(\w+)\}\}", lambda m: str(fields.get(m.group(1), doc.field(m.group(1)) or cv.get(m.group(1), "")) or ""), s)
        anchors, flow_el, blocks = [], {}, []
        for k in croot.kids:
            if isinstance(k, str):
                if "{{logo}}" in k and self.logo:
                    anchors.append(self.cover_logo(cover_rules.get(".logo", {})))
                continue
            raw = k.text()
            r = rule_for(k)
            role = next((x for x in ("kicker", "title", "subtitle") if "{{" + x + "}}" in raw), None)
            if role:
                for x in ("kicker", "title", "subtitle"):
                    if "{{" + x + "}}" in raw:
                        flow_el[x] = r
                continue
            if "{{logo}}" in raw and self.logo:
                anchors.append(self.cover_logo(cover_rules.get(".logo", {})))
                continue
            if not raw.strip():
                shape = self.cover_shape(r)
                if shape:
                    anchors.append(shape)
                continue
            if "{{client}}" in raw and not fields["client"]:
                continue
            blocks.append((k, r))
        meta_rule = cover_rules.get("table.meta", {})
        out = []
        # flow: kicker, title, subtitle at the theme's positions
        top_mm = self.top / TW_MM
        y = top_mm
        texts = [("kicker", fields["kicker"], "CoverKicker"), ("title", fields["title"], "Title"), ("subtitle", fields["subtitle"], "Subtitle")]
        first, title_ind = True, None
        for role, txt, style in texts:
            r = flow_el.get(role, {})
            f = self.props_fmt(r, 20)
            size_pt = (f.get("size") or {"kicker": 20, "title": 56, "subtitle": 26}[role]) / 2
            want = _mm(r.get("top"))
            p = P(style, jc=_jc(r.get("text-align")) or None)
            lft, rgt = _mm(r.get("left")), _mm(r.get("right"))
            if lft is not None and lft > 20.5 and not r.get("text-align") == "center":
                p.ind = {"left": round((lft - 20) * TW_MM)}
                if rgt is not None and rgt > 20.5:
                    p.ind["right"] = round((rgt - 20) * TW_MM)
            if role == "title":
                title_ind = p.ind
            if want is not None and want > y:
                p.before = round((want - y) * TW_MM)
                y = want
            elif not first:
                p.before = 120
            if first:
                for a in anchors:
                    p.raw(a)
                first = False
            if txt:
                p.text(txt, {})
            lines = max(1, math.ceil(len(txt) * size_pt * .52 / (TEXT_W / 20)))
            y += lines * size_pt * 1.25 / 72 * 25.4
            out.append(p_xml(p, self.link_xml))
        client = fields["client"]
        if client and "{{client}}" not in (doc.theme.cover or ""):
            p = P(None, before=600, after=0, jc=_jc(flow_el.get("title", {}).get("text-align")), ind=title_ind)
            p.text("Prepared for", {"caps": True, "size": 15, "color": c["grey"], "spacing": 20})
            out.append(p_xml(p, self.link_xml))
            p = P(None, before=40, jc=_jc(flow_el.get("title", {}).get("text-align")), ind=title_ind)
            p.text(client, {"size": 26, "color": c["accent"], "b": True})
            out.append(p_xml(p, self.link_xml))
        # positioned blocks: meta table and the theme's other text (organisation, address, prepared for)
        if doc.meta.get("meta"):
            out.append(self.cover_meta(meta_rule, cover_rules))
        for k, r in blocks:
            out.append(self.cover_block(k, r, cover_rules, fill))
        out.append('<w:p><w:pPr><w:spacing w:before="0" w:after="0"/></w:pPr></w:p>')
        self.pending_break = True
        return out

    def cover_logo(self, r):
        got = self.logo_image()
        if not got:
            return ""
        name, w, h = got
        hpt = _len(r.get("height"), 10) or 30
        cy = hpt * 20
        cx = cy * w / max(h, 1)
        x = (_mm(r.get("left")) or 20) * TW_MM
        y = (_mm(r.get("top")) or 20) * TW_MM
        return self.drawing(name, cx, cy, "logo", anchor=(x, y, False))

    def logo_image(self):
        if not self.logo:
            return None
        key = "theme-logo"
        if key in self.media:
            return self.media[key]
        path = os.path.join(self.tdir, self.logo)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self.media[key] = None
            return None
        info = image_size(data)
        res = None
        if info and info[0] == "svg":
            conv = self.svg_to_png(data.decode("utf-8", "replace"), 1200)
            if conv:
                data, w, h = conv
                info = ("png", w, h)
            else:
                info = None
        if info and info[0] in ("png", "jpeg", "gif"):
            name = f"media/image{len(self.media_parts) + 1}.{info[0]}"
            self.media_parts[name] = data
            res = (name, info[1], info[2])
        self.media[key] = res
        return res

    def cover_shape(self, r):
        """A decorative element of the cover (a coloured bar, circle or a faint image) as a picture behind the text."""
        bg = r.get("background") or r.get("background-color") or ""
        if r.get("position") != "absolute" or not bg:
            return None
        L, T, R, B = (_mm(r.get(k)) for k in ("left", "top", "right", "bottom"))
        W, H = _mm(r.get("width")), _mm(r.get("height"))
        pw, ph = 210.0, 297.0
        if W is None:
            W = pw - (L or 0) - (R or 0) if (L is not None and R is not None) else None
        if H is None:
            H = ph - (T or 0) - (B or 0) if (T is not None and B is not None) else None
        if W is None or H is None or W <= 0 or H <= 0:
            return None
        x = L if L is not None else pw - (R or 0) - W
        y = T if T is not None else ph - (B or 0) - H
        url = re.search(r"url\(['\"]?([^'\")]+)", bg)
        if url:
            path = os.path.join(self.tdir, url.group(1))
            try:
                with open(path, encoding="utf-8") as f:
                    svg = f.read()
            except OSError:
                return None
            op = r.get("opacity", "1")
            m = re.search(r"<svg\b[^>]*>", svg)
            if not m or "</svg>" not in svg:
                return None
            j = svg.rindex("</svg>")
            svg = svg[:m.end()] + f'<g opacity="{xt(op)}">' + svg[m.end():j] + "</g>" + svg[j:]
            res = self.svg_to_png(svg, int(W * 8))
            if not res:
                return None
            data, pw_px, ph_px = res
            # keep the aspect (background-size: contain)
            if pw_px / ph_px > W / H:
                nh = W * ph_px / pw_px
                y += (H - nh) / 2
                H = nh
            else:
                nw = H * pw_px / ph_px
                x += (W - nw) / 2
                W = nw
        else:
            col = self.color(bg.split()[0] if not bg.startswith("var(") else bg)
            if not col:
                return None
            ell = "50%" in (r.get("border-radius") or "")
            scale = 6
            data = shape_png(col, ell, max(2, int(W * scale)), max(2, int(H * scale))) if ell else shape_png(col, False, 4, 4)
        # clip to the page with a crop, so nothing hangs off the sheet
        vx0, vy0, vx1, vy1 = max(x, 0), max(y, 0), min(x + W, pw), min(y + H, ph)
        if vx1 <= vx0 or vy1 <= vy0:
            return None
        crop = {"l": (vx0 - x) / W * 100000, "t": (vy0 - y) / H * 100000, "r": (x + W - vx1) / W * 100000, "b": (y + H - vy1) / H * 100000}
        name = f"media/image{len(self.media_parts) + 1}.png"
        self.media_parts[name] = data
        return self.drawing(name, (vx1 - vx0) * TW_MM, (vy1 - vy0) * TW_MM, "", crop if any(v > 1 for v in crop.values()) else None,
                            anchor=(vx0 * TW_MM, vy0 * TW_MM, True))

    def floating(self, blocks, x, y, w, h=0):
        """A borderless table at a fixed spot on the page (cover blocks), kept inside the body area: Word and
        LibreOffice treat floating tables in the bottom margin differently (LibreOffice drops them)."""
        y = min(y, PAGE_H - MARGIN - h - 60)
        if w > TEXT_W:                                  # and inside the text width, or it is pushed to the next page
            x, w = MARGIN, TEXT_W
        x = max(MARGIN, min(x, PAGE_W - MARGIN - w))
        tblpr = (f'<w:tblPr><w:tblpPr w:leftFromText="0" w:rightFromText="0" w:vertAnchor="page" w:horzAnchor="page" '
                 f'w:tblpX="{int(max(0, x))}" w:tblpY="{int(max(0, y))}"/><w:tblW w:w="{int(w)}" w:type="dxa"/>'
                 + tbl_borders({}) + '<w:tblLayout w:type="fixed"/>' + cell_mar(0, 0, 0, 0)
                 + '<w:tblLook w:val="0000" w:firstRow="0" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="1" w:noVBand="1"/></w:tblPr>')
        return blocks(tblpr, int(w))

    def cover_meta(self, r, cover_rules):
        doc, c = self.doc, self.c
        rows = doc.meta.get("meta", [])
        f = self.props_fmt(r, 20)
        size = f.get("size") or 18
        first = cover_rules.get("table.meta td:first-child", {})
        lab_c = self.color(first.get("color", "")) or c["grey"]
        lab_w = (_len(first.get("width"), size / 2) or 0) * 20 or 1900
        colon = ": " if '": "' in (cover_rules.get("table.meta td:last-child::before", {}).get("content", "")) else ""
        val_w = 5200
        trs = []
        pad = (_len((cover_rules.get("table.meta td", {}).get("padding") or "2pt").split()[0], 10) or 2) * 20
        for k, v in rows:
            pl = P(None, before=0, after=0, line=240, jc="left")
            pl.text(str(k), {"size": size, "color": lab_c})
            pv = P(None, before=0, after=0, line=240, jc="left")
            if colon:
                pv.text(colon, {"size": size})
            vr, _ = parse_html(str(v))
            self._inline_kids(vr, pv, {"size": size}, Ctx(val_w))
            trs.append(f'<w:tr><w:trPr><w:cantSplit/></w:trPr><w:tc><w:tcPr><w:tcW w:w="{int(lab_w)}" w:type="dxa"/>{cell_mar(int(pad), 0, int(pad), 0, "tcMar")}</w:tcPr>{p_xml(pl, self.link_xml)}</w:tc>'
                       f'<w:tc><w:tcPr><w:tcW w:w="{val_w}" w:type="dxa"/>{cell_mar(int(pad), 0, int(pad), 0, "tcMar")}</w:tcPr>{p_xml(pv, self.link_xml)}</w:tc></w:tr>')
        h = len(rows) * (size / 2 * 1.2 + 2 * pad / 20) * 20
        x = (_mm(r.get("left")) or 20) * TW_MM
        if r.get("top"):
            y = _mm(r.get("top")) * TW_MM
        else:
            y = PAGE_H - (_mm(r.get("bottom")) or 25) * TW_MM - h
        return self.floating(lambda tblpr, _w: f'<w:tbl>{tblpr}<w:tblGrid><w:gridCol w:w="{int(lab_w)}"/><w:gridCol w:w="{val_w}"/></w:tblGrid>{"".join(trs)}</w:tbl>'
                             + '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="120" w:lineRule="auto"/><w:rPr><w:sz w:val="4"/></w:rPr></w:pPr></w:p>',
                             x, y, lab_w + val_w, h)

    def cover_block(self, el, r, cover_rules, fill):
        """Any other text element of the theme's cover (organisation, website, address, prepared for)."""
        f = self.props_fmt(r, 20)
        size = f.get("size") or 18
        jc = _jc(r.get("text-align")) or ("right" if r.get("right") and not r.get("left") else "left")
        ps, cur = [], None
        name = "." + sorted(el.cls)[0] if el.cls else ""

        def sub_fmt(k):
            out = dict(f)
            for sel in [name + " " + k.tag] + [name + " ." + x for x in k.cls]:
                if sel.strip() in cover_rules:
                    out.update(self.props_fmt(cover_rules[sel.strip()], size))
            return out

        def walk(node, fmt):
            nonlocal cur
            for k in node.kids:
                if isinstance(k, str):
                    t = fill(k)
                    if t.strip() or (cur is not None and t):
                        if cur is None:
                            cur = P(None, before=0, after=0, line=264, jc=jc)
                        cur.text(t, fmt)
                    continue
                kf = sub_fmt(k)
                props = {}
                for sel in [name + " " + k.tag] + [name + " ." + x for x in k.cls]:
                    props.update(cover_rules.get(sel.strip(), {}))
                block = k.tag in ("div", "p") or props.get("display") == "block"
                if k.tag == "br":
                    if cur is not None:
                        ps.append(cur)
                    cur = None
                    continue
                if block and cur is not None and cur.has_content():
                    ps.append(cur)
                    cur = None
                walk(k, dict(fmt, **{a: b for a, b in kf.items() if a not in f or kf[a] != f[a]}))
                if block:
                    if cur is not None:
                        ps.append(cur)
                    cur = None
        walk(el, dict(f))
        if cur is not None:
            ps.append(cur)
        ps = [p for p in ps if p.has_content()]
        if not ps:
            return ""
        h = 0
        for p in ps:
            sz = max([f_.get("size") or size for k_, v_, f_ in p.items] or [size])
            chars = len(p.plain())
            h += max(1, math.ceil(chars * sz / 2 * .5 * 20 / 9000)) * sz / 2 * 1.5 * 20
        h += 120
        L, R, T, B = (_mm(r.get(k)) for k in ("left", "right", "top", "bottom"))
        W = _mm(r.get("width"))
        if L is not None and R is not None:
            w = (210 - L - R) * TW_MM
            x = L * TW_MM
        elif W is not None:
            w = W * TW_MM
            x = L * TW_MM if L is not None else PAGE_W - (R or 20) * TW_MM - w
        else:
            w = 4800
            x = L * TW_MM if L is not None else PAGE_W - (R or 20) * TW_MM - w
        y = T * TW_MM if T is not None else PAGE_H - (B or 20) * TW_MM - h
        body = "".join(p_xml(p, self.link_xml) for p in ps)
        return self.floating(lambda tblpr, w: f'<w:tbl>{tblpr}<w:tblGrid><w:gridCol w:w="{w}"/></w:tblGrid><w:tr><w:trPr><w:cantSplit/></w:trPr><w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>'
        f'</w:tcPr>{body}</w:tc></w:tr></w:tbl>'
                             + '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="120" w:lineRule="auto"/><w:rPr><w:sz w:val="4"/></w:rPr></w:pPr></w:p>',
                             x, y, w, h)

    # ------------------------------------------------------------------ contents
    def toc_xml(self, pages):
        title = self.doc.meta.get("toc_title", "Contents")
        out = [f'<w:p><w:pPr><w:pStyle w:val="TOCHeading"/><w:pageBreakBefore/></w:pPr>{run(title)}</w:p>']
        n = len(self.headings)
        for i, (lvl, bm, num, text) in enumerate(self.headings):
            body = ""
            if i == 0:
                body += ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u </w:instrText></w:r>'
                         '<w:r><w:fldChar w:fldCharType="separate"/></w:r>')
            pg = str(pages.get(bm, ""))
            inner = (run(num) + "<w:r><w:tab/></w:r>" if num else "") + run(text) + "<w:r><w:tab/></w:r>" + fld_runs(f"PAGEREF {bm} \\h", pg)
            body += f'<w:hyperlink w:anchor="{xt(bm)}" w:history="1">{inner}</w:hyperlink>'
            if i == n - 1:
                body += '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            out.append(f'<w:p><w:pPr><w:pStyle w:val="TOC{lvl}"/></w:pPr>{body}</w:p>')
        if not n:
            return []
        return out

    # ------------------------------------------------------------------ the whole document
    def body(self):
        doc = self.doc
        root, self.svgs = parse_html(doc.content())
        self.anchors = {}
        self.num_color = {}
        for tag in ("h1", "h2", "h3"):
            c = self.color(self.rule(f"{tag} .n").get("color", ""))
            if c:
                self.num_color[tag] = c
        used = set()

        def scan(el):
            for k in el.els():
                if k.tag in ("h1", "h2", "h3") and k.attrs.get("id"):
                    base = "_Toc_" + re.sub(r"[^A-Za-z0-9_]", "_", k.attrs["id"])[:30]
                    name, i = base, 1
                    while name in used:
                        i += 1
                        name = f"{base[:30]}_{i}"
                    used.add(name)
                    self.anchors[k.attrs["id"]] = name
                scan(k)
        scan(root)
        out = self.cover()
        ctx = Ctx(TEXT_W, jc="both" if self.justify else None)
        kids = root.kids
        split = None
        if doc.meta.get("toc", True):
            split = 0
            if doc.meta.get("toc_after_first"):
                for i, k in enumerate(kids):
                    if isinstance(k, El) and k.tag == "section":
                        split = i + 1
                        break
        for i, k in enumerate(kids):
            if i == split:
                out.append("<!--TOC-->")
                self.pending_break = True
            if isinstance(k, str):
                if k.strip():
                    out += self.flow(_wrap(k), ctx)
            elif k.tag not in SKIP:
                out += self.block(k, ctx) if (k.tag in BLOCK or k.has_block()) else self.flow(_wrap(k), ctx)
        if split is not None and split >= len(kids):
            out.append("<!--TOC-->")
        # no two tables directly after each other (Word would join them)
        fixed = []
        for b in out:
            if fixed and b.startswith("<w:tbl>") and fixed[-1].startswith("<w:tbl>") and "w:tblpPr" not in b:
                fixed.append(self.spacer(ctx))
            fixed.append(b)
        if fixed and fixed[-1].startswith("<w:tbl>"):
            fixed.append(self.spacer(ctx))
        return fixed

    def styles_xml(self):
        c, font = self.c, self.font
        lang = {"en": "en-GB", "nl": "nl-NL", "de": "de-DE", "fr": "fr-FR"}.get((self.doc.meta.get("lang") or "en").split("-")[0], "en-GB")
        rule = self.rule

        def hsize(tag, default, minimum):
            pt = _len(rule(tag).get("font-size"), 10)
            return round(max(pt or default, minimum) * 2)
        h1 = hsize("h1", 14, 13)
        h2 = hsize("h2", 11.5, 11)
        h3 = hsize("h3", 10.5, 10)

        def track(name, size):
            v = (self.var(f"var(--{name})") or "").strip()
            pt = _len(v, size / 2) if v else None
            return f'<w:spacing w:val="{round(pt * 20)}"/>' if pt else ""
        h1b = rule("h1").get("border-bottom", "")
        h1_border = ""
        if h1b:
            m = re.search(r"([\d.]+)(px|pt)", h1b)
            sz = round(float(m.group(1)) * (.75 if m.group(2) == "px" else 1) * 8) if m else 12
            col = self.color(re.sub(r"[\d.]+(px|pt)|solid|dashed|dotted", "", h1b).strip()) or c["accent"]
            h1_border = f'<w:pBdr><w:bottom w:val="single" w:sz="{max(4, sz)}" w:space="3" w:color="{col}"/></w:pBdr>'
        jc = '<w:jc w:val="both"/>' if self.justify else ""
        f = xt(font)
        mono = xt(self.mono)
        cover_title = self._cover_style("title", {"b": True, "size": 56, "color": c["ink"]})
        cover_sub = self._cover_style("subtitle", {"size": 26, "color": c["muted"]})
        cover_kick = self._cover_style("kicker", {"b": True, "size": 20, "color": c["accent"], "caps": True})

        def style(sid, name, body, typ="paragraph", based="Normal", nxt=None, extra=""):
            b = f'<w:basedOn w:val="{based}"/>' if based else ""
            n = f'<w:next w:val="{nxt}"/>' if nxt else ""
            return f'<w:style w:type="{typ}" w:styleId="{sid}"><w:name w:val="{name}"/>{b}{n}{extra}{body}</w:style>'
        tabs1 = '<w:tabs><w:tab w:val="left" w:pos="%d"/></w:tabs>'
        toc_tabs = '<w:tabs><w:tab w:val="left" w:pos="%d"/><w:tab w:val="right" w:leader="dot" w:pos="%d"/></w:tabs>'
        q = "<w:qFormat/>"
        s = [
            f'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{f}" w:eastAsia="{f}" w:hAnsi="{f}" w:cs="{f}"/>'
            f'<w:color w:val="{c["ink"]}"/><w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="{lang}" w:eastAsia="en-US" w:bidi="ar-SA"/></w:rPr></w:rPrDefault>'
            '<w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>',
            f'<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>{q}<w:pPr><w:widowControl/>{jc}</w:pPr></w:style>',
            '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/><w:unhideWhenUsed/></w:style>',
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/>'
            '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar>'
            '</w:tblPr></w:style>',
            '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/><w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/></w:style>',
            style("Title", "Title", f'<w:pPr><w:spacing w:before="0" w:after="120" w:line="240" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr>{rpr(cover_title)}',
                  nxt="Normal", extra='<w:uiPriority w:val="10"/>' + q),
            style("Subtitle", "Subtitle", f'<w:pPr><w:spacing w:before="0" w:after="160" w:line="264" w:lineRule="auto"/><w:jc w:val="left"/>'
                  f'</w:pPr>{rpr(cover_sub)}', nxt="Normal", extra='<w:uiPriority w:val="11"/>' + q),
            style("CoverKicker", "Cover Kicker", f'<w:pPr><w:spacing w:before="0" w:after="120"/><w:jc w:val="left"/></w:pPr>{rpr(cover_kick)}', nxt="Title", extra=q),
            style("Heading1", "heading 1", f'<w:pPr><w:keepNext/><w:keepLines/>{h1_border}{tabs1 % round(h1 / 2 * 1.9 * 20)}<w:spacing w:before="360" w:after="160" w:line="240" w:lineRule="auto"/>'
                  f'<w:jc w:val="left"/><w:outlineLvl w:val="0"/></w:pPr>'
                  f'<w:rPr><w:b/><w:bCs/><w:color w:val="{c["h1"]}"/>{track("track-h1", h1)}<w:sz w:val="{h1}"/><w:szCs w:val="{h1}"/></w:rPr>', nxt="Normal", extra='<w:uiPriority w:val="9"/>' + q),
            style("Heading2", "heading 2", f'<w:pPr><w:keepNext/><w:keepLines/>{tabs1 % round(h2 / 2 * 2.6 * 20)}<w:spacing w:before="280" w:after="100" w:line="240" w:lineRule="auto"/><w:jc w:val="left"/>'
                  f'<w:outlineLvl w:val="1"/></w:pPr>'
                  f'<w:rPr><w:b/><w:bCs/><w:color w:val="{c["h2"]}"/>{track("track-h2", h2)}<w:sz w:val="{h2}"/><w:szCs w:val="{h2}"/></w:rPr>', nxt="Normal", extra='<w:uiPriority w:val="9"/><w:unhideWhenUsed/>' + q),
            style("Heading3", "heading 3", f'<w:pPr><w:keepNext/><w:keepLines/>{tabs1 % round(h3 / 2 * 3.2 * 20)}<w:spacing w:before="220" w:after="80" w:line="240" w:lineRule="auto"/><w:jc w:val="left"/>'
                  f'<w:outlineLvl w:val="2"/></w:pPr>'
                  f'<w:rPr><w:b/><w:bCs/><w:color w:val="{self.color(rule("h3").get("color", "")) or c["strong"]}"/><w:sz w:val="{h3}"/><w:szCs w:val="{h3}"/>'
                  f'</w:rPr>', nxt="Normal", extra='<w:uiPriority w:val="9"/><w:unhideWhenUsed/>' + q),
            style("Heading4", "heading 4", '<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="160" w:after="60"/><w:jc w:val="left"/><w:outlineLvl w:val="3"/></w:pPr>'
                  '<w:rPr><w:b/><w:bCs/></w:rPr>', nxt="Normal", extra='<w:uiPriority w:val="9"/><w:unhideWhenUsed/>' + q),
            style("Caption", "caption", f'<w:pPr><w:spacing w:before="60" w:after="220" w:line="252" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr>'
                  f'<w:rPr><w:i/><w:iCs/><w:color w:val="{c["grey"]}"/><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr>', nxt="Normal", extra='<w:uiPriority w:val="35"/><w:unhideWhenUsed/>' + q),
            style("ListParagraph", "List Paragraph", '<w:pPr><w:spacing w:after="140"/><w:ind w:left="720"/><w:contextualSpacing/></w:pPr>', extra='<w:uiPriority w:val="34"/>' + q),
            style("TableText", "Table Text", '<w:pPr><w:spacing w:before="0" w:after="0" w:line="252" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr><w:rPr><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>', extra=q),
            style("SmallText", "Small Text", f'<w:pPr><w:spacing w:after="120" w:line="252" w:lineRule="auto"/></w:pPr><w:rPr><w:color w:val="{c["grey"]}"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr>', extra=q),
            style("Quote", "Quote", f'<w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="{c["accent"]}"/></w:pBdr><w:spacing w:before="120" w:after="160"/><w:ind w:left="340" w:right="340"/>'
                  f'<w:jc w:val="left"/></w:pPr>'
                  f'<w:rPr><w:i/><w:iCs/></w:rPr>', nxt="Normal", extra='<w:uiPriority w:val="29"/>' + q),
            style("CodeBlock", "Code Block", f'<w:pPr><w:shd w:val="clear" w:color="auto" w:fill="{c["light"]}"/><w:spacing w:before="60" w:after="160" w:line="240" w:lineRule="auto"/>'
                  f'<w:ind w:left="113" w:right="113"/><w:jc w:val="left"/></w:pPr>'
                  f'<w:rPr><w:rFonts w:ascii="{mono}" w:hAnsi="{mono}" w:cs="{mono}"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr>', extra=q),
            style("TOCHeading", "TOC Heading", f'<w:pPr><w:spacing w:before="0" w:after="240"/><w:jc w:val="left"/></w:pPr><w:rPr><w:b/><w:bCs/>'
                  f'<w:color w:val="{c["h1"]}"/>{track("track-h1", h1)}<w:sz w:val="{h1}"/><w:szCs w:val="{h1}"/></w:rPr>',
                  nxt="Normal", extra='<w:uiPriority w:val="39"/><w:unhideWhenUsed/>' + q),
            style("TOC1", "toc 1", f'<w:pPr>{toc_tabs % (680, TEXT_W)}<w:spacing w:before="120" w:after="40" w:line="240" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr><w:rPr><w:b/><w:bCs/></w:rPr>',
                  nxt="Normal", extra='<w:uiPriority w:val="39"/><w:unhideWhenUsed/>'),
            style("TOC2", "toc 2", f'<w:pPr>{toc_tabs % (1360, TEXT_W)}<w:spacing w:before="0" w:after="30" w:line="240" w:lineRule="auto"/><w:ind w:left="680"/><w:jc w:val="left"/></w:pPr><w:rPr><w:i/><w:iCs/></w:rPr>',
                  nxt="Normal", extra='<w:uiPriority w:val="39"/><w:unhideWhenUsed/>'),
            style("TOC3", "toc 3", f'<w:pPr>{toc_tabs % (2160, TEXT_W)}<w:spacing w:before="0" w:after="20" w:line="240" w:lineRule="auto"/><w:ind w:left="1360"/><w:jc w:val="left"/></w:pPr><w:rPr>'
                  f'<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>',
                  nxt="Normal", extra='<w:uiPriority w:val="39"/><w:unhideWhenUsed/>'),
            style("Header", "header", f'<w:pPr><w:tabs><w:tab w:val="center" w:pos="{TEXT_W // 2}"/><w:tab w:val="right" w:pos="{TEXT_W}"/></w:tabs><w:spacing w:after="0" w:line="240" w:lineRule="auto"/>'
                  f'<w:jc w:val="left"/></w:pPr>'
                  f'<w:rPr><w:color w:val="{c["grey"]}"/><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr>', extra='<w:uiPriority w:val="99"/><w:unhideWhenUsed/>'),
            style("Footer", "footer", f'<w:pPr><w:tabs><w:tab w:val="center" w:pos="{TEXT_W // 2}"/><w:tab w:val="right" w:pos="{TEXT_W}"/></w:tabs><w:spacing w:after="0" w:line="240" w:lineRule="auto"/>'
                  f'<w:jc w:val="left"/></w:pPr>'
                  '<w:rPr><w:color w:val="808080"/><w:sz w:val="15"/><w:szCs w:val="15"/></w:rPr>', extra='<w:uiPriority w:val="99"/><w:unhideWhenUsed/>'),
            style("Hyperlink", "Hyperlink", f'<w:rPr><w:color w:val="{c["accent"]}"/><w:u w:val="single"/></w:rPr>', typ="character", based="DefaultParagraphFont", extra='<w:uiPriority w:val="99"/><w:unhideWhenUsed/>'),
            style("InlineCode", "Inline Code", f'<w:rPr><w:rFonts w:ascii="{mono}" w:hAnsi="{mono}" w:cs="{mono}"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>', typ="character", based="DefaultParagraphFont", extra=q),
            style("PaiperTable", "Report Table", '<w:pPr><w:spacing w:before="0" w:after="0" w:line="252" w:lineRule="auto"/></w:pPr><w:rPr><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
                  '<w:tblPr>' + tbl_borders({"insideH": ("single", 4, c["rule"]) if self.ruled else ("single", 12, "FFFFFF")}) + cell_mar(60, 113, 60, 113) + '</w:tblPr>'
                  f'<w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="{"auto" if self.ruled else c["light"]}"/></w:tcPr>'
                  f'<w:tblStylePr w:type="firstRow"><w:rPr><w:b/><w:bCs/><w:color w:val="{c["th-ink"]}"/></w:rPr><w:tcPr>'
                  f'<w:shd w:val="clear" w:color="auto" w:fill="{"auto" if self.ruled else c["th-bg"]}"/><w:vAlign w:val="bottom"/>'
                  f'</w:tcPr></w:tblStylePr>',
                  typ="table", based="TableNormal", extra='<w:uiPriority w:val="59"/>'),
        ]
        return (DECL + f'<w:styles {NS}>' + "".join(s) + "</w:styles>")

    def _cover_style(self, role, default):
        croot, _ = parse_html(self.doc.theme.cover or "")
        r = {}
        for k in croot.els():
            if "{{" + role + "}}" in k.text():
                for sel, props in self.rules:
                    if any(sel == ".cover ." + x for x in k.cls):
                        r.update(props)
        if r:                                           # the theme styles it: plain text plus what its CSS says
            f = {"size": default.get("size", 20), "color": self.c["ink"], "b": False}
        else:
            f = dict(default)
        f.update(self.props_fmt(r, default.get("size", 20)))
        return {k: v for k, v in f.items() if v is not None}

    def numbering_xml(self):
        bullets = ["•", "–", "•"]
        lv = []
        for i in range(9):
            left = LIST_LEFT + LIST_STEP * i
            lv.append(f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="{bullets[i % 3]}"/><w:lvlJc w:val="left"/>'
                      f'<w:pPr><w:ind w:left="{left}" w:hanging="{LIST_HANG}"/></w:pPr></w:lvl>')
        a0 = '<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>' + "".join(lv) + "</w:abstractNum>"
        fmts = ["decimal", "lowerLetter", "lowerRoman"]
        lv = []
        for i in range(9):
            left = LIST_LEFT + LIST_STEP * i
            lv.append(f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmts[i % 3]}"/><w:lvlText w:val="%{i + 1}."/><w:lvlJc w:val="left"/>'
                      f'<w:pPr><w:ind w:left="{left}" w:hanging="{LIST_HANG}"/></w:pPr></w:lvl>')
        a1 = '<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>' + "".join(lv) + "</w:abstractNum>"
        nums = "".join(f'<w:num w:numId="{nid}"><w:abstractNumId w:val="{ab}"/><w:lvlOverride w:ilvl="{il}"><w:startOverride w:val="{st}"/></w:lvlOverride></w:num>'
                       for nid, ab, il, st in self.nums)
        return DECL + f"<w:numbering {NS}>{a0}{a1}{nums}</w:numbering>"

    def fonts(self):
        """fontTable.xml, its relationships and the embedded font parts."""
        embeds, rels, parts = [], Rels(), {}
        for rel in self.dx.get("embed") or []:
            path = os.path.join(self.tdir, rel)
            try:
                with open(path, "rb") as f:
                    data = f.read()
                fam, bold, ital = font_info(data)
            except (OSError, struct.error):
                self.notes.append(f"font not embedded: {rel}")
                continue
            if data[:4] not in (b"\x00\x01\x00\x00", b"true"):
                self.notes.append(f"font not embedded (not TrueType): {rel}")
                continue
            kind = "embedBoldItalic" if bold and ital else "embedBold" if bold else "embedItalic" if ital else "embedRegular"
            key = "{" + str(uuid.uuid4()).upper() + "}"
            name = f"fonts/font{len(parts) + 1}.odttf"
            parts["word/" + name] = obfuscate(data, key)
            rid = rels.add("font", name)
            embeds.append((kind, rid, key))
        order = ["embedRegular", "embedBold", "embedItalic", "embedBoldItalic"]
        embeds.sort(key=lambda e: order.index(e[0]))
        seen, emb = set(), ""
        for kind, rid, key in embeds:
            if kind not in seen:
                seen.add(kind)
                emb += f'<w:{kind} r:id="{rid}" w:fontKey="{key}"/>'
        fonts = [(self.font, emb), (self.mono, "")]
        panose = {"Century Gothic": "020B0502020202020204", "Calibri": "020F0502020204030204", "Consolas": "020B0609020204030204"}
        xml = (DECL + f"<w:fonts {NS}>" + "".join(
            f'<w:font w:name="{xt(n)}">{"<w:panose1 w:val=" + chr(34) + panose[n] + chr(34) + "/>" if n in panose else ""}<w:charset w:val="00"/><w:family w:val="{"modern" if n == self.mono else "swiss"}"/>'
            f'<w:pitch w:val="{"fixed" if n == self.mono else "variable"}"/>{e}</w:font>' for n, e in fonts) + "</w:fonts>")
        return xml, (rels if rels.items else None), parts

    def header_footer(self):
        """(header default, footer default, first-page header, first-page footer) part XML + rels."""
        doc = self.doc
        hrels = Rels()
        logo = self.logo_image()
        if logo:
            name, w, h = logo
            hpt = 25.0
            m = re.search(r"running-logo-img[^{]*\{[^}]*height:\s*([\d.]+)pt", _read(os.path.join(self.tdir, "theme.css")))
            if m:
                hpt = float(m.group(1))
            cy = hpt * 20
            hp = f'<w:p><w:pPr><w:pStyle w:val="Header"/></w:pPr>{self.drawing(name, cy * w / max(h, 1), cy, "logo", rels=hrels)}</w:p>'
        else:
            hp = f'<w:p><w:pPr><w:pStyle w:val="Header"/></w:pPr>{run(doc.title)}</w:p>'
        foot = doc.theme.meta.get("footer", {"left": "{org}", "center": "{classification}", "right": "page {page}"})

        def part(txt):
            out = ""
            for piece in re.split(r"(\{pages?\})", txt or ""):
                if piece == "{page}":
                    out += f'<w:fldSimple w:instr=" PAGE ">{run("1")}</w:fldSimple>'
                elif piece == "{pages}":
                    out += f'<w:fldSimple w:instr=" NUMPAGES ">{run("1")}</w:fldSimple>'
                elif piece:
                    out += run(doc.fill(piece))
            return out
        fp = (f'<w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr>{part(foot.get("left", ""))}<w:r><w:tab/></w:r>'
              f'{part(foot.get("center", ""))}<w:r><w:tab/></w:r>{part(foot.get("right", ""))}</w:p>')
        empty = '<w:p><w:pPr><w:pStyle w:val="Header"/></w:pPr></w:p>'
        return ((DECL + f"<w:hdr {NS}>{hp}</w:hdr>", hrels), (DECL + f"<w:ftr {NS}>{fp}</w:ftr>", None),
                (DECL + f"<w:hdr {NS}>{empty}</w:hdr>", None), (DECL + f"<w:ftr {NS}>{empty.replace('Header', 'Footer')}</w:ftr>", None))

    def build(self):
        """All parts of the package: {name: bytes}."""
        doc = self.doc
        body = self.body()
        hf = self.header_footer()
        names = ["header1.xml", "footer1.xml", "header2.xml", "footer2.xml"]
        rids = []
        for n, (xml, rels) in zip(names, hf):
            self.parts["word/" + n] = xml.encode("utf-8")
            if rels and rels.items:
                self.parts[f"word/_rels/{n}.rels"] = rels.xml().encode("utf-8")
            rids.append(self.rels.add("header" if n.startswith("header") else "footer", n))
        sect = (f'<w:sectPr><w:headerReference w:type="default" r:id="{rids[0]}"/><w:headerReference w:type="first" r:id="{rids[2]}"/>'
                f'<w:footerReference w:type="default" r:id="{rids[1]}"/><w:footerReference w:type="first" r:id="{rids[3]}"/>'
                f'<w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/><w:pgMar w:top="{self.top}" w:right="{MARGIN}" w:bottom="{MARGIN}" w:left="{MARGIN}" '
                f'w:header="539" w:footer="454" w:gutter="0"/><w:cols w:space="708"/><w:titlePg/><w:docGrid w:linePitch="360"/></w:sectPr>')
        self.body_blocks, self.sect = body, sect
        for rel, typ in (("styles.xml", "styles"), ("settings.xml", "settings"), ("fontTable.xml", "fontTable"), ("numbering.xml", "numbering")):
            self.rels.add(typ, rel)
        ft, ft_rels, font_parts = self.fonts()
        self.parts["word/fontTable.xml"] = ft.encode("utf-8")
        if ft_rels:
            self.parts["word/_rels/fontTable.xml.rels"] = ft_rels.xml().encode("utf-8")
        self.parts.update(font_parts)
        self.parts["word/styles.xml"] = self.styles_xml().encode("utf-8")
        self.parts["word/numbering.xml"] = self.numbering_xml().encode("utf-8")
        emb = "<w:embedTrueTypeFonts/>" if font_parts else ""
        self.parts["word/settings.xml"] = (DECL + f'<w:settings {NS}><w:zoom w:percent="100"/>{emb}<w:defaultTabStop w:val="709"/>'
                                           '<w:characterSpacingControl w:val="doNotCompress"/><w:compat><w:compatSetting w:name="compatibilityMode" '
                                           'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
                                           '<w:decimalSymbol w:val="."/><w:listSeparator w:val=","/></w:settings>').encode("utf-8")
        for name, data in self.media_parts.items():
            self.parts["word/" + name] = data
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        author = doc.cfg.get("author") or doc.meta_value("Prepared by") or ""
        self.parts["docProps/core.xml"] = (DECL + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                                           'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                                           'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                                           f'<dc:title>{xt(doc.meta.get("cover", {}).get("title") or doc.title)}</dc:title>'
                                           + (f"<dc:subject>{xt(doc.meta.get('cover', {}).get('subtitle', ''))}</dc:subject>" if doc.meta.get("cover", {}).get("subtitle") else "")
                                           + f"<dc:creator>{xt(author)}</dc:creator><cp:lastModifiedBy>{xt(author)}</cp:lastModifiedBy>"
                                           + (f"<cp:version>{xt(doc.meta_value('Version'))}</cp:version>" if doc.meta_value("Version") else "")
                                           + f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
                                           f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>').encode("utf-8")
        self.parts["docProps/app.xml"] = (DECL + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
                                          'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                                          f'<Application>paiper</Application><Company>{xt(doc.field("org"))}</Company></Properties>').encode("utf-8")
        self.parts["_rels/.rels"] = (DECL + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                     f'<Relationship Id="rId1" Type="{RT}officeDocument" Target="word/document.xml"/>'
                                     '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                                     f'<Relationship Id="rId3" Type="{RT}extended-properties" Target="docProps/app.xml"/></Relationships>').encode("utf-8")
        self.set_document(estimate_pages(self.body_blocks, self.headings, self.top))

    def set_document(self, pages):
        toc = self.toc_xml(pages)
        blocks = []
        for b in self.body_blocks:
            if b == "<!--TOC-->":
                blocks += toc
            else:
                blocks.append(b)
        self.parts["word/document.xml"] = (DECL + f"<w:document {NS}><w:body>" + "".join(blocks) + self.sect + "</w:body></w:document>").encode("utf-8")
        self.parts["word/_rels/document.xml.rels"] = self.rels.xml().encode("utf-8")

    def package(self):
        exts = {n.rsplit(".", 1)[-1].lower() for n in self.parts if "." in n.rsplit("/", 1)[-1]}
        mime = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "gif": "image/gif", "rels": "application/vnd.openxmlformats-package.relationships+xml",
                "xml": "application/xml", "odttf": "application/vnd.openxmlformats-officedocument.obfuscatedFont"}
        over = {"word/document.xml": CT_MAIN + "document.main+xml", "word/styles.xml": CT_MAIN + "styles+xml",
                "word/settings.xml": CT_MAIN + "settings+xml", "word/fontTable.xml": CT_MAIN + "fontTable+xml",
                "word/numbering.xml": CT_MAIN + "numbering+xml", "docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
                "docProps/app.xml": "application/vnd.openxmlformats-officedocument.extended-properties+xml"}
        for n in self.parts:
            if re.match(r"word/header\d+\.xml$", n):
                over[n] = CT_MAIN + "header+xml"
            elif re.match(r"word/footer\d+\.xml$", n):
                over[n] = CT_MAIN + "footer+xml"
        ct = (DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              + "".join(f'<Default Extension="{e}" ContentType="{mime[e]}"/>' for e in sorted(exts) if e in mime)
              + "".join(f'<Override PartName="/{n}" ContentType="{t}"/>' for n, t in sorted(over.items()) if n in self.parts) + "</Types>")
        return [("[Content_Types].xml", ct.encode("utf-8"))] + sorted(self.parts.items(), key=lambda kv: (not kv[0].startswith("_rels"), kv[0]))

    def write(self, out):
        tmp = out + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in self.package():
                z.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), data, zipfile.ZIP_STORED if name.endswith((".jpeg", ".png", ".gif")) else zipfile.ZIP_DEFLATED)
        os.replace(tmp, out)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


LIST_LEFT, LIST_STEP, LIST_HANG = 400, 360, 280


def estimate_pages(blocks, headings, top):
    """Rough page of each heading (for the pre-filled contents), from text length, table rows and picture heights."""
    usable = PAGE_H - top - MARGIN
    page, y = 1, 0.0
    toc_lines = len(headings)
    pages = {}
    first = True
    for b in blocks:
        if b == "<!--TOC-->":
            page += 1 if y > 0 else 0
            y = 0.0
            toc_h = 800 + toc_lines * 300
            page += int(toc_h // usable)
            y = toc_h % usable
            continue
        if first:                                       # the cover is one page
            if "pageBreakBefore" not in b:
                continue
        brk = "<w:pageBreakBefore/>" in b
        if brk and not first:
            page += 1
            y = 0.0
        elif brk and first:
            page, y = 2, 0.0
        first = False if brk or not first else first
        if b.startswith("<w:tbl>"):
            h = 0.0
            for tr in re.findall(r"<w:tr>.*?</w:tr>", b, re.S):
                rh = 300.0
                for tc in re.findall(r"<w:tc>.*?</w:tc>", tr, re.S):
                    wm = re.search(r'<w:tcW w:w="(\d+)"', tc)
                    w = int(wm.group(1)) if wm else 3000
                    chars = len("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", tc)))
                    lines = max(1, math.ceil(chars * 92 / max(400, w - 226)))
                    ph = sum(int(c) / EMU_TW for c in re.findall(r'<wp:extent cx="\d+" cy="(\d+)"', tc))
                    rh = max(rh, lines * 250 + 140 + ph + (len(re.findall(r"<w:p[ >]", tc)) - 1) * 60)
                h += rh
        else:
            chars = len("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", b)))
            size = 20
            m = re.search(r'<w:pStyle w:val="(Heading\d|Caption|TableText|SmallText)"', b)
            if m:
                size = {"Heading1": 28, "Heading2": 23, "Heading3": 21}.get(m.group(1), 17)
            lines = max(1, math.ceil(chars * size * 2.6 / TEXT_W)) if chars else 0
            h = lines * size * 13.8 + (140 if chars else 0)
            h += sum(int(c) / EMU_TW for c in re.findall(r'<wp:inline[^>]*><wp:extent cx="\d+" cy="(\d+)"', b)) + (200 if "<wp:inline" in b else 0)
            if m and m.group(1).startswith("Heading"):
                h += 300
        if y + h > usable and y > 0:
            page += 1
            y = h if h < usable else h % usable
        else:
            y += h
        for name in re.findall(r'<w:bookmarkStart w:id="\d+" w:name="([^"]+)"', b):
            if any(name == hh[1] for hh in headings):
                pages[name] = page
    return pages


def _fit_box(w, h, bw, bh, allow_crop=True):
    """Size a w x h picture into a bw x bh box (twips): fill it with a small crop, else fit inside. -> (cx, cy, crop)."""
    a, b = w / max(h, 1), bw / max(bh, 1)
    if allow_crop:
        if a > b:
            keep = b / a
            if keep >= .72:
                c = (1 - keep) / 2 * 100000
                return bw, bh, {"l": c, "r": c}
        else:
            keep = a / b
            if keep >= .72:
                c = (1 - keep) / 2 * 100000
                return bw, bh, {"t": c, "b": c}
    if a > b:
        return bw, bw / a, None
    return bh * a, bh, None


def _xml_ok(s):
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(s.encode("utf-8"))
        return True
    except ET.ParseError:
        return False


CAMEL = ["viewBox", "preserveAspectRatio", "textLength", "lengthAdjust", "gradientUnits", "gradientTransform", "patternUnits",
         "patternContentUnits", "patternTransform", "clipPathUnits", "maskUnits", "maskContentUnits", "markerWidth", "markerHeight",
         "markerUnits", "refX", "refY", "startOffset", "stdDeviation", "baseFrequency", "numOctaves", "spreadMethod", "attributeName",
         "repeatCount", "linearGradient", "radialGradient", "clipPath", "textPath", "feGaussianBlur", "foreignObject", "feOffset",
         "feBlend", "feColorMatrix", "feFlood", "feMerge", "feMergeNode", "feComposite", "tableValues"]


def _repair_svg(s):
    """Re-serialise HTML-flavoured SVG (unquoted attributes, unclosed tags) as XML."""
    t = _Tree()
    t.feed(s)
    t.close()
    fix = {c.lower(): c for c in CAMEL}

    def ser(el):
        if isinstance(el, str):
            return escape(el)
        tag = fix.get(el.tag, el.tag)
        attrs = "".join(f' {fix.get(k, k)}="{xt(v)}"' for k, v in el.attrs.items() if re.match(r"^[\w:.-]+$", k))
        inner = "".join(ser(k) for k in el.kids)
        return f"<{tag}{attrs}>{inner}</{tag}>"
    svg = next((k for k in t.root.els() if k.tag == "svg"), None)
    return ser(svg) if svg is not None else s


def _find_all(el, cls):
    out = []
    for k in el.els():
        if cls in k.cls:
            out.append(k)
        else:
            out += _find_all(k, cls)
    return out


def _wrap(*kids):
    e = El("div")
    for k in kids:
        e.kids.append(k)
    return e


def _jc(v):
    return {"left": "left", "right": "right", "center": "center", "justify": "both"}.get((v or "").strip().lower())


def _mm(v):
    pt = _len(v, 10) if v else None
    return pt / 72 * 25.4 if pt is not None else None


def _int(v, d):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return d


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------------------------- page numbers via LibreOffice
def lo_pages(path, names):
    """Page of each bookmark as LibreOffice lays the document out (soffice + pdfinfo), or {} when they are missing."""
    soffice, pdfinfo = _tool("soffice"), _tool("pdfinfo")
    if not soffice and os.path.exists("/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        soffice = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if not soffice or not pdfinfo:
        return {}
    work = tempfile.mkdtemp(prefix="paiper-lo-")
    profile = os.path.join(os.path.expanduser("~"), ".cache", "paiper", "lo-profile")
    try:
        src = os.path.join(work, "doc.docx")
        shutil.copy(path, src)
        flt = 'pdf:writer_pdf_Export:{"ExportBookmarksToPDFDestination":{"type":"boolean","value":"true"}}'
        subprocess.run([soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", flt, "--outdir", work, src],
                       capture_output=True, timeout=300)
        pdf = os.path.join(work, "doc.pdf")
        if not os.path.exists(pdf):
            return {}
        r = subprocess.run([pdfinfo, "-dests", pdf], capture_output=True, text=True, timeout=60)
        enc = {re.sub(r"[^A-Za-z0-9]", lambda m: "%02X" % ord(m.group(0)), n): n for n in names}   # LibreOffice escapes "_" as 5F
        enc.update({n: n for n in names})
        pages = {}
        for line in r.stdout.splitlines():
            m = re.match(r'\s*(\d+)\s+\[[^\]]*\]\s+"([^"]+)"', line)
            if m and m.group(2) in enc and enc[m.group(2)] not in pages:
                pages[enc[m.group(2)]] = int(m.group(1))
        return pages
    except (OSError, subprocess.SubprocessError):
        return {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------------------------- entry point
def export(folder, out=None, cfg=None, paginate=True):
    """Write the document as .docx. Returns (path, notes)."""
    global PAGE_W, PAGE_H, TEXT_W
    doc = Doc(folder, cfg)
    PAGE_W, PAGE_H = (12240, 15840) if doc.page == "letter" else (11906, 16838)          # twips
    TEXT_W = PAGE_W - 2 * MARGIN
    if not out:
        out = os.path.join(doc.dir, os.path.splitext(doc.pdf_name())[0] + ".docx")
    w = Word(doc, paginate)
    try:
        w.build()
        w.write(out)
        names = {h[1] for h in w.headings}
        if paginate and names and doc.meta.get("toc", True):
            pages = lo_pages(out, names)
            if pages:
                w.set_document(pages)
                w.write(out)
    finally:
        w.cleanup()
    return out, sorted(set(w.notes))

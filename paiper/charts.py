"""Optional SVG chart helpers. Nothing in paiper needs them: content is free HTML, so any hand-made
SVG, canvas or JS chart works as well. Charts are drawn in a 720-wide viewBox and scale to the text width.

Colours default to a neutral ink/grey palette; pass your own (e.g. from paiper.palette("<theme>")),
or use CSS variables in style attributes (style="fill:var(--accent)") to follow the document theme.
"""
import html
import statistics  # noqa: F401  (histogram)

INK = "#1b2430"      # main measure
ORG = "#c4572a"      # the one thing that needs attention
MUT = "#666666"      # labels
D2 = "#9a9a9a"       # secondary series
GRID = "#e3e3e3"
LIGHT = "#f2f2f2"
BAR = "#a9bccd"
MID = "#5f7488"
W = 720


def esc(s):
    return html.escape(str(s), quote=True)


def svg(h, body, label):
    return (f'<svg class="chart" viewBox="0 0 {W} {h}" role="img" aria-label="{esc(label)}" '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def T(x, y, s, size=12, fill=INK, anchor="start", weight=400, extra="", halo=False):
    h = ' stroke="#fff" stroke-width="4" paint-order="stroke" stroke-linejoin="round"' if halo else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}"{h} {extra}>{esc(s)}</text>')


def R(x, y, w, h, fill, rx=0, title=None, extra=""):
    t = f"<title>{esc(title)}</title>" if title else ""
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w,0):.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}" {extra}>{t}</rect>'


def L(x1, y1, x2, y2, stroke=GRID, sw=1, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="{sw}"{d}/>'


def legend(items, x, y, gap=18):
    out, cx = [], x
    for color, name, kind in items:
        if kind == "ring":
            out.append(f'<rect x="{cx}" y="{y-9}" width="9" height="9" rx="2" fill="#fff" stroke="{color}" stroke-width="1.6"/>')
        elif kind == "line":
            out.append(L(cx, y - 4, cx + 14, y - 4, color, 2, "4 3"))
            cx += 5
        else:
            out.append(R(cx, y - 9, 9, 9, color, 2))
        out.append(T(cx + 14, y, name, 11, MUT))
        cx += 14 + len(name) * 5.9 + gap
    return "".join(out)


def hbar(rows, label_w=250, unit="", highlight=None, series=None, note=None):
    """rows: (label, [values...]) ; series: [(name,color)] aligned with values."""
    series = series or [("", INK)]
    mx = max(sum(v) for _, v in rows)
    x0, x1 = label_w, W - 50
    def sx(v):
        return x0 + v / mx * (x1 - x0)
    bh, step = 20, 34
    b = []
    for i, (label, vals) in enumerate(rows):
        y = 8 + i * step
        b.append(T(label_w - 12, y + 14, label, 12.5, INK, "end"))
        x = x0
        for (name, color), v in zip(series, vals):
            if v <= 0:
                continue
            w = sx(v) - x0
            b.append(R(x, y, w - (2 if len(series) > 1 else 0), bh, color, 3, f"{label}: {v} {name}".strip()))
            x += w
        b.append(T(x + 8, y + 14.5, f"{sum(vals)}{unit}", 12.5, INK, weight=700))
    h = 8 + len(rows) * step + (22 if len(series) > 1 else 4)
    if len(series) > 1:
        b.append(legend([(c, n, "box") for n, c in series], x0, h - 6))
    return svg(h + 4, "".join(b), "Horizontal bar chart")


def histogram(vals, bins=20, lo=None, hi=None, unit="", xlabel="", color=BAR, marks=None):
    """Distribution of `vals` in `bins` equal bins; `marks`: [(value, label)] drawn as dashed lines (e.g. the median)."""
    vals = list(vals)
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi <= lo:
        hi = lo + 1
    x0, x1, y0, y1 = 40.0, 690.0, 30.0, 200.0
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        i = min(int((v - lo) / step), bins - 1)
        if 0 <= i:
            counts[i] += 1
    mx = max(counts) or 1

    def sx(v):
        return x0 + (v - lo) / (hi - lo) * (x1 - x0)

    def sy(c):
        return y1 - c / mx * (y1 - y0)
    b = [L(x0, y1, x1, y1, MUT, 1)]
    for i, c in enumerate(counts):
        a = lo + i * step
        b.append(R(sx(a) + 1, sy(c), (x1 - x0) / bins - 2, y1 - sy(c), color, 2, f"{a:g} to {a + step:g}{unit}: {c}"))
    for k in range(6):
        t = lo + k * (hi - lo) / 5
        b.append(T(sx(t), y1 + 17, f"{t:g}{unit}", 11, MUT, "middle"))
    for value, label in marks or []:
        b.append(L(sx(value), y0 - 4, sx(value), y1, ORG, 2, "5 3"))
        b.append(T(sx(value) + 7, y0 + 6, label, 12, ORG, weight=700))
    if xlabel:
        b.append(T(x1, y1 + 34, xlabel, 11, MUT, "end"))
    return svg(int(y1 + 42), "".join(b), xlabel or "Histogram")


def pct(sorted_vals, p):
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)

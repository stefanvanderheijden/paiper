"""Just enough CSS parsing: the rules of a stylesheet, for theme variables and the Word export."""
import re


def _blocks(s):
    i, n = 0, len(s)
    while i < n:
        j = s.find("{", i)
        if j < 0:
            return
        depth, k = 1, j + 1
        while k < n and depth:
            depth += {"{": 1, "}": -1}.get(s[k], 0)
            k += 1
        yield s[i:j].strip(), s[j + 1:k - 1]
        i = k


def css_rules(text):
    """[(selector, {prop: value})] for top-level rules and @media print; screen-only rules are left out."""
    text = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    out = []
    for sel, body in _blocks(text):
        if sel.startswith("@"):
            if sel.startswith("@media") and "screen" not in sel:
                out += css_rules(body)
            continue
        props = {}
        for decl in body.split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                props[k.strip().lower()] = v.replace("!important", "").strip()
        for one in sel.split(","):
            out.append((" ".join(one.split()), props))
    return out

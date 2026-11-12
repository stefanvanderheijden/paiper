"""paiper tests (stdlib unittest): rendering, anchors, edits, the review API and its security, MCP."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from paiper import config, doc as D, store  # noqa: E402
from paiper.server import Handler  # noqa: E402

CONTENT = """<section><h1 id="intro" class="pb"><span class="n">1.</span>Intro</h1>
<p>First paragraph with <strong>bold</strong> text.</p>
<p>Second paragraph.</p>
<ul><li>One</li><li>Two <ul><li>Nested</li></ul></li></ul>
<h2 id="intro-1"><span class="n">1.1.</span>Detail</h2>
<figure><svg class="chart" viewBox="0 0 10 10"><rect width="5" height="5" style="fill:var(--accent)"/></svg>
<figcaption><span class="ft">A figure</span></figcaption></figure>
<p data-cid="preset-1">Anchored by a build script.</p>
</section>"""


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "docs")
        self.doc = os.path.join(self.root, "client", "report")
        os.makedirs(self.doc)
        with open(os.path.join(self.doc, "paiper.json"), "w") as f:
            json.dump({"type": "report", "title": "Test report", "theme": "default", "content": "content.html",
                       "cover": {"title": "Test"}, "meta": [["Version", "1.0"]]}, f)
        with open(os.path.join(self.doc, "content.html"), "w") as f:
            f.write(CONTENT)
        with open(os.path.join(self.doc, "photo.txt"), "w") as f:
            f.write("inside")
        with open(os.path.join(self.root, "secret.txt"), "w") as f:
            f.write("outside")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump({"roots": {"docs": self.root}, "author": "Tester", "port": 0}, f)
        self._old = config.CONFIG_PATH
        config.CONFIG_PATH = self.cfg_path

    def tearDown(self):
        config.CONFIG_PATH = self._old
        shutil.rmtree(self.tmp)


class TestDoc(Env):
    def test_anchors_and_editable(self):
        out = D.tag_content(CONTENT)
        self.assertEqual(out.count("data-editable"), 8)          # h1, 2 p + the preset p, 3 li (incl. nested), h2
        self.assertIn('data-cid="preset-1" data-editable>', out)  # build-script anchors are kept
        self.assertEqual(len(set(b["cid"] for b in D.scan_blocks(CONTENT))), len(D.scan_blocks(CONTENT)))

    def test_anchor_stable_when_other_text_changes(self):
        a = {b["tag"] + str(i): b["cid"] for i, b in enumerate(D.scan_blocks(CONTENT))}
        b_ = {b["tag"] + str(i): b["cid"] for i, b in enumerate(D.scan_blocks(CONTENT.replace("Second", "2nd")))}
        changed = [k for k in a if a[k] != b_[k]]
        self.assertEqual(len(changed), 1)

    def test_clean_inline(self):
        self.assertEqual(D.clean_inline('a <span style="x">b</span> <b>c</b><div>d</div><button class="cbtn">+</button>&nbsp;e<br><br>'),
                         "a b <b>c</b>d e")
        self.assertEqual(D.clean_inline('<a href="javascript:x" onclick="y">l</a>'), "<a>l</a>")
        self.assertEqual(D.clean_inline('<a href="https://x.org">l</a>'), '<a href="https://x.org">l</a>')
        self.assertNotIn("onclick", D.clean_inline('<b onclick="y">x</b>'))
        self.assertNotIn("<script", D.clean_inline("<script>alert(1)</script>ok"))

    def test_edit_and_revert(self):
        d = D.Doc(self.doc)
        b = next(x for x in D.scan_blocks(d.content()) if "Second" in d.content()[x["open_end"]:x["inner_end"]])
        new_cid, before, after = d.apply_edit(b["cid"], "Second paragraph.", "Second, <em>edited</em>.")
        self.assertIn("<p>Second, <em>edited</em>.</p>", d.content())
        self.assertNotEqual(new_cid, b["cid"])
        with self.assertRaises(ValueError):                        # stale page: text changed meanwhile
            d.apply_edit(new_cid, "something else", "x")
        self.assertTrue(d.revert_edit({"before": before, "after": after}))
        self.assertEqual(d.content(), CONTENT)

    def test_render_modes(self):
        d = D.Doc(self.doc)
        html = d.render("server")
        self.assertIn('/_paiper/rt/viewer.js', html)
        self.assertIn('href="#intro-1"', html)                     # contents from h1/h2
        self.assertIn("data-cid=", html)
        p = d.render("print")
        self.assertNotIn("<script src", p)
        self.assertIn("counter(page)", p)
        self.assertIn("file://", p)

    def test_print_resolves_svg_vars(self):
        p = D.Doc(self.doc).render("print")
        i = p.index('<svg class="chart"')
        svg = p[i:p.index("</svg>", i)]
        self.assertNotIn("var(", svg)                              # WeasyPrint would draw it black
        self.assertIn("#2563ab", svg.lower())                      # default theme accent

    def test_scripts_and_comments_are_left_alone(self):
        src = '<p>Real</p><script>var s = "<p>not a block</p>";</script><!-- <p>nor this</p> --><style>p > li {}</style>'
        out = D.tag_content(src)
        self.assertEqual(len(D.scan_blocks(src)), 1)
        self.assertIn('var s = "<p>not a block</p>";', out)
        self.assertIn("<!-- <p>nor this</p> -->", out)

    def test_title_cannot_break_out_of_the_page(self):
        pj = os.path.join(self.doc, "paiper.json")
        meta = json.loads(read(pj))
        meta["title"] = 'A/B </script><script>alert(1)</script>'
        with open(pj, "w") as f:
            json.dump(meta, f)
        d = D.Doc(self.doc, config.load())
        self.assertNotIn("</script><script>alert(1)", d.render("server"))
        self.assertNotIn("/", d.pdf_name())

    def test_review_items_survive_writers_in_other_processes(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from paiper import store\n"
                "for i in range(25): store.add(sys.argv[2], text='x')")
        ps = [subprocess.Popen([sys.executable, "-c", code, ROOT, self.doc]) for _ in range(4)]
        for q in ps:
            self.assertEqual(q.wait(), 0)
        self.assertEqual(len(store.load(self.doc)), 100)
        self.assertEqual(sorted(os.listdir(self.doc)), ["comments.json", "content.html", "paiper.json", "photo.txt"])

    def test_page_size(self):
        cfg = config.load()
        self.assertIn("size: A4", D.Doc(self.doc, cfg).render("print"))
        self.assertIn("size: Letter", D.Doc(self.doc, dict(cfg, page="letter")).render("print"))
        pj = os.path.join(self.doc, "paiper.json")
        meta = json.loads(read(pj))
        meta["page"] = "letter"
        with open(pj, "w") as f:
            json.dump(meta, f)
        out = D.Doc(self.doc, cfg).render("server")                # the document's own setting wins
        self.assertIn("--page-w: 215.9mm", out)
        from paiper.docx import export
        path, _ = export(self.doc, os.path.join(self.tmp, "letter.docx"), cfg, paginate=False)
        import zipfile
        with zipfile.ZipFile(path) as z:
            self.assertIn('w:w="12240" w:h="15840"', z.read("word/document.xml").decode())

    def test_palette(self):
        import paiper
        self.assertIn("accent", paiper.palette("default"))


class TestServer(Env):
    def setUp(self):
        super().setUp()
        Handler.cfg = config.load()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.docid = "docs/client/report"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        h = {"Host": f"localhost:{self.port}"}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_pages(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        s, b = self.req("GET", "/d/docs/client/report/")
        self.assertEqual(s, 200)
        self.assertIn(b"Test report", b)
        self.assertEqual(self.req("GET", "/_paiper/rt/viewer.js")[0], 200)
        self.assertEqual(self.req("GET", "/_paiper/themes/default/theme.css")[0], 200)

    def test_files_only_inside_documents(self):
        self.assertEqual(self.req("GET", "/d/docs/client/report/photo.txt")[0], 200)
        self.assertEqual(self.req("GET", "/d/docs/secret.txt")[0], 404)
        self.assertEqual(self.req("GET", "/d/docs/client/report/../../secret.txt")[0], 404)
        self.assertEqual(self.req("GET", "/d/docs/client/report/comments.json")[0], 404)
        self.assertEqual(self.req("GET", "/_paiper/themes/default/../../cli.py")[0], 403)

    def test_hidden_files_not_served(self):
        with open(os.path.join(self.doc, ".env"), "w") as f:
            f.write("TOKEN=1")
        os.makedirs(os.path.join(self.doc, ".git"))
        with open(os.path.join(self.doc, ".git", "config"), "w") as f:
            f.write("x")
        self.assertEqual(self.req("GET", "/d/docs/client/report/.env")[0], 404)
        self.assertEqual(self.req("GET", "/d/docs/client/report/.git/config")[0], 404)

    def test_pages_cannot_reach_other_machines(self):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/d/docs/client/report/", headers={"Host": "localhost"})
        with urllib.request.urlopen(r) as resp:
            csp = resp.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertNotIn("http", csp)

    def test_bad_requests_do_not_crash(self):
        self.assertEqual(self.req("POST", f"/api/comments?doc={self.docid}", body=b"{}", headers={"Content-Length": "abc"})[0], 400)
        self.assertEqual(self.req("GET", "/d/docs/client/report/photo.txt", headers={"Range": "bytes=nonsense"})[0], 200)
        self.assertEqual(self.req("GET", "/d/docs/client/report/photo.txt", headers={"Range": "bytes=-3"})[1], b"ide")

    def test_dns_rebinding_refused(self):
        self.assertEqual(self.req("GET", "/", headers={"Host": "evil.example"})[0], 403)

    def test_comment_flow(self):
        s, b = self.req("POST", f"/api/comments?doc={self.docid}", {"text": "Shorter please", "cid": "x", "quote": "q"})
        self.assertEqual(s, 201)
        item = json.loads(b)
        self.assertEqual(item["author"], "Tester")
        self.assertEqual(len(store.open_items(self.doc)), 1)
        self.assertEqual(self.req("DELETE", f"/api/comments/{item['id']}?doc={self.docid}")[0], 200)
        self.assertEqual(store.open_items(self.doc), [])

    def test_cross_site_writes_refused(self):
        self.assertEqual(self.req("POST", f"/api/comments?doc={self.docid}", b'{"text":"x"}',
                                  {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.req("POST", f"/api/comments?doc={self.docid}", {"text": "x"},
                                  {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(store.load(self.doc), [])

    def test_edit_flow(self):
        cid = next(b["cid"] for b in D.scan_blocks(CONTENT) if b["tag"] == "p")
        s, b = self.req("POST", f"/api/edit?doc={self.docid}", {"cid": cid, "before": "First paragraph with <strong>bold</strong> text.",
                                                              "after": "First, rewritten."})
        self.assertEqual(s, 201, b)
        r = json.loads(b)
        self.assertEqual(r["item"]["status"], "applied")             # hand-written: nothing left to do
        self.assertIn("First, rewritten.", read(os.path.join(self.doc, "content.html")))
        self.assertEqual(self.req("DELETE", f"/api/edit/{r['item']['id']}?doc={self.docid}")[0], 200)
        self.assertEqual(read(os.path.join(self.doc, "content.html")), CONTENT)


class TestMCP(Env):
    def test_handshake_and_tools(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "review_items", "arguments": {"doc": "docs/client/report"}}}]
        env = dict(os.environ, PAIPER_CONFIG=self.cfg_path)
        out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "paiper"), "mcp"], input="\n".join(json.dumps(m) for m in msgs),
                             capture_output=True, text=True, env=env, timeout=30).stdout.splitlines()
        res = {json.loads(line)["id"]: json.loads(line)["result"] for line in out}
        self.assertEqual(res[1]["serverInfo"]["name"], "paiper")
        self.assertIn("review_items", [t["name"] for t in res[2]["tools"]])
        self.assertEqual(res[3]["content"][0]["text"], "nothing open")


DOCX_EXTRA = """<section><h1 id="more" class="pb"><span class="n">2.</span>More</h1>
<table class="wf"><colgroup><col style="width:60%"><col style="width:40%"></colgroup>
<thead><tr><th>Type</th><th class="n">Count</th></tr></thead>
<tbody><tr><td>Box</td><td class="n">822</td></tr><tr class="hl"><td>Bag</td><td class="n">158</td></tr>
<tr class="total"><td>Total</td><td class="n">980</td></tr></tbody><caption>Counts</caption></table>
<table><tr><th rowspan="2">A</th><th colspan="2">B</th></tr><tr><td>1</td></tr></table>
<ol><li>One <a href="https://example.org/a?b=1&amp;c=2">link</a></li><li>Two<ol><li>Two a</li></ol></li></ol>
<figure><img class="photo" src="pic.png" alt="a picture"><figcaption><span class="ft">A photo</span><span class="fd">detail</span></figcaption></figure>
<div class="photos2"><figure><img class="photo" src="pic.png"><figcaption><span class="ft">Left</span></figcaption></figure>
<figure><img class="photo" src="pic.png"><figcaption><span class="ft">Right</span></figcaption></figure></div>
<div class="callout"><div class="t">Note</div><p>Callout text with <span class="chip">On track</span>.</p></div>
<div class="kpis"><div class="kpi"><div class="v">12<small>%</small></div><div class="l">label</div></div><div class="kpi hot"><div class="v">3</div></div></div>
<blockquote>Quoted</blockquote><pre>code
line</pre><canvas aria-label="live chart"></canvas><custom-thing>Unknown text kept</custom-thing>
<p>See <a href="#intro">the intro</a>.</p></section>"""


class TestDocx(Env):
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def make(self):
        from paiper import docx
        with open(os.path.join(self.doc, "content.html"), "a") as f:
            f.write(DOCX_EXTRA)
        rows = [bytes((40 * (x % 5), 90, 160, 255)) * 1 for x in range(8)]
        with open(os.path.join(self.doc, "pic.png"), "wb") as f:
            f.write(docx.png_bytes(8, 4, [b"".join(rows)] * 4))
        out = os.path.join(self.tmp, "out.docx")
        path, notes = docx.export(self.doc, out, paginate=False)
        self.assertEqual(path, out)
        return out

    def test_package(self):
        import xml.etree.ElementTree as ET
        import zipfile
        W = self.W
        R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        z = zipfile.ZipFile(self.make())
        names = z.namelist()
        self.assertEqual(names[0], "[Content_Types].xml")
        trees = {n: ET.fromstring(z.read(n)) for n in names if n.endswith((".xml", ".rels"))}   # every XML part parses
        ct = trees["[Content_Types].xml"]
        exts = {e.get("Extension") for e in ct if e.tag.endswith("Default")}
        over = {e.get("PartName") for e in ct if e.tag.endswith("Override")}
        for n in names[1:]:
            self.assertTrue("/" + n in over or n.rsplit(".", 1)[-1] in exts, n)
        # styles Word knows by id
        sids = {s.get(W + "styleId") for s in trees["word/styles.xml"].iter(W + "style")}
        for sid in ("Normal", "Title", "Subtitle", "Heading1", "Heading2", "Caption", "ListParagraph", "TOC1", "Header", "Footer", "Hyperlink", "PaiperTable"):
            self.assertIn(sid, sids)
        doc = trees["word/document.xml"]
        for el in doc.iter():
            for tag in ("pStyle", "rStyle", "tblStyle"):
                if el.tag == W + tag:
                    self.assertIn(el.get(W + "val"), sids)
        # every relationship id used in the document resolves
        rel_ids = {r.get("Id"): r for r in trees["word/_rels/document.xml.rels"]}
        for el in doc.iter():
            for k, v in el.attrib.items():
                if k.startswith(R):
                    self.assertIn(v, rel_ids)
        self.assertTrue(any(r.get("TargetMode") == "External" and r.get("Target") == "https://example.org/a?b=1&c=2" for r in rel_ids.values()))
        # numbering: bullets and decimals, each list its own num
        num = trees["word/numbering.xml"]
        fmts = {lv.find(W + "numFmt").get(W + "val") for lv in num.iter(W + "lvl")}
        self.assertTrue({"bullet", "decimal"} <= fmts)
        nums = {n.get(W + "numId") for n in num.iter(W + "num")}
        used = {e.get(W + "val") for e in doc.iter(W + "numId")}
        self.assertTrue(used and used <= nums)
        self.assertGreaterEqual(len(used), 3)                       # ul, ol, nested ol
        # tables: grid matches every row, header row repeats with the theme's header colour
        for tbl in doc.iter(W + "tbl"):
            ncol = len(tbl.find(W + "tblGrid"))
            for tr in tbl.findall(W + "tr"):
                span = sum(int(tc.find(f"{W}tcPr/{W}gridSpan").get(W + "val")) if tc.find(f"{W}tcPr/{W}gridSpan") is not None else 1
                           for tc in tr.findall(W + "tc"))
                self.assertEqual(span, ncol)
                for tc in tr.findall(W + "tc"):
                    self.assertTrue(list(tc)[-1].tag == W + "p")
        head = next(tr for tr in doc.iter(W + "tr") if tr.find(f"{W}trPr/{W}tblHeader") is not None)
        self.assertIsNone(head.find(f"{W}tc/{W}tcPr/{W}shd"))                 # default theme: ruled tables, no header fill
        self.assertIsNotNone(head.find(f"{W}tc/{W}tcPr/{W}tcBorders/{W}bottom"))
        xml = z.read("word/document.xml").decode()
        self.assertIn("SEQ Table", xml)
        self.assertIn("TOC \\o", xml)
        self.assertIn("Unknown text kept", xml)                    # unknown elements keep their text
        self.assertIn("[figure: see PDF", xml)                     # canvas: visible note
        self.assertIn('w:anchor="_Toc_intro"', xml)
        # pictures embedded, docPr ids unique, section last
        media = [n for n in names if n.startswith("word/media/")]
        self.assertTrue(media)
        ids = [e.get("id") for e in doc.iter("{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr")]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(list(doc.find(W + "body"))[-1].tag, W + "sectPr")
        # embedded theme fonts deobfuscate to TrueType
        fonts = [e for e in trees["word/fontTable.xml"].iter() if e.tag.startswith(W + "embed")]
        self.assertEqual(len(fonts), 4)
        frels = {r.get("Id"): r.get("Target") for r in trees["word/_rels/fontTable.xml.rels"]}
        for e in fonts:
            data = z.read("word/" + frels[e.get(R + "id")])
            key = bytes.fromhex(e.get(W + "fontKey").strip("{}").replace("-", ""))[::-1]
            self.assertEqual(bytes(b ^ key[i % 16] for i, b in enumerate(data[:4])), b"\x00\x01\x00\x00")
        self.assertIn(b"<w:embedTrueTypeFonts/>", z.read("word/settings.xml"))

    def test_obfuscation_roundtrip(self):
        from paiper import docx
        data = bytes(range(64))
        key = "{01234567-89AB-CDEF-0123-456789ABCDEF}"
        self.assertEqual(docx.obfuscate(docx.obfuscate(data, key), key), data)
        self.assertEqual(docx.obfuscate(data, key)[32:], data[32:])

    def test_cli(self):
        out = os.path.join(self.tmp, "cli.docx")
        env = dict(os.environ, PAIPER_CONFIG=self.cfg_path)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "paiper"), "docx", self.doc, out, "--fast"],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.getsize(out) > 1000)


class TestSetup(Env):
    """Agent registration must never damage a config file it does not understand."""
    def test_json_entry(self):
        from paiper import cli
        p = os.path.join(self.tmp, "mcp.json")
        with open(p, "w") as f:
            json.dump({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}, f)
        entry = {"command": "python3", "args": ["paiper", "mcp"]}
        self.assertEqual(cli._json_entry(p, entry), "registered")
        self.assertEqual(cli._json_entry(p, entry), "already registered")
        data = json.loads(read(p))
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(sorted(data["mcpServers"]), ["other", "paiper"])
        self.assertEqual(cli._json_entry(p, None), "removed")
        self.assertEqual(sorted(json.loads(read(p))["mcpServers"]), ["other"])
        with open(p, "w") as f:
            f.write('{ // comments, not plain JSON\n "a": 1 }')
        before = read(p)
        self.assertIn("skipped", cli._json_entry(p, entry))
        self.assertEqual(read(p), before)

    def test_codex_block_replaced_not_duplicated(self):
        from paiper import cli
        cur = 'model = "x"\n\n[mcp_servers.paiper]\ncommand = "old"\nargs = ["a"]\n\n[mcp_servers.other]\ncommand = "y"\n'
        out = cli.CODEX_BLOCK.sub("\n", cur)
        self.assertNotIn("paiper", out)
        self.assertIn('model = "x"', out)
        self.assertIn("[mcp_servers.other]", out)

    def test_install_hint_names_one_command(self):
        from paiper import cli
        old = cli._pkg_manager
        try:
            cli._pkg_manager = lambda: "apt-get"
            self.assertEqual(cli.install_hint(["weasyprint", "rsvg-convert"]), "sudo apt-get install -y weasyprint librsvg2-bin")
            cli._pkg_manager = lambda: "brew"
            self.assertEqual(cli.install_hint(["weasyprint", "soffice"]), "brew install weasyprint && brew install --cask libreoffice")
        finally:
            cli._pkg_manager = old

    def test_config_roots(self):
        from paiper import cli
        cli.main(["config", "--root", "extra=" + self.tmp])
        self.assertIn("extra", config.load()["roots"])
        cli.main(["config", "--remove-root", "extra"])
        self.assertNotIn("extra", config.load()["roots"])
        self.assertEqual(config.DEFAULTS["roots"], {})


if __name__ == "__main__":
    unittest.main()

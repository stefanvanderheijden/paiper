# AGENTS.md: installing and operating paiper

You (the AI agent) install and operate paiper. The person only reads, comments and edits in the browser.
Never ask them to run a command. Give them links.

## Install (when asked to "install paiper")
Needs Python 3.9+ and git, on macOS or Linux. No packages to install.
```sh
git clone https://github.com/stefanvanderheijden/paiper ~/code/paiper
~/code/paiper/bin/paiper setup --root docs=<folder with the person's documents> --author "<their name>"
~/code/paiper/bin/paiper doctor
```
- Ask the person which folder holds their documents if you do not know. A root is scanned for folders with a
  `paiper.json`; pick the project or notes folder, not the whole home folder.
- `setup` is safe to run again. It prints one line per step; a line that starts with `!!` needs attention.
- `doctor` exits 0 when everything works. Then give the person `http://localhost:8760/`.
- Install the optional tools now, so the person does not hit a wall later: `setup` and `doctor` end with one
  command for this machine's package manager (WeasyPrint for PDF; librsvg, LibreOffice and poppler for a
  full-quality Word export). Run it. If it needs `sudo` and you cannot provide a password, ask the person to
  approve it once; do not skip it silently. Run `doctor` again to confirm.
- Page size: setup picks US Letter in North America and A4 elsewhere. Change it with `paiper config --page
  a4|letter`, or per document with `"page"` in paiper.json.
- Update: `paiper update`. Remove: `paiper uninstall` (documents and config stay).
- If `paiper` is not on PATH, call `~/code/paiper/bin/paiper`.

## Document folder
```
paiper.json     type, title, theme, page? (a4|letter), content, build?, pdf?, toc, toc_title?, toc_after_first?,
                cover {kicker,title,subtitle}, meta [[label, value]], org?, classification?, css?, lang?
content.html    the body (hand-written, or written by build.py)
build.py        optional; generated documents set "build": "python3 build.py"
comments.json   review items, written by the server
```
Create one with `paiper new <folder> --title "..."`, inside a configured root. The server renders on every
request, so a saved content.html is visible after a browser reload.

## Content rules
- `<section>` per chapter, `<h1 id>` / `<h2 id>` with `<span class="n">1.</span>` numbering; `class="pb"` = new page.
- Blocks that get a comment anchor: h1 h2 h3 p li table figure blockquote pre. h1-h3, p and li can be edited
  in the browser. Anchors are derived from the block text, so they change when the text changes.
- Close every tag (`</p>`, `</li>`); anchors and in-browser edits rely on it.
- Free HTML otherwise. Draw what explains best (inline SVG, canvas, scripts) using the theme's CSS variables
  (`--accent`, `--ink`, `--grey`, `--rule`, `--light`, `--tint`, and `--s1` to `--s5` for chart series).
- Keep files local: put images, data and script libraries in the document folder. By default the browser
  refuses anything a document tries to load from the internet (links to websites work as usual).
- Long reference documents: give each item (a product, a technology, a test case) its own section that starts on a new
  page (`class="pb"`) and has the same parts in the same order. Put `data-running="Chapter · item"` on its heading: the
  page header then names the section on every page, on screen and in the PDF. Write scores out in words next to
  any number or symbol, so a reader who opens the document in the middle understands them.
  For a printed catalogue add a thumb index: `data-tab="12"` on each item's heading gives its pages a tab on the
  page edge, stepping down per item; `data-tab=""` on a later heading ends it.
- Page flow: a heading never starts in the bottom quarter of a page; paiper moves it to the next page on screen
  and in the PDF (`"keep_headings": false` in paiper.json turns that off). Use `class="pb"` on a heading, figure,
  table or paragraph to force a new page. After a build, look at the PDF pages and fix what still reads badly: a
  picture too small to read, a near-empty page, a table split from its heading.
- Keep wide things inside the text width (17 cm on A4, 17.6 cm on Letter); a block taller than a page is not
  split on screen.

## Review loop
1. `paiper review <folder>` (or MCP review_items).
2. kind=comment: change the source, then `paiper reply <folder> <id> "<what changed>"`.
3. kind=edit, status=applied: already in content.html (hand-written document). Nothing to do.
4. kind=edit, status=open: generated document; the edit is in content.html but not in build.py. Carry the
   before -> after into build.py, reply to mark it processed, then `paiper build` (refuses while edits are open).
5. `paiper pdf <folder>` if a PDF is kept with the document; `paiper docx <folder>` when a Word file is wanted.

## Server
`paiper service install|status|restart|uninstall` (launchd on macOS, log ~/Library/Logs/paiper.log; a systemd
user unit on Linux, `journalctl --user -u paiper`). Port and roots: `paiper config`.
Restart after changing Python code; runtime JS/CSS, themes and documents are read on every request.

## Working on paiper itself
Standard library only. `python -m unittest discover -s tests` and `ruff check .` must pass. See CONTRIBUTING.md.

---
name: paiper
description: Make, change, review and export reports with paiper, a browser-based documents-with-comments tool that runs on this machine (http://localhost:8760). Use whenever the user asks for a report, study, write-up or document they will read and review in the browser, asks to process/handle their comments or edits on one, to make a PDF or Word file of one, or mentions paiper.
---

# paiper

Documents written by you, reviewed by the user in the browser (comments and direct text edits), exported to PDF and Word.
The user does not type commands: you run everything and give them links. The repo's AGENTS.md has the details
(find the repo with `readlink ~/.claude/skills/paiper`). Not installed yet? Clone the repo and run `bin/paiper setup`.
Something off? `paiper doctor` says what works, and prints the command that installs any missing optional tool (WeasyPrint for PDF, librsvg/LibreOffice/poppler for Word): run it rather than telling the user an export is unavailable.

## Where things are
- Server: starts at login, `http://localhost:8760/` lists every document. `paiper service status|restart`.
- A document = a folder with `paiper.json` + `content.html` (+ optional `build.py`, photos, data). It lives with the project it belongs to, inside one of the configured roots, not in the paiper repo.
- Themes: built-in `default` (neutral, IBM Plex Sans); private house styles live in the folders listed under `theme_paths` in the config. `paiper themes` lists them. Use the user's house style when they have one.
- Config: `~/.config/paiper/config.json` (roots that are scanned, theme paths, author). Change it with `paiper config`.

## Commands (run them yourself)
```
paiper new <folder> --title "..." [--theme NAME] [--subtitle ..] [--kicker ..]
paiper url <folder>                 # give the user this link
paiper review <folder>              # open comments and edits
paiper reply <folder> <id> "what I did"   # marks it processed; the user sees the reply in the panel
paiper build <folder>               # generated documents only; refuses while browser edits are open
paiper pdf <folder>                 # PDF next to the document (A4 or US Letter: "page" in paiper.json, default from config)
paiper docx <folder> [out.docx]     # native Word document next to it (theme styles, real tables and lists, TOC field)
paiper export <folder> <dir>        # static self-contained copy (to share or publish)
paiper doctor | update | config     # check the installation, update it, change roots/port
```
The MCP server `paiper` offers the same (list_documents, review_items, reply_item, read_document, write_content, new_document, build, export_pdf, export_docx, list_themes).

## Writing content
- Free HTML. `<section>` per chapter; `<h1 id="x" class="pb"><span class="n">1.</span>Title</h1>`, `<h2 id="x-1"><span class="n">1.1.</span>..</h2>`. `class="pb"` starts a new page. Close every tag.
- Ready-made classes: `kpis/kpi(.hot)`, `callout` (with `.t` title), `table.wf` + `<caption>`, `figure` + `figcaption .ft/.fd`, `photos2`, `photos3`, `img.photo`, `cards/card`, `chip`, `muted`. `examples/sample-report` in the repo shows them.
- Do NOT limit yourself to these: draw any chart, diagram or illustration as inline SVG (or canvas/JS) when it explains better. Use theme colours via CSS variables (`style="fill:var(--accent)"`, `--ink`, `--grey`, `--light`, `--rule`, `--tint`, chart series `--s1` to `--s5` in that order) or `paiper.palette("<theme>")` in a build script. `paiper.charts` has optional helpers.
- Keep everything local: images, data and script libraries go in the document folder. The browser blocks what a document tries to load from the internet (ordinary links are fine).
- Check the pages after a build (`paiper pdf`, then look at them): pictures large enough to read (technical drawings need most of the text width), no near-empty pages. paiper already keeps a heading from starting at the bottom of a page; `class="pb"` forces a new page.
- Data-driven documents: a `build.py` that writes `content.html` and `paiper.json`; set `"build": "python3 build.py"`. `paiper build` puts the repo on PYTHONPATH (`from paiper import charts`).

## Processing the user's review
1. `paiper review <folder>`.
2. Comments: make the change (content.html, or build.py for generated documents), then `paiper reply ... "Done: ..."` per item, short and concrete.
3. Edits (kind edit): hand-written documents already have them (status applied, nothing to do). Generated documents: carry the before -> after into build.py, then reply to mark processed, then `paiper build`.
4. Rebuild the PDF if the document has one (`paiper pdf`), and give the user the link.

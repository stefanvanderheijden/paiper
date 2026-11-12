# Changelog

## 0.2.0 (2026-11-12)

Security
- Content-Security-Policy on every page: documents cannot load code from or send data to another machine
  (`paiper config --remote-content on` lifts it).
- Hidden files and folders inside a document folder (`.env`, `.git`) are never served.
- A document title can no longer break out of the page's configuration block.
- Malformed `Range` and `Content-Length` headers are handled instead of raising.

Reliability
- Review items are locked across processes: a comment added in the browser while an agent replies is not lost.
- Content and config files are written atomically.
- Comment anchors skip `<script>`, `<style>` and HTML comments, so a document's own JavaScript is left intact.
- Paths with spaces work in the systemd unit, the launchd plist and PDF export.

Setup
- `paiper doctor` (what works), `paiper update`, `paiper uninstall`.
- `setup` checks that the server answers, continues past a failing step, never rewrites an agent config file it
  cannot parse, and replaces a stale Codex entry instead of leaving it. `--no-agents` skips agent registration.
- `paiper config --remove-root`, `--remote-content on|off`.
- `setup` and `doctor` print one command that installs the optional tools that are missing (WeasyPrint, librsvg,
  LibreOffice, poppler) for the machine's package manager.

Documents
- US Letter next to A4: `"page"` in paiper.json, default in the config (`paiper config --page`), chosen from the
  locale on first setup. Screen, PDF and Word follow it.
- Default theme restyled: cover, headings, ruled tables, key figures, and a colourblind-safe chart palette
  (`--s1` to `--s5`, available in every theme).
- Word export (`paiper docx`, MCP `export_docx`).
- New sample report with two data charts.

## 0.1.0 (2026-11-12)

First release.
- Reports: A4 pages on screen and in PDF (WeasyPrint), cover, contents, numbered figures and tables.
- Review: comments on any block or selection, direct text edits, undo, replies from the agent.
- Generated documents: edits are queued for the build script; `paiper build` refuses while any are open.
- Viewer: zoom (buttons, keys, pinch), lightbox for photos and charts.
- Themes: built-in `default`; private themes from configured folders. The interface is never themed.
- Agents: CLI, MCP server, Claude Code skill, AGENTS.md; `paiper setup` registers them for Claude Code,
  the Claude desktop app, Codex, Cursor and Gemini CLI.
- Local server with home page; launchd (macOS) and systemd (Linux) service.

# Contributing

- Keep it dependency-free: Python standard library only (WeasyPrint stays optional, for PDFs).
- Python 3.9+. Run `python -m unittest discover -s tests` and `ruff check .` before a pull request.
- The reading interface (`paiper/runtime/ui.css`, top bar, panels) is paiper's own; themes only style the
  pages. Keep that split.
- Content is free HTML. Do not add features that restrict what an agent may put in a document.

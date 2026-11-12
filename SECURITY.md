# Security

paiper runs a local web server that can write to files in the folders you configure. It is built for one
person on their own machine. It has no accounts and no passwords, because it never listens on the network.

## What the server does
- Binds to `127.0.0.1` only.
- Refuses requests whose `Host` header is not `localhost`, `127.0.0.1` or `[::1]` (DNS rebinding).
- Accepts writes (`POST`, `PUT`, `DELETE`) only with a JSON body and, when the browser sends an `Origin`, a
  localhost origin. A page on another site cannot write to your documents: a JSON request triggers a CORS
  preflight, which paiper never grants.
- Serves static files only from inside folders that contain a `paiper.json`. It never serves `comments.json`,
  `paiper.json`, or hidden files and folders (`.env`, `.git`).
- Reduces text edits from the browser to inline formatting (bold, italic, links with http(s)/mailto or
  relative targets, a few classes) before it writes them.
- Sends a Content-Security-Policy with every page: a document can run its own inline scripts, but the browser
  will not let it load code from, or send data to, another machine. `paiper config --remote-content on` lifts
  this if your documents need resources from the internet.

## What it does not protect against
- **Documents are trusted HTML.** Whoever writes `content.html` can put scripts in it. All documents share one
  origin (`localhost:8760`), so a script in one document can read and edit the others. The policy above stops
  it from fetching or posting to other machines, but a script can still navigate the tab to another site.
  Do not put documents from people you do not trust under a configured root.
- **Build scripts are code.** `paiper build` runs the command in the document's `paiper.json` (`"build"`), like
  `make` runs a Makefile. Read it before building a document you did not write.
- **Other users on a shared machine** can reach `localhost:8760`. Use paiper on a machine that is yours.
- **Setup changes agent configuration.** `paiper setup` adds an MCP server entry named `paiper` to the agents it
  finds (Claude Code, Claude desktop, Codex, Cursor, Gemini CLI) and a login service. `--no-agents` and
  `--no-service` skip those; `paiper uninstall` removes them.

Report a vulnerability privately through GitHub's security advisories for this repository.

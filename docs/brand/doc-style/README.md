# Open DJ doc style

One look and one voice for anything a person reads outside the deck: briefs,
runbooks, status pages, claude.ai artifacts and the app's own error screens.
Open [`style-guide.html`](style-guide.html) in a browser for the rules (sections S1 to S9).

## Files

| File | What it is |
|---|---|
| `odj-doc.css` | The stylesheet: dark-first tokens, warm light theme, components. Loads Anybody from the offline subset beside it |
| `anybody-800-w150-headline.woff2`, `Anybody-OFL.txt` | The shipped font subset (A-Z, a-z, digits, basic punctuation) and its SIL OFL 1.1 license, copied from `apps/desktop/setup/` |
| `template-brief.html` | Decision brief: verdict, facts, evidence table, open items |
| `template-runbook.html` | Runbook: runs-on facts, numbered steps, red-line table, one copy button |
| `template-error-page.html` | In-app error screen, shaped on the desktop engine-startup page |
| `template-status.html` | Status page: counts strip, then task rows with pills |
| `artifact-inline.html` | One-file variant for claude.ai artifacts: CSS inlined, Anybody from Google Fonts |
| `style-guide.html` | The guide itself, S1 to S9, including the open decisions |

## Which to use

- **Repo doc** (lives in git, must render offline): copy a `template-*.html` and keep it
  linking `odj-doc.css`. If the doc lives in another folder, fix the relative href.
- **claude.ai artifact**: start from `artifact-inline.html`. Artifacts cannot load repo
  files, so the repo templates will render unstyled there. Paste the body sections of the
  matching template into it.
- **In-app page** (shipped inside the app): start from `template-error-page.html` and ship
  `odj-doc.css`, the woff2 and the license beside it. Never a CDN; the page must work with
  no network and no engine.

## Start a new doc

1. Copy the template that matches the page type (S8 in the guide).
2. Replace every `{{PLACEHOLDER}}`. The HTML comment above each slot says what goes in it.
3. Delete the slot comments and any section you do not need. A finished doc has no `{{`.
4. Voice: answer first, weekday dates (`Thu 1 Oct 2026`), UTC times, a source line per
   claim that can drift, a `title` on every number, and a "What exactly was tested" footer.

## Open decisions

D1 (three oranges are live), D2 (two font paths) and D3 (dark-first for docs) are still
open. See S9 in `style-guide.html`.

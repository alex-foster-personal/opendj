# Launch wordmark font: Anybody 800, width 150, ALL CAPS

Selected on Thu 10 Sep 2026 to sit beside the
terracotta two-shade mark. Open [`picker.html`](picker.html) in any browser to re-run the
comparison. It loads the candidates from Google Fonts. It opens in a fixed order, and
**Reshuffle** re-blinds it.

## Rounds

| Round | Date | Set | Change |
|---|---|---|---|
| 1 | Thu 10 Sep 2026 | 7 | Current system font (SF Pro 700, as the control) plus Unbounded 700, Syne 800, Archivo Expanded 800, Bricolage Grotesque 800, Big Shoulders Display 800, Michroma. Mixed case. |
| 2 | Thu 10 Sep 2026 | 11 | Syne, Big Shoulders and Michroma dropped. Added Anybody 800 (wdth 150), Krona One, Sora 800, Red Hat Display 900, Dela Gothic One, Familjen Grotesk 700, Rubik Mono One. |
| 3 | Thu 10 Sep 2026 | 19 | The three from round 2 were restored. Added caps-first faces: Bebas Neue, Anton, Archivo Black, Syncopate 700, Oswald 700. ALL CAPS became the default, with a toggle for mixed case. |
| Pick | Thu 10 Sep 2026 | 1 | **Anybody 800, wdth 150**, in ALL CAPS. |

Every other candidate is still in `picker.html`, so the decision can be reopened without
rebuilding anything.

## What shipped

- `apps/webui/frontend/static/fonts/anybody-800-w150-wordmark.woff2`: a 1.6 KB static
  instance, subset to `OPENDJopendj` and space, so the app stays offline and self-contained.
  If the wordmark text ever changes, regenerate it from Google Fonts' css2 API with a
  `text=` parameter listing the new glyphs.
- `Anybody-OFL.txt`: the SIL Open Font License 1.1 that ships beside it.
- `BrandLaunch.svelte`: the launch-screen wordmark uses it, uppercase.
- `apps/desktop/setup/`: the desktop engine-startup page's headlines ("Starting Open DJ",
  "Open DJ cannot reach its engine") match it. That page has to render when nothing else
  works, so it carries its own 6.4 KB copy (A-Z, a-z, digits, basic punctuation) and its
  licence in the same directory, and falls back to the system font if the file is missing.

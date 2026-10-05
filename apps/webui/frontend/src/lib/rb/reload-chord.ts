/**
 * Who owns the R chord: the page (LIBUX-05's overlay-mode toggle) or the host.
 *
 * LIBUX-05 bound Cmd+R to technically-working mode, which hides every
 * /performance region so the desktop shows through the window. Cmd+R is the
 * reload chord, and claiming it turned a refresh into "the whole UI vanished
 * and nothing reloaded": the page hid itself, kept the hidden regions'
 * separator rules, and preventDefault stopped the reload. LIBUX-29 fixed that
 * for a browser tab; LIBUX-31 extends it to the packaged desktop shell, where
 * the same near-empty window confused the user just as much.
 *
 * So the page never claims Cmd+R, on any host. The rule is on the modifier,
 * not on a platform or shell sniff. Ctrl+R stays the page's, so overlay mode
 * keeps a keyboard door. That leaves one known gap, unchanged by this module:
 * Windows or Linux, where Ctrl+R is the reload chord and the page still
 * claims it.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 the page never claims Cmd+R, in a tab or a desktop shell.
 *     [if] `pageOwnsReloadChord({ metaKey: true })` is true [then ⛔️] broken
 *   ✔︎ ✅ 🎯 the page still claims Ctrl+R.
 *     [if] `pageOwnsReloadChord({ metaKey: false })` is false [then ⛔️] broken
 *   ✔︎ ✅ 🎯 the answer takes no shell kind.
 *     [if] the module imports the shell probe [then ⛔️] broken
 */
export function pageOwnsReloadChord(chord: { readonly metaKey: boolean }): boolean {
	return !chord.metaKey;
}

/**
 * Who owns the R chord: the page (LIBUX-05's overlay-mode toggle) or the host.
 *
 * LIBUX-05 binds Cmd+R to technically-working mode, which hides every
 * /performance region so the desktop shows through the window. That only
 * means something where the window can be transparent, which is the desktop
 * shell. In a browser tab Cmd+R is the reload chord, and claiming it there
 * turned a refresh into "the whole UI vanished and nothing reloaded": the
 * page hid itself, kept the hidden regions' separator rules, and
 * preventDefault stopped the reload (LIBUX-29).
 *
 * The rule is on the modifier, not on a platform sniff: Meta+R is a reload
 * chord only on Apple platforms, and on the others the Meta key never
 * delivers an R chord to a page at all. Ctrl+R stays the page's in a tab, so
 * overlay mode keeps a keyboard door there. That leaves one known gap,
 * unchanged by this module: a browser tab on Windows or Linux, where Ctrl+R
 * is the reload chord and the page still claims it.
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 a browser tab never claims Cmd+R.
 *     [if] `pageOwnsReloadChord({ metaKey: true }, null)` is true [then ⛔️] broken
 *   ✔︎ ✅ 🎯 a desktop shell claims Cmd+R and Ctrl+R alike.
 *     [if] a shell kind is given and the answer is false [then ⛔️] broken
 *   ✔︎ ✅ 🎯 a browser tab still claims Ctrl+R.
 *     [if] `pageOwnsReloadChord({ metaKey: false }, null)` is false [then ⛔️] broken
 */
import type { NativeShellKind } from '$lib/shell/native-shell';

export function pageOwnsReloadChord(
	chord: { readonly metaKey: boolean },
	shell: NativeShellKind | null
): boolean {
	if (shell !== null) return true;
	return !chord.metaKey;
}

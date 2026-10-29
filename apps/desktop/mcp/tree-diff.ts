/**
 * tree-diff: what changed between two ui_tree snapshots, as `act` reports it.
 *
 * Lines are compared WITHOUT their @refs, so a dialog inserted near the top
 * reads as the dialog, not as every later node "changing" its ref. They are
 * compared as a MULTISET: two identical "Remove" buttons are two lines, and
 * losing one of them is a change.
 */

/** A tree line without its @ref: refs renumber when anything is inserted above.
 * The ref is always the line's last token (webview-mcp's walk appends it), so
 * only that position is stripped: " @e12" inside a quoted name is text. */
export function refless(line: string): string {
	return line.replace(/ @e\d+$/, '');
}

/** Lines of `from` with no counterpart left in `against`, matched one for one. */
function _unmatched(from: readonly string[], against: readonly string[]): string[] {
	const available = new Map<string, number>();
	for (const line of against) {
		const key = refless(line);
		available.set(key, (available.get(key) ?? 0) + 1);
	}
	return from.filter((line) => {
		const key = refless(line);
		const left = available.get(key) ?? 0;
		if (left === 0) return true;
		available.set(key, left - 1);
		return false;
	});
}

/** A new dialog or menu is what the caller most needs named, so it leads every diff. */
function _isSurface(line: string): boolean {
	return /^\s*(dialog|alertdialog|menu|listbox)\b/.test(line);
}

export function diffTrees(before: readonly string[], after: readonly string[], lineCap: number): string {
	const removed = _unmatched(before, after);
	// Added lines keep their (new) refs so the caller can act on them.
	const added = _unmatched(after, before);
	if (removed.length === 0 && added.length === 0) return '(no tree change)';
	if (removed.length + added.length > lineCap) {
		// Preview the SHALLOWEST new nodes: they name what appeared (a dialog, a
		// panel) without paying for its full contents.
		// A new dialog or menu is what the caller most needs named, even when
		// shallower plain text also changed (measured on the Settings dialog).
		const indentOf = (l: string): number => l.length - l.trimStart().length;
		const surface = (l: string): number => (_isSurface(l) ? 0 : 1);
		const preview = [...added]
			.sort((a, b) => surface(a) - surface(b) || indentOf(a) - indentOf(b))
			.slice(0, 12)
			.map((l) => `+ ${l.trim()}`);
		return (
			`(large change: ${removed.length} lines gone, ${added.length} new -- ` +
			`re-observe with ui_tree. New dialogs and menus first, then the shallowest new nodes:)\n${preview.join('\n')}`
		);
	}
	// Dialogs first, then removals, then the rest of the additions: a row
	// removed by the same action must not push the new dialog down the list.
	const surfaced = added.filter(_isSurface);
	const rest = added.filter((l) => !_isSurface(l));
	return [
		...surfaced.map((l) => `+ ${l.trim()}`),
		...removed.map((l) => `- ${l.trim()}`),
		...rest.map((l) => `+ ${l.trim()}`)
	].join('\n');
}

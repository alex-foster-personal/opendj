/**
 * The first-run native folder picker as one testable step, split out of
 * SetupOverlay.svelte so node tests can drive every outcome (#13152).
 *
 * Only a returned path reaches the wizard: a missing desktop bridge, a
 * cancelled dialog and a rejected dialog (or a failed plugin load) all leave
 * whatever the user already typed on the row untouched.
 */

import { nativeShellKind, pickFolder, type Scope } from '$lib/shell/native-shell';

/** Open the native picker for one row and hand a chosen path to `choose`. */
export async function pickFolderIntoRow(
	rowId: string,
	choose: (rowId: string, path: string) => void,
	scope: Scope = globalThis
): Promise<'unavailable' | 'cancelled' | 'failed' | 'chosen'> {
	if (nativeShellKind(scope) === null) return 'unavailable';
	try {
		const selected = await pickFolder('Choose a folder', scope);
		if (typeof selected !== 'string') return 'cancelled';
		choose(rowId, selected);
		return 'chosen';
	} catch {
		// A failed plugin load or rejected dialog must not clear a typed path.
		return 'failed';
	}
}

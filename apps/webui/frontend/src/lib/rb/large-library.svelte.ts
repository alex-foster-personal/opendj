/**
 * Issue #9432: the large-library toggle (rekordbox spacebar analogue). When on,
 * the performance layout grows the library row and holds the wavestack and
 * deck rows at their floors. Session state only; not persisted.
 */
export const largeLibrary = $state({ on: false });

export function toggleLargeLibrary(): boolean {
	largeLibrary.on = !largeLibrary.on;
	return largeLibrary.on;
}

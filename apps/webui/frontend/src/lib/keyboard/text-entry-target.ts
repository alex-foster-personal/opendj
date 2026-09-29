/**
 * Shared gate for global single-key shortcuts (issue #3528).
 *
 * True only when Space or a letter key would edit text in the focused element.
 * Buttons, range sliders, library rows, and page chrome are false so app
 * shortcuts still fire there.
 */
const TEXT_INPUT_TYPES = new Set([
	'',
	'text',
	'search',
	'email',
	'url',
	'tel',
	'password',
	'number'
]);
const TEXT_ROLES = new Set(['textbox', 'searchbox', 'combobox']);

export function isTextEntryTarget(target: EventTarget | null): boolean {
	if (target === null || typeof target !== 'object') return false;
	const el = target as {
		tagName?: string;
		isContentEditable?: boolean;
		type?: string;
		getAttribute?: (name: string) => string | null;
	};
	if (el.isContentEditable === true) return true;
	if (el.tagName === 'TEXTAREA') return true;
	if (el.tagName === 'SELECT') return true;
	if (el.tagName === 'INPUT') return TEXT_INPUT_TYPES.has((el.type ?? '').toLowerCase());
	return TEXT_ROLES.has(el.getAttribute?.('role') ?? '');
}

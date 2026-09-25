const INTERACTIVE_TAGS = new Set([
	'AUDIO',
	'BUTTON',
	'DETAILS',
	'INPUT',
	'SELECT',
	'SUMMARY',
	'TEXTAREA',
	'VIDEO'
]);
const INTERACTIVE_ROLES = new Set([
	'application',
	'button',
	'checkbox',
	'combobox',
	'grid',
	'gridcell',
	'link',
	'listbox',
	'menu',
	'menubar',
	'menuitem',
	'menuitemcheckbox',
	'menuitemradio',
	'option',
	'radio',
	'radiogroup',
	'scrollbar',
	'searchbox',
	'slider',
	'spinbutton',
	'switch',
	'tab',
	'tablist',
	'textbox',
	'tree',
	'treegrid',
	'treeitem'
]);

/**
 * Explicit opt-out for a pointer-only control that carries an ARIA role (so
 * screen readers still describe it correctly) but wires no `onkeydown` of its
 * own - currently only WaveRow.svelte's waveform-seek canvas
 * (role="slider", tabindex="-1"). See pin 18627f290052: that canvas's
 * onPointerDown skips preventDefault whenever the deck is empty or a command
 * is pending, so a click in exactly those states lets the browser's default
 * focus-on-click behavior land on it despite tabindex="-1" - and once it
 * holds focus, every performance hotkey (including 'm') silently no-opped.
 *
 * This is scoped to a data attribute, deliberately NOT to "any negative
 * tabindex": a negative tabindex is a completely normal way to make an
 * element focusable-only-programmatically (roving-tabindex ARIA widgets,
 * modal focus traps, a contenteditable node, a native <input>/<button> a
 * script focuses) while it still owns real keyboard behavior. Keying the
 * exception off tabindex sign alone would have swallowed every one of those
 * keystrokes into the global performance hotkeys instead.
 */
export const POINTER_ONLY_HOTKEY_PASSTHROUGH_ATTR = 'data-hotkey-pointer-only';

/**
 * True when the browser owns the focused element's keyboard behavior.
 *
 * Performance shortcuts are global only while focus is on page chrome. Native
 * controls need Tab to move focus and Space to activate their own action.
 */
export function isNativeInteractiveTarget(target: EventTarget | null): boolean {
	if (!(target instanceof Element)) return false;
	if (target.hasAttribute(POINTER_ONLY_HOTKEY_PASSTHROUGH_ATTR)) return false;
	if (target.hasAttribute('tabindex')) return true;
	if (INTERACTIVE_ROLES.has(target.getAttribute('role') ?? '')) return true;
	if (!(target instanceof HTMLElement)) return false;
	if (target.isContentEditable || INTERACTIVE_TAGS.has(target.tagName)) return true;
	if (target.tagName === 'A' && target.hasAttribute('href')) return true;
	return false;
}

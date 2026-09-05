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
 * True when the browser owns the focused element's keyboard behavior.
 *
 * Performance shortcuts are global only while focus is on page chrome. Native
 * controls need Tab to move focus and Space to activate their own action.
 */
export function isNativeInteractiveTarget(target: EventTarget | null): boolean {
	if (!(target instanceof Element)) return false;
	if (target.hasAttribute('tabindex')) return true;
	if (INTERACTIVE_ROLES.has(target.getAttribute('role') ?? '')) return true;
	if (!(target instanceof HTMLElement)) return false;
	if (target.isContentEditable || INTERACTIVE_TAGS.has(target.tagName)) return true;
	if (target.tagName === 'A' && target.hasAttribute('href')) return true;
	return false;
}

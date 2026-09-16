/** Pure control-addressing for the performance UI mirror (#3196). */

export const CONTROL_SELECTOR = 'button, input, [role="button"], [role="slider"]';

/** Preferred mirror name: testid, then aria-label, then trimmed text, then index. */
export function controlPreferredName(element: Element, index: number): string {
	return (
		element.getAttribute('data-testid') ??
		element.getAttribute('aria-label') ??
		(element.textContent?.trim() || `control-${index + 1}`)
	);
}

/**
 * Assign a mirror key that is unique among `used`. The first control keeps the
 * preferred name; later duplicates receive deterministic `#2`, `#3`, ... suffixes.
 * Suffix candidates are skipped when already taken so a natural `name#2` cannot
 * collide with a generated alias for a different control.
 */
export function uniqueControlKey(preferredName: string, used: Set<string>): string {
	if (!used.has(preferredName)) {
		used.add(preferredName);
		return preferredName;
	}
	let suffix = 2;
	while (true) {
		const candidate = `${preferredName}#${suffix}`;
		if (!used.has(candidate)) {
			used.add(candidate);
			return candidate;
		}
		suffix += 1;
	}
}

export function controlAvailability(element: Element): 'available' | 'inert' {
	return element.classList.contains('rb-inert') ? 'inert' : 'available';
}

/** One mirror entry per selected control; duplicates are suffixed, never dropped. */
export function buildControlsMap(elements: Iterable<Element>): Record<string, 'available' | 'inert'> {
	const controls: Record<string, 'available' | 'inert'> = {};
	const used = new Set<string>();
	let index = 0;
	for (const element of elements) {
		const key = uniqueControlKey(controlPreferredName(element, index), used);
		controls[key] = controlAvailability(element);
		index += 1;
	}
	return controls;
}

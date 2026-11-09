/**
 * Developer-only taste variant picks (issue #10055).
 *
 * Persisted LOCALLY only (the rb-ui-prefs localStorage blob): never written to
 * the server's ui-prefs file.
 */
import { checkVariantValue, variantSetting } from '../settings/variant-setting';

export interface VariantPrefs {
	variants: Record<string, string>;
}

export const VARIANT_PREF_DEFAULTS: VariantPrefs = { variants: {} };

export type VariantChangeListener = (id: string, from: string, to: string) => void;

const _listeners = new Map<string, Set<VariantChangeListener>>();

export function onVariantChange(id: string, cb: VariantChangeListener): () => void {
	variantSetting(id);
	let set = _listeners.get(id);
	if (!set) {
		set = new Set();
		_listeners.set(id, set);
	}
	set.add(cb);
	return () => {
		set!.delete(cb);
		if (set!.size === 0) _listeners.delete(id);
	};
}

export function makeVariantPrefAccessors(
	state: VariantPrefs,
	persist: () => void
): { readVariant(id: string): string; setVariant(id: string, value: string): void } {
	function readVariant(id: string): string {
		const def = variantSetting(id);
		const saved = state.variants[id];
		if (saved === undefined) return def.defaultValue;
		return checkVariantValue(id, saved, 'rb-ui-prefs variants');
	}

	function setVariant(id: string, value: string): void {
		variantSetting(id);
		checkVariantValue(id, value, 'setVariant');
		const from = readVariant(id);
		if (from === value) return;
		state.variants = { ...state.variants, [id]: value };
		persist();
		const listeners = _listeners.get(id);
		if (listeners) {
			for (const cb of [...listeners]) {
				cb(id, from, value);
			}
		}
	}

	return { readVariant, setVariant };
}

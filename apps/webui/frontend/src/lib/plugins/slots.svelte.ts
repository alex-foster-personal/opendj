/**
 * UI slots: where an installed module's UI mounts into the core app (PLUG-6522i).
 *
 * The engine reports which loaded modules fill which slots
 * (GET /api/v1/plugins/ui-slots). Each module's UI is an ES module served by
 * the engine from the module's own verified files, exporting
 *
 *     export function mount(el: HTMLElement, host: PluginUiHost): void | (() => void)
 *
 * The return value, when a function, is called when the slot unmounts. A
 * module that fails to import or mount is logged and leaves its slot empty;
 * the core UI around it is never affected.
 *
 * WHY SLOTS AND NOT IMPORTS. B2B Link today patches about 16 core components
 * directly. A downloadable module cannot: the core bundle has to name the
 * places a module may draw, and the module brings everything else.
 */

import { API_BASE } from '$lib/api/base';
import { getUiSlots, type UiSlot } from '$lib/api/plugins';
import { memoUntilFailure } from './slot-keys';

/** What a module's mount() receives. Kept small on purpose: it grows only
 * when a module needs something and the seam is reviewed. */
export interface PluginUiHost {
	pluginId: string;
	slot: string;
	/** Absolute base for the engine API, e.g. http://127.0.0.1:8683/api/v1 */
	apiBase: string;
}

export type MountFn = (el: HTMLElement, host: PluginUiHost) => void | (() => void);

class SlotRegistry {
	slots = $state<UiSlot[]>([]);
	// A failed read is forgotten, so the next load() asks again once the engine is back.
	#fetch = memoUntilFailure(() =>
		getUiSlots().then((slots) => {
			this.slots = slots;
		})
	);

	load(): Promise<void> {
		return this.#fetch().catch((exc) => {
			console.warn('[plugins] could not read UI slots', exc);
		});
	}

	for(name: string): UiSlot[] {
		return this.slots.filter((s) => s.slot === name);
	}
}

export const pluginSlots = new SlotRegistry();

/** Resolve an engine-relative URL (`/api/v1/...`) against the API origin. */
export function engineUrl(path: string): string {
	return `${API_BASE.replace(/\/+$/, '')}${path}`;
}

/** Resolve a slot URL (engine-relative) against the API origin. */
export function moduleUrl(slot: UiSlot): string {
	return engineUrl(slot.url);
}

/** Import a module's UI and mount it. Returns the cleanup, or null on failure. */
export async function mountSlot(el: HTMLElement, slot: UiSlot): Promise<(() => void) | null> {
	try {
		const mod = (await import(/* @vite-ignore */ moduleUrl(slot))) as { mount?: MountFn };
		if (typeof mod.mount !== 'function') {
			console.warn(`[plugins] ${slot.plugin_id} UI exports no mount()`);
			return null;
		}
		const cleanup = mod.mount(el, {
			pluginId: slot.plugin_id,
			slot: slot.slot,
			apiBase: `${API_BASE.replace(/\/+$/, '')}/api/v1`
		});
		return typeof cleanup === 'function' ? cleanup : () => {};
	} catch (exc) {
		console.warn(`[plugins] ${slot.plugin_id} UI failed to mount into ${slot.slot}`, exc);
		return null;
	}
}

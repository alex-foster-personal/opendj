/**
 * SETNOTE-6340g: top bars mark where the toggle goes with an empty
 * `<set-notes-toggle>` tag and import nothing from set notes. Once the
 * deferred host loads, this defines the element, and the browser upgrades
 * every instance, now and on later routes, with no DOM observer. Before
 * that the tag is an empty placeholder the top bar sizes.
 */
import { mount, unmount } from 'svelte';
import SetNotesToggle from './SetNotesToggle.svelte';

export const TOGGLE_TAG = 'set-notes-toggle';

class SetNotesToggleElement extends HTMLElement {
	#instance: ReturnType<typeof mount> | null = null;

	connectedCallback(): void {
		this.#instance ??= mount(SetNotesToggle, { target: this });
	}

	disconnectedCallback(): void {
		if (this.#instance) void unmount(this.#instance);
		this.#instance = null;
	}
}

export function defineSetNotesToggle(): void {
	if (!customElements.get(TOGGLE_TAG)) customElements.define(TOGGLE_TAG, SetNotesToggleElement);
}

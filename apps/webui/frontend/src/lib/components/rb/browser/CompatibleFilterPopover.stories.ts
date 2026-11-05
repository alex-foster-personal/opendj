import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import CompatibleFilterPopover from './CompatibleFilterPopover.svelte';

const trigger = createRawSnippet(() => ({
	render: () =>
		`<button type="button" title="Hover or focus to open the compatible-filter ranges">Compatible</button>`
}));

/**
 * Hover or focus the trigger to lazy-load and open the range panel; it
 * closes 120 ms after the pointer or focus leaves. The stories pass a loader
 * that resolves to no panel, so hovering here never reads or writes ui-prefs
 * or any other backend state.
 */
const meta = {
	title: 'Components/Browser/CompatibleFilterPopover',
	component: CompatibleFilterPopover,
	tags: ['autodocs'],
	args: { children: trigger, loadPanel: async () => null, mode: 'transition' }
} satisfies Meta<typeof CompatibleFilterPopover>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Closed (resting) state: only the wrapped trigger shows. */
export const Resting: Story = {};

/** Wrapping a longer trigger, to check the inline-flex alignment. */
export const WideTrigger: Story = {
	args: {
		children: createRawSnippet(() => ({
			render: () =>
				`<span><button type="button">Compatible with Night Drive (Extended Mix)</button><span> 126 BPM, 8A</span></span>`
		}))
	}
};

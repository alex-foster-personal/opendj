import type { Meta, StoryObj } from '@storybook/sveltekit';

import BpmKeyEditPopover from './BpmKeyEditPopover.svelte';

/**
 * The inline BPM or key editor a library cell opens on double-click. It floats
 * above its `anchor`; here the anchor is the story canvas body, so the card
 * sits at the top of the canvas. `oncommit` resolves without saving anything
 * and `onclose` does nothing, so editing here never writes a track. The BPM
 * and key values are invented.
 */
const anchor = (typeof document === 'undefined' ? undefined : document.body) as HTMLElement;

const meta = {
	title: 'Components/Browser/BpmKeyEditPopover',
	component: BpmKeyEditPopover,
	tags: ['autodocs'],
	args: {
		kind: 'bpm',
		anchor,
		initial: '126.00',
		onclose: () => {},
		oncommit: async () => {}
	}
} satisfies Meta<typeof BpmKeyEditPopover>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Editing a BPM: type a tempo, or tap the beat. */
export const Bpm: Story = {};

/** Editing a key: a Camelot key like 8A, or a note like Gm. */
export const Key: Story = {
	args: { kind: 'key', initial: '8A' }
};

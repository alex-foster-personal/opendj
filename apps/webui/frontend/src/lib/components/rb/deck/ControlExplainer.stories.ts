import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';
import { fn } from 'storybook/test';

import ControlExplainer from './ControlExplainer.svelte';
import { TOOLTIP_ISOLATED } from '$lib/ui/tooltip-timing';

/**
 * Hover/focus teaching popover wrapped around any control. Pure props: it
 * only positions and shows copy. Hover or focus the button to open it; the
 * `Pinned` stories use `programmaticOpen` so the popover is visible without
 * interaction. Control copy here is invented sample text, not the real deck
 * help strings. The trigger is a plain button that does nothing.
 */
const trigger = (label: string) =>
	createRawSnippet(() => ({
		render: () =>
			`<button type="button" style="margin:140px 60px;padding:8px 14px;border-radius:50%">${label}</button>`
	}));

const action = createRawSnippet(() => ({
	render: () => '<button type="button" style="padding:4px 8px">Show audio I/O</button>'
}));

const meta = {
	title: 'Components/Deck/ControlExplainer',
	component: ControlExplainer,
	tags: ['autodocs'],
	argTypes: {
		placement: { control: 'inline-radio', options: ['auto', 'above', 'below', 'right'] },
		dismiss: { control: 'inline-radio', options: [undefined, 'instant', 'stay'] },
		pinOnClick: { control: 'boolean' },
		compact: { control: 'boolean' },
		disabled: { control: 'boolean' },
		programmaticOpen: { control: 'boolean' }
	},
	args: {
		title: 'CUE - jump playhead to cue',
		tooltip: TOOLTIP_ISOLATED,
		bullets: [
			'While playing: jump to the cue and pause.',
			'While paused: jump to the cue, or set it if none exists.',
			'Click only - no hold-to-preview.'
		],
		children: trigger('CUE'),
		onProgrammaticClose: fn()
	}
} satisfies Meta<typeof ControlExplainer>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Hover or focus the button: title plus bullets. */
export const Default: Story = {};

/** Pinned open: the popover shows with the mini cue animation above the bullets. */
export const PinnedWithDemo: Story = {
	args: { programmaticOpen: true, demo: 'cue', placement: 'below' }
};

/** A red factual warning above the bullets. */
export const PinnedWithWarning: Story = {
	args: {
		programmaticOpen: true,
		placement: 'below',
		title: 'Hot cue save unavailable',
		warning: 'This track has no rekordbox mapping, so saving a hot cue would fail.',
		bullets: ['Cues can still be played from memory for this session.']
	}
};

/** An interactive action slot: the popover stays open while the pointer is over it. */
export const PinnedWithAction: Story = {
	args: {
		programmaticOpen: true,
		placement: 'below',
		title: 'Audio output',
		bullets: ['Pick the master and headphone devices.'],
		action,
		dismiss: 'stay'
	}
};

/** Compact one-line help with no bullets. */
export const Compact: Story = {
	args: {
		programmaticOpen: true,
		compact: true,
		placement: 'below',
		title: 'Open the settings panel',
		bullets: []
	}
};

/** disabled: no rich popover is ever shown, so hovering does nothing. */
export const Disabled: Story = { args: { disabled: true } };

/** Title only, no bullets/warning/action/demo: nothing rich to show, so the popover never opens. */
export const TitleOnly: Story = { args: { title: 'Plain native tooltip only', bullets: [] } };

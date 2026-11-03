import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import RelocatePopoverView from './RelocatePopoverView.svelte';

/**
 * "Relocate" from a track's context menu: candidate files for a track whose
 * audio went missing. The container finds candidates and applies the pick
 * against the live rekordbox database; this places itself at the click
 * point, takes focus, and closes on Escape or an outside press. Paths are
 * invented placeholders.
 */
const meta = {
	title: 'Components/Browser/RelocatePopoverView',
	component: RelocatePopoverView,
	tags: ['autodocs'],
	args: {
		x: 60,
		y: 40,
		writebackRefusal: null,
		loading: false,
		errorMessage: null,
		candidates: [],
		applying: false,
		onpick: fn(),
		onclose: fn()
	}
} satisfies Meta<typeof RelocatePopoverView>;

export default meta;
type Story = StoryObj<typeof meta>;

const candidate = (path: string, confidence: number) => ({
	path,
	confidence,
	identity_token: `token-${confidence}`,
	signals: ['filename', 'duration'],
	triple_validated: confidence > 0.9
});

/** Two candidate files; each full path is in its hover title. */
export const Candidates: Story = {
	args: {
		candidates: [
			candidate('/Users/dev/Music/Imports/Example Artist - Night Drive (Original Mix).aiff', 0.97),
			candidate('/Volumes/Archive/Music/Example Artist/Night Drive.mp3', 0.71)
		]
	}
};

/** A pick is being applied: every candidate is disabled until it lands. */
export const Applying: Story = {
	args: { ...Candidates.args, applying: true }
};

/** Searching the library roots for candidates. */
export const Loading: Story = { args: { loading: true } };

/** Nothing on disk matched. */
export const NoCandidates: Story = {};

/** The candidate search failed. */
export const Error: Story = { args: { errorMessage: 'Failed to find candidates' } };

/** One-way import mode: relocate would write rekordbox, so the menu is inert. */
export const WritebackOff: Story = {
	args: {
		writebackRefusal: 'Rekordbox write-back is off (one-way import): relocate is disabled'
	}
};

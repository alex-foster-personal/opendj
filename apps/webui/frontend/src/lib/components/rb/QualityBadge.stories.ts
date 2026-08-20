import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { TrackQuality } from '$lib/rb/types';

import QualityBadge from './QualityBadge.svelte';

/**
 * Reference story for the numeric-readout rule - see the "Numeric readouts
 * carry hover titles" decision doc. Every badge below puts its measured
 * numbers (kbps, rung N of M) in the `title`, never on screen alone.
 *
 * Shapes mirror apps/shared/audio_quality.py, the backend that actually
 * measures these. Nothing here is a mock of a network call: QualityBadge is
 * pure props, so the args ARE the contract.
 */
const rung = (over: Partial<TrackQuality>): TrackQuality => ({
	venue: 'club',
	label: 'Club',
	rank: 3,
	of: 6,
	blurb: 'holds up on a club rig',
	kbps: 256,
	container: '.mp3',
	lossless: false,
	...over
});

const meta = {
	title: 'Components/QualityBadge',
	component: QualityBadge,
	tags: ['autodocs'],
	argTypes: {
		showContainer: { control: 'boolean' }
	}
} satisfies Meta<typeof QualityBadge>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Top rung: lossless, so the rung comes from format not bitrate. */
export const Stadium: Story = {
	args: {
		quality: rung({
			venue: 'stadium',
			label: 'Stadium',
			rank: 5,
			blurb: 'survives the biggest room',
			kbps: 1411,
			container: '.flac',
			lossless: true
		})
	}
};

export const Club: Story = { args: { quality: rung({}) } };

/** Bottom rung: the badge is meant to look wrong at a glance. */
export const NaughtyStep: Story = {
	args: {
		quality: rung({
			venue: 'naughty_step',
			label: 'Naughty step',
			rank: 0,
			blurb: 'too lossy for anywhere',
			kbps: 96
		})
	}
};

/**
 * venue null: the backend could not measure it. The badge says Unknown and
 * the hover carries the backend's REASON. It never guesses a rung, because
 * a guessed rung would be mocked data.
 */
export const Unknown: Story = {
	args: {
		quality: rung({
			venue: null,
			label: 'Unknown',
			rank: null,
			blurb: 'duration missing, cannot derive effective bitrate',
			kbps: null
		})
	}
};

/** quality null: the row exists but its audio quality has not loaded yet. */
export const NotLoaded: Story = { args: { quality: null } };

/** Tight rows drop the container suffix; the hover still carries it. */
export const WithoutContainer: Story = {
	args: { quality: rung({}), showContainer: false }
};

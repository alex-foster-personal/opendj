import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { RelatedHitWire } from '$lib/rb/api-rb';

import RelatedTracksList from './RelatedTracksList.svelte';

const sampleRow = {
	stable_id: 'a1b2c3d4e5f6789012345678901234567890abcd',
	title: 'Night Drive',
	artist: 'Example Artist',
	key: '8A',
	bpm: 126,
	genre: 'House',
	score: 3,
	matched: ['key', 'genre', 'bpm']
} as RelatedHitWire;

/**
 * The Related tab list. Props only: fetch state and load/play callbacks, with
 * no global stores or daemon calls.
 */
const meta = {
	title: 'Components/Browser/RelatedTracksList',
	component: RelatedTracksList,
	tags: ['autodocs'],
	args: {
		state: { kind: 'idle' as const },
		onload: fn(),
		onplay: fn()
	}
} satisfies Meta<typeof RelatedTracksList>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Idle: Story = {};

export const Loading: Story = {
	args: { state: { kind: 'loading' } }
};

export const Error: Story = {
	args: { state: { kind: 'error', message: 'related tracks failed: backend unreachable' } }
};

export const Empty: Story = {
	args: {
		state: { kind: 'loaded', seedTitle: 'Seed Track', items: [] }
	}
};

export const WithRows: Story = {
	args: {
		state: {
			kind: 'loaded',
			seedTitle: 'Seed Track',
			items: [
				sampleRow,
				{
					...sampleRow,
					stable_id: sampleRow.stable_id.replace('a', 'b'),
					title: 'Closer',
					score: 2,
					matched: ['bpm']
				}
			]
		}
	}
};

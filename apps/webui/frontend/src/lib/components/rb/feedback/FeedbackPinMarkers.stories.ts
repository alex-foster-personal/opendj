import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { ResolvedPinPosition } from '$lib/rb/feedback-pin-position';
import type { FeedbackPin } from '$lib/rb/feedback-store.svelte';

import FeedbackPinMarkers from './FeedbackPinMarkers.svelte';

/**
 * The review comment-pin marker layer. Pure props: every pin is drawn
 * `position: fixed` at its stored percentages (or a resolved position), so
 * the markers float over the story canvas. Clicking one only calls `onopen`
 * (recorded in the Actions panel); no feedback endpoint is read or written.
 * Pin text is invented. Hover a marker for its title.
 */
const pin = (id: string, over: Partial<FeedbackPin> = {}): FeedbackPin => ({
	id,
	text: 'Waveform flickers when the deck loads',
	page: '/performance',
	anchor: null,
	x_pct: 30,
	y_pct: 35,
	created_at: '2026-10-12T21:30:00.000000Z',
	build: { git_sha: '0123abc' },
	author: 'operator',
	status: 'open',
	...over
});

const meta = {
	title: 'Components/Feedback/FeedbackPinMarkers',
	component: FeedbackPinMarkers,
	tags: ['autodocs'],
	argTypes: { optionKeyHeld: { control: 'boolean' } },
	args: {
		pins: [pin('p1')],
		seen: {},
		pathname: '/performance',
		optionKeyHeld: false,
		onopen: fn()
	}
} satisfies Meta<typeof FeedbackPinMarkers>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A single open operator pin. */
export const OpenPin: Story = {};

/** One pin per lifecycle state, left to right: open, issued, blocked, fixed, merged. */
export const LifecycleStates: Story = {
	args: {
		pins: [
			pin('s1', { x_pct: 10, y_pct: 30, status: 'open', text: 'Open pin' }),
			pin('s2', { x_pct: 25, y_pct: 30, status: 'issued', text: 'Issued pin' }),
			pin('s3', { x_pct: 40, y_pct: 30, status: 'blocked', text: 'Blocked pin' }),
			pin('s4', { x_pct: 55, y_pct: 30, status: 'fixed', text: 'Fixed pin' }),
			pin('s5', { x_pct: 70, y_pct: 30, status: 'merged', text: 'Merged pin' })
		]
	}
};

/** Agent-authored pin wears the robot mark. */
export const AgentPin: Story = {
	args: { pins: [pin('a1', { author: 'agent', text: 'Agent note: checked the grid offset' })] }
};

/** Half-fixed: an open pin whose agent note starts "PARTIAL:" and cites a follow-on issue. */
export const PartialPin: Story = {
	args: {
		pins: [pin('h1', { agent_note: 'PARTIAL: label fixed, spacing still open in #4321' })]
	}
};

/** Agent touched the pin after the viewer last saw it: the unread dot shows. */
export const Unread: Story = {
	args: {
		pins: [pin('u1', { updated_at: '2026-10-13T08:00:00.000000Z', status: 'issued' })],
		seen: { u1: '2026-10-12T21:30:00.000000Z' }
	}
};

/** Harvested pin renders collapsed (small and dim). */
export const Harvested: Story = {
	args: { pins: [pin('v1', { status: 'harvested', harvested_at: '2026-10-12T23:00:00.000000Z' })] }
};

/** Option held: badges appear. This pin is on another page, regressed after its fix, and harvested. */
export const WithBadges: Story = {
	args: {
		optionKeyHeld: true,
		pins: [
			pin('b1', {
				page: '/library',
				status: 'harvested',
				fixed_at: '2026-10-11T10:00:00.000000Z',
				fixed_in_sha: 'abc1234',
				updated_at: '2026-10-12T10:00:00.000000Z'
			})
		]
	}
};

/** The pin's anchor moved, so it draws at a fallback position with a dashed ring. */
export const FallbackPosition: Story = {
	args: {
		pins: [pin('f1', { anchor: '#old-transport-button' })],
		positions: new Map<string, ResolvedPinPosition>([
			[
				'f1',
				{
					tier: 'viewport',
					fallback: true,
					via: 'saved screen position',
					x: 400,
					y: 260,
					x_pct: 30,
					y_pct: 35
				}
			]
		])
	}
};

/** No pins: renders nothing. */
export const NoPins: Story = {
	tags: ['renders-nothing'],
	args: { pins: [] }
};

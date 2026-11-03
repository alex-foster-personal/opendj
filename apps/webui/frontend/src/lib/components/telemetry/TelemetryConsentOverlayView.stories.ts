import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import TelemetryConsentOverlayView from './TelemetryConsentOverlayView.svelte';

/**
 * The first-launch diagnostics consent (OBS-05). The container follows the
 * consent state and records the answer; both buttons raise `onAnswer`. The
 * terms version is an invented sample.
 */
const meta = {
	title: 'Components/Telemetry/TelemetryConsentOverlayView',
	component: TelemetryConsentOverlayView,
	tags: ['autodocs'],
	parameters: { layout: 'fullscreen' },
	args: {
		open: true,
		busy: false,
		failure: null,
		termsVersion: '2026-09-01',
		replayOffered: true,
		onAnswer: fn()
	}
} satisfies Meta<typeof TelemetryConsentOverlayView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** First launch with session replay offered. */
export const WithReplay: Story = {};

/** A build with no replay loader: the replay bullet is gone. */
export const NoReplay: Story = { args: { replayOffered: false } };

/** The answer is being recorded: both buttons disabled. */
export const Saving: Story = { args: { busy: true } };

/** Recording the answer failed. */
export const SaveFailed: Story = { args: { failure: 'engine did not answer within 5 s' } };

/** Already answered: nothing renders. */
export const Closed: Story = { tags: ['renders-nothing'], args: { open: false } };

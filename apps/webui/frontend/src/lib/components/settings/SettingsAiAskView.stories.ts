import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import SettingsAiAskView from './SettingsAiAskView.svelte';

/**
 * The AI block at the foot of the settings results: AI search status, the
 * ask button, the outcome line and the Apply / Reject confirm.
 */
const meta = {
	title: 'Components/Settings/SettingsAiAskView',
	component: SettingsAiAskView,
	tags: ['autodocs'],
	args: {
		aiPending: false,
		aiError: null,
		applyBusy: false,
		query: 'make it darker',
		applyMsg: null,
		applyOk: null,
		pendingProposal: null,
		askAiApply: fn(),
		confirmProposal: fn()
	}
} satisfies Meta<typeof SettingsAiAskView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Ready to ask: a query of two or more characters enables the button. */
export const Ready: Story = {};

/** A query too short to ask about: the button is disabled. */
export const QueryTooShort: Story = { args: { query: 'd' } };

/** AI matching is running. */
export const Matching: Story = { args: { aiPending: true } };

/** AI search failed; the failure is in the hover title. */
export const AiUnavailable: Story = { args: { aiError: 'fast model not configured' } };

/** Waiting on the model's proposal. */
export const Asking: Story = { args: { applyBusy: true } };

/** A proposal is waiting for Apply or Reject. */
export const Proposed: Story = {
	args: {
		applyMsg: 'Proposed: theme = dark (you asked for a darker look)',
		pendingProposal: { key: 'theme', value: 'dark', rationale: 'you asked for a darker look' }
	}
};

/** The change was applied. */
export const Applied: Story = { args: { applyMsg: 'Applied: theme = dark', applyOk: true } };

/** The model proposed nothing allowlisted. */
export const Refused: Story = {
	args: { applyMsg: 'No allowlisted setting matches that request.', applyOk: false }
};

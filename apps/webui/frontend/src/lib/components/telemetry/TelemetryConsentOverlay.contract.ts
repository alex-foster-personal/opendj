import { expect, fn, userEvent, waitFor, within } from 'storybook/test';

import {
	setConsentDialogOpen,
	setCurrentConsent,
	type ConsentOut
} from '$lib/telemetry-consent-state';

/**
 * TelemetryConsentOverlay UI contract, shared by the Svelte story
 * (TelemetryConsentOverlay.stories.ts) and its React twin
 * (TelemetryConsentOverlay.stories.tsx). See src/react/README.md.
 *
 * The overlay reads the framework-free module state in
 * telemetry-consent-state.ts, not props, so each story sets that state in
 * `beforeEach` (before the component mounts, exactly as the consent module
 * does in the app) and its cleanup closes the dialog for the next story.
 */

type Args = { answer: (decision: 'accepted' | 'declined') => Promise<unknown> };
type Ctx = { canvasElement: HTMLElement; args: Args };

const consent = (over: Partial<ConsentOut>): ConsentOut => ({
	consent_required: true,
	decided_at: null,
	decision: 'undecided',
	environment: 'test',
	release: 'test-build',
	replay_loader_url: 'https://example.invalid/replay.js',
	replay_on_error_sample_rate: 1,
	replay_session_sample_rate: 0,
	telemetry_active: false,
	terms_current_version: '2026-09-01',
	terms_version: null,
	...over
});

function openWith(state: ConsentOut): () => () => void {
	return () => {
		setCurrentConsent(state);
		setConsentDialogOpen(true);
		return () => {
			setConsentDialogOpen(false);
			setCurrentConsent(null);
		};
	};
}

const items = (root: HTMLElement): string[] =>
	Array.from(root.ownerDocument.querySelectorAll('.tc-list li strong')).map((s) => s.textContent ?? '');

export const contracts = {
	/** Replay offered: three bullets, the terms version in the summary, "I agree" answers accepted. */
	Accept: {
		args: { answer: fn(async () => undefined) } satisfies Args,
		beforeEach: openWith(consent({})),
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const dialog = canvas.getByRole('dialog', { name: 'Help us test Open DJ' });
			await expect(dialog).toHaveAttribute('aria-modal', 'true');
			await expect(dialog).toHaveAttribute('data-testid', 'telemetry-consent-dialog');
			await expect(items(canvasElement)).toEqual(['Crash and error reports:', 'Session replays:', 'Never:']);
			await expect(canvas.getByText('Full terms (version 2026-09-01)')).toBeVisible();
			await expect(canvas.queryByRole('alert')).toBeNull();
			const accept = canvas.getByRole('button', { name: 'I agree' });
			await expect(accept).toHaveClass('tc-btn', 'tc-accept');
			await expect(accept).toHaveAttribute('data-testid', 'telemetry-consent-accept');
			await userEvent.click(accept);
			await expect(args.answer).toHaveBeenCalledWith('accepted');
			await waitFor(() => expect(accept).toBeEnabled());
		}
	},
	/** No replay loader: the session-replay bullet is gone, "Not now" answers declined. */
	DeclineNoReplay: {
		args: { answer: fn(async () => undefined) } satisfies Args,
		beforeEach: openWith(consent({ replay_loader_url: null, terms_current_version: '7' })),
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(items(canvasElement)).toEqual(['Crash and error reports:', 'Never:']);
			await expect(canvas.getByText('Full terms (version 7)')).toBeVisible();
			const decline = canvas.getByRole('button', { name: 'Not now' });
			await expect(decline).toHaveAttribute('data-testid', 'telemetry-consent-decline');
			await userEvent.click(decline);
			await expect(args.answer).toHaveBeenCalledWith('declined');
			await expect(args.answer).toHaveBeenCalledTimes(1);
		}
	},
	/** A failed save shows the reason in an alert and re-enables both buttons. */
	SaveFails: {
		args: {
			answer: fn(async () => {
				throw new Error('engine unreachable');
			})
		} satisfies Args,
		beforeEach: openWith(consent({})),
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			await userEvent.click(canvas.getByRole('button', { name: 'I agree' }));
			await expect(args.answer).toHaveBeenCalledWith('accepted');
			const alert = await canvas.findByRole('alert');
			await expect(alert).toHaveClass('tc-failure');
			await expect(alert).toHaveTextContent('Could not save your answer: engine unreachable');
			await waitFor(() => expect(canvas.getByRole('button', { name: 'Not now' })).toBeEnabled());
			await expect(canvas.getByRole('button', { name: 'I agree' })).toBeEnabled();
		}
	},
	/** Dialog state closed: nothing renders. */
	Closed: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { answer: fn(async () => undefined) } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(canvasElement.querySelector('.tc-backdrop')).toBeNull();
			await expect(args.answer).not.toHaveBeenCalled();
		}
	}
};

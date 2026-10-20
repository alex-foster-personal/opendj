import { useState, useSyncExternalStore } from 'react';

import {
	currentConsent,
	isConsentDialogOpen,
	subscribeConsentDialogOpen
} from '$lib/telemetry-consent-state';

import './TelemetryConsentOverlay.react.css';

// React twin of TelemetryConsentOverlay.svelte: same DOM, classes, ARIA and
// behavior, held to the same contract (TelemetryConsentOverlay.contract.ts).
// First-launch diagnostics consent (OBS-05); the text is the tester-facing
// rendering of docs/legal/test-user-terms.md, so keep all three in step.

export type TelemetryConsentOverlayProps = {
	/** Records the answer with the engine; `answerConsent` in production. */
	answer: (decision: 'accepted' | 'declined') => Promise<unknown>;
};

export default function TelemetryConsentOverlay({ answer: recordAnswer }: TelemetryConsentOverlayProps) {
	const open = useSyncExternalStore(subscribeConsentDialogOpen, isConsentDialogOpen, isConsentDialogOpen);
	const [busy, setBusy] = useState(false);
	const [failure, setFailure] = useState<string | null>(null);

	if (!open) return null;

	const termsVersion = currentConsent()?.terms_current_version ?? '';
	const replayOffered = currentConsent()?.replay_loader_url !== null;

	async function answer(decision: 'accepted' | 'declined'): Promise<void> {
		setBusy(true);
		setFailure(null);
		try {
			await recordAnswer(decision);
		} catch (error) {
			setFailure(error instanceof Error ? error.message : String(error));
		} finally {
			setBusy(false);
		}
	}

	return (
		<div className="tc-backdrop" role="presentation">
			<div
				className="tc-panel"
				role="dialog"
				aria-modal="true"
				aria-labelledby="tc-title"
				data-testid="telemetry-consent-dialog"
				tabIndex={-1}
			>
				<h2 id="tc-title" className="tc-title">
					Help us test Open DJ
				</h2>
				<p className="tc-lead">
					This is a pre-release build. If you agree, the app sends diagnostics to Sentry (EU region) so we can see
					what broke without asking you for log files. If you decline, the app works exactly the same and sends
					nothing.
				</p>
				<ul className="tc-list">
					<li>
						<strong>Crash and error reports:</strong> error type and message, code location, the app route, the
						build id, and machine facts (OS, browser engine, sample rate).
					</li>
					{replayOffered && (
						<li>
							<strong>Session replays:</strong> a recording of the app's screen with all text masked (track names
							become blocks), media blocked (no album art) and no waveform capture, plus where you clicked and any
							errors at the time.
						</li>
					)}
					<li>
						<strong>Never:</strong> audio, track titles or playlist names in error reports (fields are
						allowlisted), file paths (reduced to their extension), your identity or IP, or anything at all while a
						deck is playing.
					</li>
				</ul>
				<details className="tc-details">
					<summary>Full terms (version {termsVersion})</summary>
					<p>
						Reports go to this project's Sentry organization in the EU. Sentry keeps them for a limited period, on
						the order of weeks, and nothing is shared onward.
					</p>
					<p>
						Change your mind later: create an empty file named <code>telemetry-opt-out</code> in the Open DJ data
						folder (under Library/Application Support) and restart, or delete{' '}
						<code>telemetry-consent.json</code> there to be asked again.
					</p>
				</details>
				{failure !== null && (
					<p className="tc-failure" role="alert">
						Could not save your answer: {failure}
					</p>
				)}
				<div className="tc-actions">
					<button
						type="button"
						className="tc-btn"
						disabled={busy}
						data-testid="telemetry-consent-decline"
						onClick={() => void answer('declined')}
					>
						Not now
					</button>
					<button
						type="button"
						className="tc-btn tc-accept"
						disabled={busy}
						data-testid="telemetry-consent-accept"
						onClick={() => void answer('accepted')}
					>
						I agree
					</button>
				</div>
			</div>
		</div>
	);
}

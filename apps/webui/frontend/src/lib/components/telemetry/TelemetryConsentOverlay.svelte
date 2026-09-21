<script lang="ts">
	/**
	 * First-launch diagnostics consent (OBS-05). The text here is the
	 * tester-facing rendering of docs/legal/test-user-terms.md; keep the two
	 * in step and bump TERMS_VERSION in apps/shared/telemetry/consent.py when
	 * either changes materially.
	 */
	import {
		currentConsent,
		isConsentDialogOpen,
		subscribeConsentDialogOpen
	} from '$lib/telemetry-consent-state';

	interface Props {
		/** Records the answer with the engine; `answerConsent` in production.
		 * A prop rather than an import so this component never imports the
		 * module that mounts it (that pair would be an import cycle). */
		answer: (decision: 'accepted' | 'declined') => Promise<unknown>;
	}

	let { answer: recordAnswer }: Props = $props();

	let open = $state(false);
	let busy = $state(false);
	let failure = $state<string | null>(null);

	$effect(() => {
		const sync = (): void => {
			open = isConsentDialogOpen();
		};
		sync();
		return subscribeConsentDialogOpen(sync);
	});

	const termsVersion = $derived(currentConsent()?.terms_current_version ?? '');
	const replayOffered = $derived(currentConsent()?.replay_loader_url !== null);

	async function answer(decision: 'accepted' | 'declined'): Promise<void> {
		busy = true;
		failure = null;
		try {
			await recordAnswer(decision);
		} catch (error) {
			failure = error instanceof Error ? error.message : String(error);
		} finally {
			busy = false;
		}
	}
</script>

{#if open}
	<div class="tc-backdrop" role="presentation">
		<div
			class="tc-panel"
			role="dialog"
			aria-modal="true"
			aria-labelledby="tc-title"
			data-testid="telemetry-consent-dialog"
			tabindex="-1"
		>
			<h2 id="tc-title" class="tc-title">Help us test Open DJ</h2>
			<p class="tc-lead">
				This is a pre-release build. If you agree, the app sends diagnostics to Sentry (EU
				region) so we can see what broke without asking you for log files. If you decline,
				the app works exactly the same and sends nothing.
			</p>
			<ul class="tc-list">
				<li>
					<strong>Crash and error reports:</strong> error type and message, code location, the
					app route, the build id, and machine facts (OS, browser engine, sample rate).
				</li>
				{#if replayOffered}
					<li>
						<strong>Session replays:</strong> a recording of the app's screen with all text
						masked (track names become blocks), media blocked (no album art) and no waveform
						capture, plus where you clicked and any errors at the time.
					</li>
				{/if}
				<li>
					<strong>Never:</strong> audio, track titles or playlist names in error reports (fields
					are allowlisted), file paths (reduced to their extension), your identity or IP, or
					anything at all while a deck is playing.
				</li>
			</ul>
			<details class="tc-details">
				<summary>Full terms (version {termsVersion})</summary>
				<p>
					Reports go to this project's Sentry organization in the EU. Sentry keeps them for a
					limited period, on the order of weeks, and nothing is shared onward.
				</p>
				<p>
					Change your mind later: create an empty file named <code>telemetry-opt-out</code> in
					the Open DJ data folder (under Library/Application Support) and restart, or delete
					<code>telemetry-consent.json</code> there to be asked again.
				</p>
			</details>
			{#if failure !== null}
				<p class="tc-failure" role="alert">Could not save your answer: {failure}</p>
			{/if}
			<div class="tc-actions">
				<button
					type="button"
					class="tc-btn"
					disabled={busy}
					data-testid="telemetry-consent-decline"
					onclick={() => void answer('declined')}
				>
					Not now
				</button>
				<button
					type="button"
					class="tc-btn tc-accept"
					disabled={busy}
					data-testid="telemetry-consent-accept"
					onclick={() => void answer('accepted')}
				>
					I agree
				</button>
			</div>
		</div>
	</div>
{/if}

<style>
	.tc-backdrop {
		position: fixed;
		inset: 0;
		z-index: 9990;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgb(0 0 0 / 0.55);
	}
	.tc-panel {
		width: min(34rem, calc(100vw - 2rem));
		max-height: calc(100vh - 2rem);
		overflow: auto;
		padding: 1.25rem 1.5rem;
		border: 1px solid var(--border);
		border-radius: 0.5rem;
		background: var(--surface);
		color: var(--text);
		box-shadow: 0 0.5rem 2rem rgb(0 0 0 / 0.35);
	}
	.tc-title {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 600;
	}
	.tc-lead {
		margin: 0.75rem 0;
		font-size: 0.9rem;
	}
	.tc-list {
		margin: 0 0 0.75rem;
		padding-left: 1.1rem;
		font-size: 0.85rem;
		line-height: 1.4;
	}
	.tc-list li + li {
		margin-top: 0.4rem;
	}
	.tc-details {
		margin: 0 0 1rem;
		font-size: 0.8rem;
		color: var(--muted, #888);
	}
	.tc-details summary {
		cursor: pointer;
		color: var(--text);
	}
	.tc-failure {
		margin: 0 0 0.75rem;
		font-size: 0.8rem;
		color: var(--danger, #e5484d);
	}
	.tc-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.5rem;
	}
	.tc-btn {
		padding: 0.4rem 0.9rem;
		border: 1px solid var(--border);
		border-radius: 0.25rem;
		background: var(--surface-2, var(--surface));
		color: inherit;
		font: inherit;
		cursor: pointer;
	}
	.tc-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.tc-accept {
		border-color: var(--accent);
		background: var(--accent);
		color: var(--accent-contrast, #fff);
	}
</style>

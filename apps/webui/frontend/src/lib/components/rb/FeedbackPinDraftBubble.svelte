<script lang="ts">
	/**
	 * The placed-but-unsaved pin bubble: text box, follow-on hint, anchor
	 * hint, Save/Cancel. Split out of FeedbackWidget.svelte (Thu 3 Sep 2026,
	 * pin review v2) to bring that file back under the 600-line file-size
	 * gate, the same way FeedbackPinCard.svelte and
	 * feedback/FeedbackPinMarkers.svelte were split out of it before (#858,
	 * issue #928).
	 */
	import { tick } from 'svelte';
	import { pinStyle, type PinDraft } from '$lib/rb/feedback';
	import { addPin, submitFollowOn } from '$lib/rb/feedback-store.svelte';
	import { readShellBuild } from '$lib/rb/build-identity';

	let {
		pinDraft = $bindable()
	}: {
		pinDraft:
			| (PinDraft & {
					viewport: { width: number; height: number };
					followOn: { parentId: string; label: string } | null;
			  })
			| null;
	} = $props();

	let pinDraftTextarea: HTMLTextAreaElement | null = $state(null);
	// r3919761150: guards against Cmd/Ctrl+Enter key-repeat re-entering
	// savePinDraft once per keydown while the first POST is still in flight.
	let savingPinDraft = $state(false);

	/** Imperative twin, called by the parent right after it arms a fresh
	 * draft (startFollowOn / handlePlacementClick) - never from a restore on
	 * mount, which must not steal focus. */
	export async function focusPinDraftTextarea(): Promise<void> {
		await tick();
		if (pinDraftTextarea === null) {
			throw new Error('pin draft textarea did not render before focus');
		}
		pinDraftTextarea.focus();
	}

	function pinUiKind(): 'chrome-loop' | 'packaged-app' {
		return readShellBuild().kind === 'absent' ? 'chrome-loop' : 'packaged-app';
	}

	async function savePinDraft(): Promise<void> {
		if (pinDraft === null || savingPinDraft) return;
		savingPinDraft = true;
		// r3919867508 / r3920118985: identity + text-drift guards a cancel+
		// re-place swap, or an edit landing mid-flight, from clearing the
		// wrong draft.
		const submitted = pinDraft;
		const submittedText = submitted.text.trim();
		try {
			if (submitted.followOn !== null) {
				// Always through the dedicated endpoint: the parent reference is
				// server-generated from submitted.followOn.parentId, independent of
				// whatever is (or is not) left in `text`.
				const saved = await submitFollowOn(submitted.followOn.parentId, submittedText);
				if (saved && pinDraft === submitted && pinDraft.text.trim() === submittedText)
					pinDraft = null;
				return;
			}
			if (submittedText === '') return;
			const saved = await addPin({
				x_pct: submitted.point.x_pct,
				y_pct: submitted.point.y_pct,
				anchor: submitted.anchor,
				page: submitted.page,
				text: submittedText,
				ui: pinUiKind(),
				viewport_width: submitted.viewport.width,
				viewport_height: submitted.viewport.height
			});
			if (saved && pinDraft === submitted && pinDraft.text.trim() === submittedText)
				pinDraft = null;
		} finally {
			savingPinDraft = false;
		}
	}
</script>

<!-- pin text bubble -->
{#if pinDraft !== null}
	<div class="fb-bubble" style={pinStyle(pinDraft.point)} role="dialog" aria-label="New comment pin">
		{#if pinDraft.followOn !== null}
			<p class="fb-hint" title="Sent through the dedicated follow-on endpoint; the reference below is generated server-side">{pinDraft.followOn.label}</p>
		{/if}
		<!-- Cmd/Ctrl+Enter submits: the one shortcut that IS allowed to work
		     inside a text field (pin 919d65b350b1). -->
		<textarea
			class="fb-bubble-text"
			rows="3"
			placeholder="What is wrong / right here? (Cmd+Enter saves)"
			bind:this={pinDraftTextarea}
			bind:value={pinDraft.text}
			onkeydown={(e) => {
				if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
					e.preventDefault();
					void savePinDraft();
				}
			}}
		></textarea>
		{#if pinDraft.anchor !== null}
			<p class="fb-hint" title="Best-effort nearest stable element under the click">near {pinDraft.anchor}</p>
		{/if}
		<div class="fb-row-btns">
			<button
				type="button"
				class="fb-mini"
				onclick={savePinDraft}
				disabled={savingPinDraft || (pinDraft.followOn === null && pinDraft.text.trim() === '')}
			>
				Save pin
			</button>
			<button type="button" class="fb-mini" onclick={() => (pinDraft = null)}>Cancel</button>
		</div>
	</div>
{/if}

<style>
	.fb-bubble {
		position: fixed;
		z-index: 310;
		width: 200px;
		padding: 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}

	.fb-bubble-text {
		width: 100%;
		box-sizing: border-box;
		background: var(--rb-inset);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font-family: var(--rb-font);
		font-size: 10px;
		padding: 3px 4px;
	}

	:global(.fb-row-btns) {
		display: flex;
		gap: 4px;
		margin-top: 4px;
	}

	:global(.fb-mini) {
		background: var(--rb-panel-raised);
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-family: var(--rb-font);
		font-size: 9px;
		padding: 2px 6px;
		line-height: 1.2;
		cursor: pointer;
	}
	:global(.fb-mini:hover:not(:disabled)) {
		color: var(--rb-text);
	}
	:global(.fb-close) {
		margin-left: auto;
		min-width: 20px;
	}

	:global(.fb-hint) {
		margin: 2px 0 0;
		color: var(--rb-text-dim);
	}
</style>

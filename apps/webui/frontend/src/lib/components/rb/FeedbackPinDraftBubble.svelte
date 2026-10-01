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
	import { pinBodyPos, pinBodyStyle, type PinDraft } from '$lib/rb/feedback';
	import {
		addPin,
		addPinWithAttachment,
		submitFollowOn,
		submitFollowOnWithAttachment
	} from '$lib/rb/feedback-store.svelte';
	import { readShellBuild } from '$lib/rb/build-identity';
	import { buildPinUiConfig } from '$lib/rb/feedback-pin-ui-config';
	import { uiPrefs } from '$lib/rb/prefs.svelte';
	import { rustMode } from '$lib/audio-engine/rust-mode.svelte';
	import { attachmentSizeRefusal, pastedImageFile } from '$lib/rb/feedback-pin-attachment';
	import type { ClientErrorContext } from '$lib/client-error-reporting';
	import { OVERLAY_Z_INDEX } from '$lib/overlays/overlay-stack';

	/** Signature of `$lib/stores.svelte`'s `pushToast`, taken as a prop
	 * instead of imported directly: the parent (FeedbackWidget.svelte)
	 * already imports the real store module, so threading its function
	 * through here does not add a second importer to the fan-in ratchet
	 * (frontend.max_fan_in) for no behavior change. */
	type PushToast = (
		message: string,
		kind?: 'info' | 'error',
		dismissMs?: number,
		cause?: unknown,
		context?: ClientErrorContext,
		groupKey?: string
	) => void;

	let {
		pinDraft = $bindable(),
		pushToast
	}: {
		pinDraft:
			| (PinDraft & {
					viewport: { width: number; height: number };
					followOn: { parentId: string; label: string } | null;
			  })
			| null;
		pushToast: PushToast;
	} = $props();

	let pinDraftTextarea: HTMLTextAreaElement | null = $state(null);
	let bubbleElement: HTMLDivElement | null = $state(null);
	let measuredPos: { x: number; y: number } | null = $state(null);
	// r3919761150: guards against Cmd/Ctrl+Enter key-repeat re-entering
	// savePinDraft once per keydown while the first POST is still in flight.
	let savingPinDraft = $state(false);

	/** A screenshot pasted into the textarea, staged in memory until Save -
	 * the pin needs a real id (issue #1333) before the attachment endpoint
	 * has anything to attach it to. Not persisted with the rest of the draft:
	 * a File cannot survive JSON.stringify, so a refresh mid-paste drops the
	 * pending image exactly the way it would drop an unsaved clipboard paste
	 * anywhere else - the typed text still persists as before. */
	let pendingAttachment: File | null = $state(null);
	/** Explicit refusal text for a paste rejected by size/type, or an upload
	 * that failed at Save - never a silent drop (issue #1333 acceptance). */
	let attachmentError: string | null = $state(null);

	const bodyStyle = $derived.by(() => {
		if (pinDraft === null) return '';
		const pos = measuredPos;
		const place = pos !== null ? `left:${pos.x}px;top:${pos.y}px` : pinBodyStyle(pinDraft.point);
		return `${place};z-index:${OVERLAY_Z_INDEX.feedbackPinBubble}`;
	});

	function _reposition(): void {
		if (bubbleElement === null || pinDraft === null) return;
		const rect = bubbleElement.getBoundingClientRect();
		measuredPos = pinBodyPos(
			pinDraft.point,
			{ w: rect.width, h: rect.height },
			{ w: window.innerWidth, h: window.innerHeight }
		);
	}

	$effect(() => {
		if (pinDraft === null) {
			pendingAttachment = null;
			attachmentError = null;
		}
	});

	$effect(() => {
		void pinDraft;
		void pinDraft?.text;
		void pendingAttachment;
		void attachmentError;
		measuredPos = null;
		void tick().then(() => _reposition());
	});

	function handlePaste(e: ClipboardEvent): void {
		const outcome = pastedImageFile(e.clipboardData?.items ? [...e.clipboardData.items] : null);
		if (outcome.kind === 'none') return;
		// Otherwise some browsers also paste a text/plain fallback (a file
		// path or nothing useful) into the textarea alongside the image.
		e.preventDefault();
		switch (outcome.kind) {
			case 'rejected':
				// FB-11: a disallowed image type is refused with an explicit
				// message, never silently dropped (PR #1425 P2).
				attachmentError = `screenshot type "${outcome.type}" is not supported (use PNG, JPEG, GIF, or WEBP)`;
				break;
			case 'file': {
				const refusal = attachmentSizeRefusal(outcome.file);
				if (refusal !== null) {
					attachmentError = refusal;
					break;
				}
				pendingAttachment = outcome.file;
				attachmentError = null;
				break;
			}
			default: {
				const _exhaustive: never = outcome;
				throw new Error(`Unhandled PastedImageOutcome: ${JSON.stringify(_exhaustive)}`);
			}
		}
	}

	function removePendingAttachment(): void {
		pendingAttachment = null;
		attachmentError = null;
	}

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
				const followOnAttachment = pendingAttachment;
				let saved: boolean;
				if (followOnAttachment === null) {
					saved = await submitFollowOn(submitted.followOn.parentId, submittedText);
				} else {
					// A screenshot staged in a follow-on draft must upload the same
					// way an initial pin's does (PR #1425 P1 round 3): the earlier
					// version returned here without ever reaching the attachment
					// logic below, silently dropping the staged File.
					const result = await submitFollowOnWithAttachment(
						submitted.followOn.parentId,
						submittedText,
						followOnAttachment
					);
					switch (result.kind) {
						case 'created':
							saved = true;
							pendingAttachment = null;
							break;
						case 'created-attachment-failed':
							saved = true;
							pushToast(
								`follow-on saved, but the screenshot was not: ${result.reason}`,
								'error',
								undefined,
								undefined,
								{ source: 'feedback-pin-attachment' }
							);
							break;
						case 'create-failed':
							saved = false;
							pushToast(`follow-on was not saved: ${result.reason}`, 'error', undefined, undefined, {
								source: 'feedback-pin-attachment'
							});
							break;
						default: {
							const _exhaustive: never = result;
							throw new Error(`Unhandled AddPinWithAttachmentResult: ${JSON.stringify(_exhaustive)}`);
						}
					}
				}
				if (saved && pinDraft === submitted && pinDraft.text.trim() === submittedText)
					pinDraft = null;
				return;
			}
			if (submittedText === '') return;
			const pinBody = {
				x_pct: submitted.point.x_pct,
				y_pct: submitted.point.y_pct,
				anchor: submitted.anchor,
				page: submitted.page,
				text: submittedText,
				ui: pinUiKind(),
				viewport_width: submitted.viewport.width,
				viewport_height: submitted.viewport.height,
				author: 'operator',
				ui_config: buildPinUiConfig({
					pathname: submitted.page,
					prefs: uiPrefs,
					rustEngine: rustMode.enabled
				})
			} as const;
			const attachment = pendingAttachment;
			let saved: unknown;
			if (attachment === null) {
				saved = await addPin(pinBody);
			} else {
				const result = await addPinWithAttachment(pinBody, attachment);
				switch (result.kind) {
					case 'created':
						saved = true;
						pendingAttachment = null;
						break;
					case 'created-attachment-failed':
						// The pin itself was created (issue #1333: a refused
						// screenshot is not a reason to lose the typed text, and
						// retrying Save here would otherwise create a SECOND pin
						// for the same text) - only the attachment failed.
						saved = true;
						pushToast(
							`comment pin saved, but the screenshot was not: ${result.reason}`,
							'error',
							undefined,
							undefined,
							{ source: 'feedback-pin-attachment' }
						);
						break;
					case 'create-failed':
						// Nothing was created at all - keep the draft (and the
						// pending attachment) so the operator's typed comment is
						// never silently lost, and report the real failure
						// instead of a false "pin saved" toast (PR #1425 P1).
						saved = false;
						pushToast(`pin was not saved: ${result.reason}`, 'error', undefined, undefined, {
							source: 'feedback-pin-attachment'
						});
						break;
					default: {
						const _exhaustive: never = result;
						throw new Error(`Unhandled AddPinWithAttachmentResult: ${JSON.stringify(_exhaustive)}`);
					}
				}
			}
			if (saved && pinDraft === submitted && pinDraft.text.trim() === submittedText)
				pinDraft = null;
		} finally {
			savingPinDraft = false;
		}
	}
</script>

<svelte:window onresize={_reposition} />

<!-- pin text bubble -->
{#if pinDraft !== null}
	<div
		class="fb-bubble"
		style={bodyStyle}
		role="dialog"
		aria-label="New comment pin"
		bind:this={bubbleElement}
	>
		{#if pinDraft.followOn !== null}
			<p class="fb-hint" title="Sent through the dedicated follow-on endpoint; the reference below is generated server-side">{pinDraft.followOn.label}</p>
		{/if}
		<!-- Cmd/Ctrl+Enter submits: the one shortcut that IS allowed to work
		     inside a text field (pin 919d65b350b1). -->
		<textarea
			class="fb-bubble-text"
			rows="3"
			placeholder="What is wrong / right here? (Cmd+Enter saves, paste a screenshot)"
			bind:this={pinDraftTextarea}
			bind:value={pinDraft.text}
			onpaste={handlePaste}
			onkeydown={(e) => {
				if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
					e.preventDefault();
					void savePinDraft();
				}
			}}
		></textarea>
		{#if pendingAttachment !== null}
			<p class="fb-hint" title={`${pendingAttachment.type}, ${(pendingAttachment.size / 1024).toFixed(0)} KB - attached on Save`}>
				screenshot: {pendingAttachment.name || 'pasted image'}
				<button type="button" class="fb-mini" onclick={removePendingAttachment}>Remove</button>
			</p>
		{/if}
		{#if attachmentError !== null}
			<p class="fb-hint fb-attachment-error">{attachmentError}</p>
		{/if}
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
		width: 200px;
		max-height: 320px;
		overflow-y: auto;
		padding: 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
	}

	.fb-bubble-text {
		width: 100%;
		max-height: 100%;
		overflow-y: auto;
		overflow-wrap: anywhere;
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

	.fb-attachment-error {
		color: var(--rb-orange);
	}
</style>

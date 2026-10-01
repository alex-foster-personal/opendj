<script lang="ts">
	import { pinBodyPos, pinBodyStyle, pinIsDone, pinStatus } from '$lib/rb/feedback';
	import { blockedPinDetail, blockedPinRequest } from '$lib/rb/feedback';
	import { isPinRegressed } from '$lib/rb/feedback-pin-board';
	import { pinVisualState } from '$lib/rb/feedback-pin-partial';
	import { linkifyAgentNote } from '$lib/rb/feedback';
	import {
		clearPinReplyDraft,
		persistPinReplyDraft,
		readPinReplyDraft
	} from '$lib/rb/feedback-pin-reply-draft';
	import { pinThread } from '$lib/rb/feedback-pin-thread';
	import { API_BASE } from '$lib/api';
	import type { FeedbackPin } from '$lib/rb/feedback-store.svelte';
	import { OVERLAY_Z_INDEX } from '$lib/overlays/overlay-stack';

	let {
		pin,
		onclose,
		onarchive,
		onfollowon,
		onreply
	}: {
		pin: FeedbackPin;
		onclose: () => void;
		onarchive: () => void | Promise<void>;
		onfollowon: () => void;
		onreply: (text: string) => Promise<FeedbackPin | null>;
	} = $props();

	let pinBodyElement: HTMLDivElement | null = $state(null);
	let lightboxElement: HTMLDivElement | null = $state(null);
	let lightboxOpen = $state(false);
	let replyText = $state('');
	let replySaving = $state(false);

	/** Set once the card's REAL size is measured; null until then and again
	 * whenever a DIFFERENT pin's body reuses this same mounted instance
	 * (switching straight from one open marker to another skips the
	 * `{#if bodyPin !== null}` unmount in FeedbackWidget). `bodyStyle` below
	 * falls back to `pinBodyStyle`'s fixed guess for that gap, same as
	 * before this pin ever gets measured. */
	let measuredPos: { x: number; y: number } | null = $state(null);

	/** `pinBodyStyle`'s fixed 252x320 guess until measured for real - a
	 * `$derived` (not a stored initial value) so it re-reads `pin` on every
	 * prop change, not just at mount. */
	const bodyStyle = $derived.by(() => {
		const pos = measuredPos;
		const place = pos !== null ? `left:${pos.x}px;top:${pos.y}px` : pinBodyStyle(pin);
		return `${place};z-index:${OVERLAY_Z_INDEX.feedbackPinBubble}`;
	});

	const thread = $derived(pinThread(pin));

	/** FBSYNC-05: a pin synced from another machine carries its screenshot's
	 * metadata but not its bytes, so the image 404s here. Keyed on the
	 * attachment id so a card reused for another pin starts clean. */
	let unsyncedAttachmentId = $state<string | null>(null);

	/** Re-clamp against the card's REAL measured size, not the guess. Runs
	 * once pinBodyElement mounts, again whenever `pin` changes (a different
	 * marker opened into this same instance), and again on every resize,
	 * since a viewport resize can turn an on-screen position into an
	 * off-screen one. */
	function _reposition(): void {
		if (pinBodyElement === null) return;
		const rect = pinBodyElement.getBoundingClientRect();
		measuredPos = pinBodyPos(
			pin,
			{ w: rect.width, h: rect.height },
			{ w: window.innerWidth, h: window.innerHeight }
		);
	}

	$effect(() => {
		// Reset so a newly-swapped-in pin repaints from the guess for one
		// frame rather than briefly showing the PREVIOUS pin's measured spot.
		void pin;
		measuredPos = null;
		lightboxOpen = false;
		unsyncedAttachmentId = null;
		replyText = readPinReplyDraft(localStorage, pin.id);
		if (pinBodyElement !== null) _reposition();
	});

	function handleLightboxKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || !lightboxOpen) return;
		event.stopImmediatePropagation();
		lightboxOpen = false;
	}

	/** An outside pointer closes only this reopened card. The widget owns a
	 * separate new-pin draft, so it remains intact. Pointerdown precedes a
	 * marker click, letting another marker reopen its own card immediately. */
	function handleOutsidePinPointerDown(event: PointerEvent): void {
		const target = event.target;
		if (!(target instanceof Node)) return;
		if (pinBodyElement?.contains(target) || lightboxElement?.contains(target)) return;
		persistPinReplyDraft(localStorage, pin.id, replyText, () => {});
		onclose();
	}

	function handleReplyInput(): void {
		persistPinReplyDraft(localStorage, pin.id, replyText, () => {});
	}

	async function submitReply(): Promise<void> {
		const text = replyText.trim();
		if (text === '' || replySaving) return;
		replySaving = true;
		const updated = await onreply(text);
		replySaving = false;
		if (updated !== null) {
			replyText = '';
			clearPinReplyDraft(localStorage, pin.id, () => {});
		}
	}
</script>

<svelte:window
	onpointerdowncapture={handleOutsidePinPointerDown}
	onresize={_reposition}
	onkeydowncapture={handleLightboxKeydown}
/>

<div
	class="fb-pin-body"
	style={bodyStyle}
	role="dialog"
	aria-label="Comment pin"
	bind:this={pinBodyElement}
>
	<p class="fb-hint">
		{pinStatus(pin)}{pinVisualState(pin) === 'partial' ? ' (partial)' : ''} - {pin.created_at}
	</p>
	{#if pin.anchor}
		<p class="fb-hint" title="Original anchor captured at drop time">Anchor: {pin.anchor}</p>
	{/if}
	<dl class="fb-provenance" data-testid="fb-pin-provenance">
		<dt title="UTC timestamp when the pin was saved">Created (UTC)</dt>
		<dd>{pin.created_at}</dd>
		<dt title="Route path when the pin was dropped">Page</dt>
		<dd>{pin.page}</dd>
		{#if pin.build?.git_sha}
			<dt title="Git commit stamped at save time">Build git sha</dt>
			<dd>{pin.build.git_sha}</dd>
		{/if}
		{#if pin.environment}
			<dt title="UI surface kind at drop time">UI</dt>
			<dd>{pin.environment.ui}</dd>
			<dt title="Viewport width in CSS pixels at drop time">Viewport</dt>
			<dd>{pin.environment.viewport_width}×{pin.environment.viewport_height}</dd>
			<dt title="Daemon machine name published in settings/health">Machine</dt>
			<dd>{pin.environment.machine}</dd>
			<dt title="Release version separate from git sha">Release</dt>
			<dd>{pin.environment.release_version}</dd>
			{#if pin.environment.user_email}
				<dt title="Account signed in when the pin was dropped, stamped by the daemon">User</dt>
				<dd>{pin.environment.user_email}</dd>
			{/if}
			{#if pin.environment.ui_config}
				{@const cfg = pin.environment.ui_config}
				<dt title="App mode, audio engine and performance tier at drop time">Config</dt>
				<dd>{cfg.app_mode} / {cfg.engine_mode} / {cfg.perf_tier}</dd>
				<dt title="Feature switches that were on at drop time">Switches on</dt>
				<dd>
					{Object.entries(cfg.switches)
						.filter(([, on]) => on)
						.map(([name]) => name)
						.join(', ') || 'none'}
				</dd>
			{/if}
		{/if}
	</dl>
	{#if pin.fixed_in_sha}
		<p class="fb-hint" title="Build where this pin was marked fixed or merged">
			Fixed in: {pin.fixed_in_sha}
		</p>
	{/if}
	{#if isPinRegressed(pin)}
		<p class="fb-hint fb-regressed" title="New activity after the pin was marked fixed">
			Regressed: new reply after fix ({pin.fixed_in_sha})
		</p>
	{/if}
	{#each thread as turn (turn.id)}
		{#if turn.kind === 'opening'}
			<p class="fb-body-text">{turn.text}</p>
		{:else if pinStatus(pin) === 'blocked' && turn.author === 'agent' && turn.text === pin.agent_note}
			<p class="fb-blocked-request">{blockedPinRequest(turn.text)}</p>
			{#if blockedPinDetail(turn.text)}
				<p class="fb-note" title="Supporting detail for the blocked request">
					{#each linkifyAgentNote(blockedPinDetail(turn.text)) as segment, i (i)}
						{#if segment.type === 'link'}
							<a href={segment.value} target="_blank" rel="noreferrer noopener" onclick={(e) => e.stopPropagation()}>{segment.value}</a>
						{:else}{segment.value}{/if}
					{/each}
				</p>
			{/if}
		{:else}
			<p class="fb-note" title={turn.author === 'operator' ? 'Your follow-up' : 'What an agent did about this pin'}>
				{#if turn.author === 'operator'}
					<span class="fb-reply-label">You: </span>
				{/if}
				{#each linkifyAgentNote(turn.text) as segment, i (i)}
					{#if segment.type === 'link'}
						<a
							href={segment.value}
							target="_blank"
							rel="noreferrer noopener"
							onclick={(e) => e.stopPropagation()}>{segment.value}</a
						>
					{:else}{segment.value}{/if}
				{/each}
			</p>
		{/if}
	{/each}
	{#if pin.attachment && unsyncedAttachmentId === pin.attachment.id}
		<p class="fb-hint fb-attachment-unsynced" data-testid="fb-attachment-unsynced">
			Screenshot not on this machine: pin sync carries attachment details, not the image
			yet.
		</p>
	{:else if pin.attachment}
		{@const attachmentId = pin.attachment.id}
		<button
			type="button"
			class="fb-attachment-btn"
			title={`${pin.attachment.content_type}, ${(pin.attachment.size_bytes / 1024).toFixed(0)} KB - click to enlarge`}
			onclick={(event) => {
				event.stopPropagation();
				lightboxOpen = true;
			}}
		>
			<img
				class="fb-attachment-img"
				src={`${API_BASE}${pin.attachment.url}`}
				alt="Pasted screenshot"
				onerror={() => (unsyncedAttachmentId = attachmentId)}
			/>
		</button>
	{/if}
	{#if pin.issue_url}
		<a
			class="fb-issue-link"
			href={pin.issue_url}
			target="_blank"
			rel="noreferrer noopener"
			title="Opens in the default browser">{pin.issue_url}</a
		>
	{/if}
	<textarea
		class="fb-reply"
		rows="2"
		aria-label="Follow-up comment"
		placeholder="Add a follow-up on this pin"
		bind:value={replyText}
		oninput={handleReplyInput}
		onkeydown={(e) => {
			if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
				e.preventDefault();
				void submitReply();
			}
		}}
	></textarea>
	<div class="fb-row-btns">
		<button
			type="button"
			class="fb-mini"
			title="Add a follow-up on this pin"
			aria-label="Add follow-up comment"
			disabled={replySaving || replyText.trim() === ''}
			onclick={() => void submitReply()}>Reply</button
		>
		{#if pinIsDone(pin)}
			<button
				type="button"
				class="fb-mini"
				title="Move this pin into the archive file, with its history"
				onclick={() => void onarchive()}>Archive</button
			>
			<button
				type="button"
				class="fb-mini"
				title="Open a new pin here, referencing this one"
				onclick={onfollowon}>Follow-on</button
			>
		{/if}
		<button
			type="button"
			class="fb-mini fb-close"
			aria-label="Close comment pin"
			title="Close comment pin"
			onclick={onclose}>×</button
		>
	</div>
</div>

{#if lightboxOpen && pin.attachment}
	<div
		class="fb-lightbox"
		style="z-index: {OVERLAY_Z_INDEX.feedbackPinBubble + 5}"
		role="dialog"
		aria-modal="true"
		aria-label="Screenshot"
		bind:this={lightboxElement}
	>
		<button
			type="button"
			class="fb-lightbox-close"
			aria-label="Close screenshot"
			title="Close screenshot"
			onclick={() => (lightboxOpen = false)}>×</button
		>
		<img
			class="fb-lightbox-img"
			src={`${API_BASE}${pin.attachment.url}`}
			alt="Pasted screenshot"
		/>
	</div>
{/if}

<style>
	.fb-pin-body {
		position: fixed;
		width: 240px;
		max-height: 320px;
		overflow-y: auto;
		padding: 6px;
		background: #0a0c0f;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
		color: var(--rb-text);
		font-size: 10px;
	}
	.fb-body-text {
		margin: 2px 0 0;
		user-select: text;
	}
	.fb-attachment-btn {
		display: block;
		margin-top: 4px;
		padding: 0;
		border: none;
		background: none;
		cursor: pointer;
	}
	.fb-attachment-img {
		display: block;
		max-width: 100%;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		cursor: pointer;
	}
	.fb-note {
		margin: 4px 0 0;
		padding-top: 4px;
		border-top: 1px solid var(--rb-border);
		color: var(--rb-text-dim);
		/* pin 27fe1e3e61b5: .perf-root sets user-select: none globally, which
		   this inherits otherwise - an agent's reply must be copyable. */
		user-select: text;
	}
	.fb-blocked-request {
		margin: 4px 0 0;
		padding: 4px;
		border-left: 2px solid var(--rb-red);
		color: var(--rb-red);
		font-weight: 700;
	}
	.fb-note a {
		color: var(--rb-accent);
		word-break: break-all;
	}
	.fb-reply-label {
		color: var(--rb-text);
	}
	.fb-reply {
		display: block;
		width: 100%;
		margin-top: 4px;
		padding: 4px;
		box-sizing: border-box;
		background: #11151a;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text);
		font: inherit;
		resize: vertical;
		user-select: text;
	}
	.fb-issue-link {
		display: block;
		margin-top: 4px;
		color: var(--rb-accent);
		word-break: break-all;
	}
	.fb-provenance {
		margin: 4px 0 0;
		padding: 4px 0 0;
		border-top: 1px solid var(--rb-border);
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 2px 6px;
		user-select: text;
	}
	.fb-provenance dt {
		color: var(--rb-text-dim);
		font-weight: 600;
	}
	.fb-provenance dd {
		margin: 0;
		word-break: break-all;
	}
	.fb-lightbox {
		position: fixed;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		background: rgba(0, 0, 0, 0.8);
	}
	.fb-lightbox-img {
		max-width: 90vw;
		max-height: 90vh;
		object-fit: contain;
	}
	.fb-lightbox-close {
		position: absolute;
		top: 8px;
		right: 8px;
		width: 18px;
		height: 18px;
		padding: 0;
		border: none;
		background: none;
		color: var(--rb-text-dim);
		font-size: 16px;
		line-height: 1;
		cursor: pointer;
	}
	.fb-lightbox-close:hover,
	.fb-lightbox-close:focus-visible {
		color: var(--rb-text);
		outline: none;
	}
</style>

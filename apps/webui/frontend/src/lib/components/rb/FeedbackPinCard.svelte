<script lang="ts">
	import { pinBodyPos, pinBodyStyle, pinIsDone, pinStatus } from '$lib/rb/feedback';
	import { pinVisualState } from '$lib/rb/feedback-pin-partial';
	import { linkifyAgentNote } from '$lib/rb/feedback';
	import type { FeedbackPin } from '$lib/rb/feedback-store.svelte';

	let {
		pin,
		onclose,
		onarchive,
		onfollowon
	}: {
		pin: FeedbackPin;
		onclose: () => void;
		onarchive: () => void | Promise<void>;
		onfollowon: () => void;
	} = $props();

	let pinBodyElement: HTMLDivElement | null = $state(null);

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
		return pos !== null ? `left:${pos.x}px;top:${pos.y}px` : pinBodyStyle(pin);
	});

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
		if (pinBodyElement !== null) _reposition();
	});

	/** An outside pointer closes only this reopened card. The widget owns a
	 * separate new-pin draft, so it remains intact. Pointerdown precedes a
	 * marker click, letting another marker reopen its own card immediately. */
	function handleOutsidePinPointerDown(event: PointerEvent): void {
		const target = event.target;
		if (!(target instanceof Node) || pinBodyElement?.contains(target)) return;
		onclose();
	}
</script>

<svelte:window onpointerdowncapture={handleOutsidePinPointerDown} onresize={_reposition} />

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
	<p class="fb-body-text">{pin.text}</p>
	{#if pin.agent_note}
		<p class="fb-note" title="What an agent did about this pin">
			{#each linkifyAgentNote(pin.agent_note) as segment, i (i)}
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
	{#if pin.issue_url}
		<a
			class="fb-issue-link"
			href={pin.issue_url}
			target="_blank"
			rel="noreferrer noopener"
			title="Opens in the default browser">{pin.issue_url}</a
		>
	{/if}
	<div class="fb-row-btns">
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

<style>
	.fb-pin-body {
		position: fixed;
		z-index: 310;
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
	.fb-note a {
		color: var(--rb-accent);
		word-break: break-all;
	}
	.fb-issue-link {
		display: block;
		margin-top: 4px;
		color: var(--rb-accent);
		word-break: break-all;
	}
</style>

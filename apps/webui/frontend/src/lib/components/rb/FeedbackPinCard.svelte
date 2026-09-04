<script lang="ts">
	import { pinBodyStyle, pinIsDone, pinStatus } from '$lib/rb/feedback';
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

	/** An outside pointer closes only this reopened card. The widget owns a
	 * separate new-pin draft, so it remains intact. Pointerdown precedes a
	 * marker click, letting another marker reopen its own card immediately. */
	function handleOutsidePinPointerDown(event: PointerEvent): void {
		const target = event.target;
		if (!(target instanceof Node) || pinBodyElement?.contains(target)) return;
		onclose();
	}
</script>

<svelte:window onpointerdowncapture={handleOutsidePinPointerDown} />

<div
	class="fb-pin-body"
	style={pinBodyStyle(pin)}
	role="dialog"
	aria-label="Comment pin"
	bind:this={pinBodyElement}
>
	<p class="fb-hint">{pinStatus(pin)} - {pin.created_at}</p>
	<p class="fb-body-text">{pin.text}</p>
	{#if pin.agent_note}
		<p class="fb-note" title="What an agent did about this pin">{pin.agent_note}</p>
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
	}
	.fb-issue-link {
		display: block;
		margin-top: 4px;
		color: var(--rb-accent);
		word-break: break-all;
	}
</style>

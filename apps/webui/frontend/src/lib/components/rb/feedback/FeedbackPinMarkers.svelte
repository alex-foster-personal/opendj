<script lang="ts">
	/**
	 * The comment-pin marker layer (#858): every drawn pin on this page, as a
	 * fixed-position glyph whose COLOR is its lifecycle status and whose click
	 * opens the pin body.
	 *
	 * Split out of FeedbackWidget.svelte so the marker's markup and the status
	 * palette that gives it meaning live together in one file. The widget owns
	 * which pins are drawn (archived pins are filtered out, never merely
	 * hidden) and what happens on open; this component owns how a pin looks.
	 */
	import { isPinUnread, pinStatus, pinStyle, type PinSeen } from '$lib/rb/feedback';
	import type { FeedbackPin } from '$lib/rb/feedback-store.svelte';

	let {
		pins,
		seen,
		onopen
	}: {
		pins: FeedbackPin[];
		seen: PinSeen;
		onopen: (pin: FeedbackPin) => void;
	} = $props();
</script>

{#each pins as pin (pin.id)}
	{@const status = pinStatus(pin)}
	<button
		type="button"
		class="fb-pin fb-{status}"
		class:fb-unread={isPinUnread(pin, seen)}
		style={pinStyle(pin)}
		title={`${pin.text} - ${pin.created_at}${pin.anchor ? ` (near ${pin.anchor})` : ''}`}
		aria-label={`Comment pin (${status}) - open`}
		onclick={() => onopen(pin)}
	>
		<svg class="fb-mark" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
			<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
		</svg>
	</button>
{/each}

<style>
	/* #858 pin lifecycle. open = amber (unchanged), issued = amber + link
	   glyph, fixed = green OUTLINE, merged = SOLID green, archived = not
	   drawn at all (filtered out upstream, never merely hidden). */
	.fb-pin {
		position: fixed;
		z-index: 80;
		transform: translate(-50%, -50%);
		color: var(--rb-orange);
		cursor: pointer;
		background: none;
		border: none;
		padding: 0;
		line-height: 0;
	}
	.fb-mark path {
		fill: currentColor;
	}
	.fb-fixed,
	.fb-merged {
		color: var(--rb-green);
	}
	.fb-fixed .fb-mark path {
		fill: none;
		stroke: currentColor;
		stroke-width: 1.3;
		stroke-linejoin: round;
	}
	.fb-merged .fb-mark path {
		fill: currentColor;
	}
	/* issued: amber still, plus the link mark that says it has been filed */
	.fb-issued::before {
		content: '';
		position: absolute;
		left: 7px;
		bottom: 0;
		width: 6px;
		height: 3px;
		border: 1px solid currentColor;
		border-radius: 2px;
	}
	/* the unread dot: an agent has written to this pin since the maintainer read it */
	.fb-unread::after {
		content: '';
		position: absolute;
		top: -2px;
		right: -3px;
		width: 5px;
		height: 5px;
		border-radius: 50%;
		background: var(--rb-accent);
	}
</style>

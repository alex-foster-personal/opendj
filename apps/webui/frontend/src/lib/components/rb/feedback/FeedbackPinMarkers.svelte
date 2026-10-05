<script lang="ts">
	/**
	 * The comment-pin marker layer (#858, #3782): every drawn pin, placed at the
	 * position FeedbackPinLayer resolved for it (feedback-pin-position.ts:
	 * its element, else a nearby anchor, else stored viewport percentages).
	 * A pin drawn on a fallback tier carries a subtle dashed ring. Badges
	 * surface anchor/route drift, harvest, and regression without hiding the pin.
	 */
	import { isPinUnread, pinStyle, type PinSeen } from '$lib/rb/feedback';
	import { resolvedPinStyle, type ResolvedPinPosition } from '$lib/rb/feedback-pin-position';
	import {
		anchorMovedAtPin,
		pinBoardState,
		type PinBoardBadge
	} from '$lib/rb/feedback-pin-board';
	import { pinVisualState } from '$lib/rb/feedback-pin-partial';
	import type { FeedbackPin } from '$lib/rb/feedback-store.svelte';

	let {
		pins,
		positions = new Map(),
		seen,
		pathname,
		optionKeyHeld = false,
		onopen
	}: {
		pins: FeedbackPin[];
		/** Resolved placement per pin id; a pin with none draws at its stored
		 * percentages, exactly as before placement existed. */
		positions?: ReadonlyMap<string, ResolvedPinPosition>;
		seen: PinSeen;
		pathname: string;
		optionKeyHeld?: boolean;
		onopen: (pin: FeedbackPin) => void;
	} = $props();

	function badgeLabel(badge: PinBoardBadge): string {
		switch (badge) {
			case 'anchor-moved':
				return 'anchor moved';
			case 'route-moved':
				return 'route moved';
			case 'harvested':
				return 'harvested';
			case 'regressed':
				return 'regressed';
			default: {
				const _exhaustive: never = badge;
				return _exhaustive;
			}
		}
	}

	function anchorMovedFor(pin: FeedbackPin): boolean {
		if (typeof document === 'undefined') return false;
		return anchorMovedAtPin(pin, (x, y) => document.elementFromPoint(x, y));
	}
</script>

{#each pins as pin (pin.id)}
	{@const state = pinVisualState(pin)}
	{@const board = pinBoardState(pin, pathname, anchorMovedFor(pin))}
	{@const pos = positions.get(pin.id)}
	<button
		type="button"
		class="fb-pin fb-{state}"
		class:fb-agent={pin.author === 'agent'}
		class:fb-unread={isPinUnread(pin, seen)}
		class:fb-harvested={board.collapsed}
		class:fb-pos-fallback={pos?.fallback === true}
		data-pin-id={pin.id}
		data-pin-tier={pos?.tier ?? 'viewport'}
		style={pos === undefined ? pinStyle(pin) : resolvedPinStyle(pin, pos)}
		title={`${pin.text} - ${pin.created_at}${pin.anchor ? ` (near ${pin.anchor})` : ''}${pos?.fallback ? ` [placed via ${pos.via ?? 'saved screen position'}]` : ''}${pin.page !== pathname ? ` [page ${pin.page}]` : ''}${board.badges.length ? ` - ${board.badges.map(badgeLabel).join(', ')}` : ''}`}
		aria-label={`Comment pin (${state}) - open${board.badges.length ? ` - ${board.badges.join(', ')}` : ''}`}
		onclick={() => onopen(pin)}
	>
		{#if pin.author === 'agent'}
			<svg class="fb-mark fb-robot" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<rect x="2" y="3" width="8" height="6" rx="1" />
				<path d="M6 1v2M4 5h.01M8 5h.01M4 7h4" />
			</svg>
		{:else if state === 'partial'}
			<svg class="fb-mark fb-mark-half fb-mark-half-open" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
			</svg>
			<svg class="fb-mark fb-mark-half fb-mark-half-fixed" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
			</svg>
		{:else}
			<svg class="fb-mark" width="12" height="11" viewBox="0 0 12 11" aria-hidden="true">
				<path d="M1.5 1.5 h9 v6 h-4.5 l-2.5 2.4 v-2.4 h-2 z" />
			</svg>
		{/if}
		{#if pin.attachment}
			<!-- FB-23: a mark, not the image. The screenshot is requested when
			     the pin is opened (FeedbackPinCard), never for every pin at load. -->
			<span class="fb-pin-thumb" title="Screenshot attached - open the pin to load it"></span>
		{/if}
		{#if optionKeyHeld}
			{#each board.badges as badge (badge)}
				<span class="fb-pin-badge fb-pin-badge-{badge}" title={badgeLabel(badge)}>{badgeLabel(badge)}</span>
			{/each}
		{/if}
	</button>
{/each}

<style>
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
	.fb-harvested {
		opacity: 0.55;
		transform: translate(-50%, -50%) scale(0.82);
	}
	/* On a fallback tier: what it was pinned to moved or is gone. Subtle on
	 * purpose - the pin is still a valid record, just re-placed. */
	.fb-pos-fallback {
		outline: 1px dashed currentColor;
		outline-offset: 2px;
		border-radius: 50%;
		opacity: 0.85;
	}
	.fb-pin-badge {
		position: absolute;
		left: 50%;
		top: 100%;
		transform: translateX(-50%);
		margin-top: 2px;
		padding: 0 3px;
		font: 9px/1.2 var(--rb-font, system-ui, sans-serif);
		white-space: nowrap;
		border-radius: 2px;
		background: rgba(0, 0, 0, 0.72);
		color: #fff;
		pointer-events: none;
	}
	.fb-pin-badge-anchor-moved { background: #b45309; }
	.fb-pin-badge-route-moved { background: #1d4ed8; }
	.fb-pin-badge-harvested { background: #4b5563; }
	.fb-pin-badge-regressed { background: #b91c1c; }
	.fb-mark path {
		fill: currentColor;
	}
	.fb-fixed,
	.fb-merged {
		color: var(--rb-green);
	}
	.fb-blocked {
		color: var(--rb-red);
	}
	.fb-agent { color: #a855f7; }
	.fb-agent.fb-fixed,
	.fb-agent.fb-merged { color: var(--rb-green); }
	.fb-robot { fill: currentColor; stroke: currentColor; stroke-width: 1; stroke-linecap: round; }
	.fb-partial {
		position: fixed;
		width: 12px;
		height: 11px;
	}
	.fb-mark-half {
		position: absolute;
		top: 0;
		left: 0;
	}
	.fb-mark-half-open {
		color: var(--rb-orange);
		clip-path: inset(0 50% 0 0);
	}
	.fb-mark-half-fixed {
		color: var(--rb-green);
		clip-path: inset(0 0 0 50%);
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
	.fb-pin-thumb {
		display: block;
		width: 28px;
		height: 20px;
		background: color-mix(in srgb, currentColor 22%, transparent);
		border: 1px solid currentColor;
		border-radius: 1px;
		margin-top: 2px;
		pointer-events: none;
	}
</style>

<script lang="ts">
	/**
	 * R4: a short inline hint next to a control whose shift-click was refused
	 * because the group already spans two channels. Positioned from the refused
	 * control's own rect, so it reads as "this one", and never takes focus.
	 */
	import { knobUi } from '$lib/rb/knob-control.svelte';

	function _onTop(el: Element): boolean {
		const r = el.getBoundingClientRect();
		const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
		return hit !== null && el.contains(hit);
	}

	const pos = $derived.by(() => {
		const hint = knobUi.groupHint;
		if (hint === null || typeof document === 'undefined') return null;
		// The raised EQ overlay reuses the mixer's knob ids, and the mixer comes
		// first in the document: anchor to the copy that is actually on top.
		const matches = [...document.querySelectorAll(`[data-knob-id="${CSS.escape(hint.id)}"]`)];
		const el = matches.find(_onTop) ?? matches.at(-1) ?? null;
		if (el === null) return null;
		const r = el.getBoundingClientRect();
		return { left: r.left + r.width / 2, top: r.bottom + 4, text: hint.text };
	});
</script>

{#if pos !== null}
	<div
		class="group-hint"
		role="status"
		data-testid="mixer-group-hint"
		style={`left: ${pos.left}px; top: ${pos.top}px;`}
	>
		{pos.text}
	</div>
{/if}

<style>
	.group-hint {
		position: fixed;
		transform: translateX(-50%);
		/* Above EqOverlay (60), which renders later at the same level. */
		z-index: 61;
		pointer-events: none;
		padding: 2px 6px;
		font-size: 10px;
		white-space: nowrap;
		color: var(--rb-text, #e6e9ef);
		background: var(--rb-panel-raised, #1d2129);
		border: 1px solid var(--rb-accent, #4aa3ff);
		border-radius: 3px;
	}
</style>

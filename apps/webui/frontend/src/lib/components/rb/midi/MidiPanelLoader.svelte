<script lang="ts">
	/**
	 * Lazy mount point for the MIDI drawer (CHROME-07, issue #3886).
	 *
	 * TopBar renders this on every /performance load, but the drawer itself
	 * only exists after a click (the TopBar MIDI label or the mixer tray's MIDI
	 * button, both of which flip midiUi.panelOpen). A static import would put
	 * the whole drawer, its learn log and its I/O controls in the boot bundle
	 * for every session, so the drawer module is fetched by the same open flip
	 * that reveals it, the pattern MidiPanel already uses for its device list.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ The drawer module is not imported until midiUi.panelOpen is true.
	 *     [if] TopBar's boot chunk still statically imports MidiPanel [then] ⛔️
	 *   ✔︎ A failed fetch is shown inline, in place of the drawer, with the
	 *     error text; nothing retries silently and nothing is swallowed.
	 *     [if] the import rejects and the open click shows nothing [then] ⛔️
	 *   ✔︎ A failed fetch is retried on the next open, not on every render.
	 *     [if] closing and reopening after a failure never retries [then] ⛔️
	 */
	import type { Component } from 'svelte';
	import { midiUi, toggleMidiPanel } from '$lib/components/rb/midi/midi-ui-state.svelte';

	let MidiPanelComponent: Component | null = $state(null);
	let loadError: string | null = $state(null);
	let loading = false;

	$effect(() => {
		if (!midiUi.panelOpen || MidiPanelComponent !== null || loading) return;
		loading = true;
		loadError = null;
		import('$lib/components/rb/MidiPanel.svelte')
			.then((m) => {
				MidiPanelComponent = m.default;
			})
			.catch((exc: unknown) => {
				console.error('[midi] MIDI panel failed to load', exc);
				loadError = exc instanceof Error ? exc.message : String(exc);
			})
			.finally(() => {
				loading = false;
			});
	});
</script>

{#if MidiPanelComponent}
	<MidiPanelComponent />
{:else if midiUi.panelOpen && loadError !== null}
	<div class="midi-load-error rb-panel" role="alert" data-testid="midi-panel-load-error">
		<span>MIDI panel failed to load: {loadError}</span>
		<button type="button" class="midi-load-close" onclick={toggleMidiPanel}>Close</button>
	</div>
{/if}

<style>
	.midi-load-error {
		position: fixed;
		top: 48px;
		right: 12px;
		z-index: 60;
		display: flex;
		gap: 10px;
		align-items: center;
		max-width: 420px;
		padding: 8px 12px;
		border: 1px solid var(--rb-red);
		color: var(--rb-text);
		font-size: 12px;
	}
	.midi-load-close {
		background: transparent;
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 11px;
		padding: 2px 8px;
		cursor: pointer;
	}
</style>

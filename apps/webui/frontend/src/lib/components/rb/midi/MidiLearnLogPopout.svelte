<script lang="ts">
	/**
	 * Floating learn-log pop-out (build unit: midi panel).
	 * A fixed overlay that keeps the MIDI learn log visible while driving the
	 * app. KISS: no drag, no resize. Two behaviours only -
	 *   - click-through: the container is pointer-events:none so clicks land on
	 *     the decks/mixer underneath; only the title bar is interactive.
	 *   - minimizable: collapse to the title bar to get it out of the way.
	 * Shares MidiLearnLogRows with the panel drawer, so the log content and
	 * friendly-label logic are identical in both places.
	 */
	import { learnLog } from '$lib/rb/midi/webmidi.svelte';
	import {
		closeLogPopout,
		midiUi,
		toggleLogPopoutMinimized
	} from '$lib/components/rb/midi/midi-ui-state.svelte';
	import MidiLearnLogRows from '$lib/components/rb/midi/MidiLearnLogRows.svelte';
</script>

{#if midiUi.logPopoutOpen}
	<div class="log-popout" class:minimized={midiUi.logPopoutMinimized} role="log" aria-label="MIDI learn log">
		<header class="popout-head">
			<span class="popout-title">MIDI log</span>
			<span class="popout-count" title="MIDI messages in the learn log (last 50 kept)">{learnLog.length}</span>
			<span class="popout-spacer"></span>
			<button
				class="popout-btn"
				aria-label={midiUi.logPopoutMinimized ? 'restore MIDI log' : 'minimize MIDI log'}
				title={midiUi.logPopoutMinimized ? 'restore' : 'minimize'}
				onclick={toggleLogPopoutMinimized}
			>
				{#if midiUi.logPopoutMinimized}
					<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" data-icon="restore">
						<rect x="1.5" y="1.5" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.2" />
					</svg>
				{:else}
					<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" data-icon="minimize">
						<path d="M1.5 5h7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
					</svg>
				{/if}
			</button>
			<button class="popout-btn" aria-label="close MIDI log" title="close" onclick={closeLogPopout}>
				&times;
			</button>
		</header>
		{#if !midiUi.logPopoutMinimized}
			<div class="popout-body">
				<MidiLearnLogRows />
			</div>
		{/if}
	</div>
{/if}

<style>
	/* Container is click-through (pointer-events:none): clicks pass to the app
	 * beneath. Only the title bar re-enables pointer events so minimize/close
	 * stay usable. */
	.log-popout {
		position: fixed;
		left: 8px;
		bottom: 8px;
		z-index: 45;
		width: min(460px, 60vw);
		display: flex;
		flex-direction: column;
		pointer-events: none;
		font-family: var(--rb-font);
		box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		overflow: hidden;
	}
	.popout-head {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 3px 6px;
		background: var(--rb-panel-raised);
		border-bottom: 1px solid var(--rb-border);
		pointer-events: auto; /* re-enable: this bar IS clickable */
		cursor: default;
	}
	.log-popout.minimized .popout-head {
		border-bottom: none;
	}
	.popout-title {
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		letter-spacing: 0.08em;
	}
	.popout-count {
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		font-variant-numeric: tabular-nums;
	}
	.popout-spacer {
		flex: 1;
	}
	.popout-btn {
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-size: 13px;
		line-height: 1;
		padding: 0 4px;
		cursor: pointer;
	}
	.popout-btn:hover {
		color: var(--rb-text);
	}
	/* Body stays click-through so the log never blocks the decks behind it.
	 * That means it can't be scrolled - fine: newest is at the top and the
	 * ring buffer is capped, so the live tail is always in view. */
	.popout-body {
		max-height: 40vh;
		overflow: hidden;
		background: var(--rb-panel);
		padding: 4px;
	}
</style>

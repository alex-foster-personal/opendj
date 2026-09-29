<script lang="ts">
	/**
	 * MIDI panel drawer (build unit: midi panel).
	 * Overlay drawer over the /performance layout, opened from the TopBar
	 * MIDI label. Everything shown is REAL state from webmidi.svelte.ts +
	 * midi-ui-state.svelte.ts - no fabricated placeholders anywhere.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ 🎯 Permission section: request button + live permission state;
	 *     request failures render red (midiUi.lastError), never vanish.
	 *     [if] the browser prompt is denied [then] the panel shows 'denied'
	 *       + the error text, no silent retry ⛔️
	 *   ✔︎ 🎯 Devices section: MidiDeviceList (name, map matched, binding
	 *     count, LED test per device).
	 *   ✔︎ 🎯 Learn-log section: MidiLearnLog console (last 50, unmapped red).
	 *   ✔︎ 🎯 Esc / backdrop / close button all dismiss the drawer.
	 *     [if] Esc while open doesn't close [then] broken
	 */
	import type { Component } from 'svelte';
	import MidiLearnLog from '$lib/components/rb/midi/MidiLearnLog.svelte';
	import {
		closeLogPopout,
		floatMidiPanel,
		midiUi,
		applyMidiEnabledSetting,
		setMidiPanelWidthMode,
		toggleMidiPanel,
		toggleMidiPanelExpanded
	} from '$lib/components/rb/midi/midi-ui-state.svelte';
	import { midiState } from '$lib/rb/midi/webmidi.svelte';

	const PERMISSION_LABEL: Record<typeof midiState.permission, string> = {
		unsupported: 'not supported in this browser (WebMIDI needs Chrome or Edge)',
		prompt: 'not requested yet',
		granted: 'granted',
		denied: 'denied - re-enable in browser site settings, then reload'
	};

	function handleKeydown(e: KeyboardEvent): void {
		if (e.key === 'Escape' && midiUi.panelOpen) {
			toggleMidiPanel();
		}
	}

	// This whole panel already only RENDERS while panelOpen (the {#if} below),
	// but a static import still puts a module's bytes in the eager /performance
	// bundle regardless of runtime visibility. The device list (per-device map
	// match, binding count, LED test) is the heavier of the panel's two
	// sub-views, so it is the one deferred to a real network fetch, triggered
	// by the same open flip that reveals it.
	//
	// The learn-log pop-out is rarer still (its trigger is inside this panel),
	// so it loads here too. This component mounts on the first open and stays
	// mounted, so each import runs once per document and the pop-out outlives
	// the drawer closing. Both follow MidiPanelLoader's contract: a failed
	// fetch shows its error in place, nothing is swallowed, and recovery is a
	// Reload the user clicks, since the browser's module map keeps the failure
	// and a same-document re-import can never succeed.
	let MidiDeviceListComponent: Component | null = $state(null);
	let MidiLearnLogPopoutComponent: Component | null = $state(null);
	let deviceListError: string | null = $state(null);
	let popoutError: string | null = $state(null);

	function _failed(what: string, exc: unknown): string {
		console.error(`[midi] ${what} failed to load`, exc);
		return exc instanceof Error ? exc.message : String(exc);
	}

	// Reads no state, so each runs once, at mount.
	$effect(() => {
		import('$lib/components/rb/midi/MidiDeviceList.svelte')
			.then((m) => (MidiDeviceListComponent = m.default))
			.catch((exc: unknown) => (deviceListError = _failed('device list', exc)));
	});

	$effect(() => {
		import('$lib/components/rb/midi/MidiLearnLogPopout.svelte')
			.then((m) => (MidiLearnLogPopoutComponent = m.default))
			.catch((exc: unknown) => (popoutError = _failed('learn log pop-out', exc)));
	});
</script>

<svelte:window onkeydown={handleKeydown} />

{#if midiUi.panelOpen}
	<button class="midi-backdrop" aria-label="close MIDI panel" onclick={toggleMidiPanel}></button>
	<div
		class="midi-drawer rb-panel"
		class:expanded={midiUi.widthMode === 'expanded'}
		class:floating={midiUi.widthMode === 'floating'}
		role="dialog"
		aria-label="MIDI devices and learn log"
		data-width-mode={midiUi.widthMode}
	>
		<header class="drawer-head">
			<span class="drawer-title">MIDI</span>
			<div class="drawer-actions">
				<button
					type="button"
					class="drawer-action"
					aria-label={midiUi.widthMode === 'expanded' ? 'compact width' : 'expand to 70% viewport'}
					title={midiUi.widthMode === 'expanded' ? 'Compact width' : 'Expand to 70% viewport width'}
					onclick={toggleMidiPanelExpanded}
				>
					{midiUi.widthMode === 'expanded' ? 'Compact' : 'Expand'}
				</button>
				<button
					type="button"
					class="drawer-action"
					aria-label="float panel"
					title="Float panel (non-oversized)"
					onclick={() => {
						if (midiUi.widthMode === 'floating') setMidiPanelWidthMode('compact');
						else floatMidiPanel();
					}}
				>
					{midiUi.widthMode === 'floating' ? 'Dock' : 'Float'}
				</button>
				<button class="drawer-close" aria-label="close" onclick={toggleMidiPanel}>&times;</button>
			</div>
		</header>

		<section class="drawer-section">
			<h3 class="section-title">Permission</h3>
			<div class="perm-row">
				<span
					class="perm-state"
					class:perm-granted={midiState.permission === 'granted'}
					class:perm-bad={midiState.permission === 'denied' || midiState.permission === 'unsupported'}
				>
					{PERMISSION_LABEL[midiState.permission]}
				</span>
				{#if midiState.permission === 'prompt' || midiState.permission === 'denied'}
					<button
						class="perm-request"
						disabled={midiUi.requestPending}
						onclick={() => void applyMidiEnabledSetting(true)}
					>
						{midiUi.requestPending ? 'Waiting for browser prompt...' : 'Request MIDI access'}
					</button>
				{/if}
			</div>
			{#if midiUi.lastError !== null}
				<p class="perm-error">{midiUi.lastError}</p>
			{/if}
		</section>

		<section class="drawer-section">
			<h3 class="section-title">Devices ({midiState.devices.length})</h3>
			{#if MidiDeviceListComponent}
				<MidiDeviceListComponent />
			{:else if deviceListError !== null}
				<p class="perm-error" role="alert">
					Devices failed to load: {deviceListError}
					<button type="button" class="drawer-action" onclick={() => location.reload()}>Reload</button>
				</p>
			{/if}
		</section>

		<section class="drawer-section grow">
			<h3 class="section-title">Learn log</h3>
			<MidiLearnLog />
		</section>
	</div>
{/if}

{#if MidiLearnLogPopoutComponent}
	<MidiLearnLogPopoutComponent />
{:else if midiUi.logPopoutOpen && popoutError !== null}
	<div class="popout-error rb-panel" role="alert">
		<span>Learn log pop-out failed to load: {popoutError}</span>
		<button type="button" class="drawer-action" onclick={() => location.reload()}>Reload</button>
		<button type="button" class="drawer-action" onclick={closeLogPopout}>Close</button>
	</div>
{/if}

<style>
	/* Error text carries the failed module URL, which has no spaces: let it
	   wrap, or it pushes Reload and Close past the viewport edge. */
	.popout-error {
		position: fixed;
		right: 12px;
		bottom: 48px;
		z-index: 60;
		display: flex;
		gap: 10px;
		align-items: center;
		max-width: 420px;
		padding: 8px 12px;
		border: 1px solid var(--rb-red);
		color: var(--rb-text);
		font-size: 12px;
		overflow-wrap: anywhere;
	}
	.midi-backdrop {
		position: fixed;
		inset: 0;
		z-index: 40;
		background: rgba(0, 0, 0, 0.45);
		border: none;
		padding: 0;
		cursor: default;
	}
	.midi-drawer {
		position: fixed;
		top: var(--rb-topbar-h);
		right: 0;
		bottom: 0;
		z-index: 41;
		width: min(420px, 92vw);
		max-width: 70vw;
		display: flex;
		flex-direction: column;
		gap: 10px;
		padding: 10px;
		overflow-y: auto;
		box-shadow: -6px 0 18px rgba(0, 0, 0, 0.5);
	}
	.midi-drawer.expanded {
		width: 70vw;
	}
	.midi-drawer.floating {
		top: calc(var(--rb-topbar-h) + 12px);
		right: 12px;
		bottom: auto;
		max-height: min(80vh, 640px);
		border-radius: 6px;
		border: 1px solid var(--rb-border);
	}
	.drawer-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.drawer-actions {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.drawer-action {
		background: transparent;
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		color: var(--rb-text-dim);
		font-size: 10px;
		padding: 2px 6px;
		cursor: pointer;
	}
	.drawer-action:hover {
		color: var(--rb-text);
		border-color: var(--rb-text-dim);
	}
	.drawer-title {
		color: var(--rb-text);
		font-size: var(--rb-fs-deck-title);
		font-weight: 600;
		letter-spacing: 0.08em;
	}
	.drawer-close {
		background: transparent;
		border: none;
		color: var(--rb-text-dim);
		font-size: 16px;
		line-height: 1;
		cursor: pointer;
		padding: 0 4px;
	}
	.drawer-close:hover {
		color: var(--rb-text);
	}
	.drawer-section {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.drawer-section.grow {
		flex: 1;
		min-height: 0;
	}
	.section-title {
		margin: 0;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		border-bottom: 1px solid var(--rb-border);
		padding-bottom: 3px;
	}
	.perm-row {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.perm-state {
		color: var(--rb-text);
		font-size: var(--rb-fs-browser);
	}
	.perm-state.perm-granted {
		color: var(--rb-green);
	}
	.perm-state.perm-bad {
		color: var(--rb-red);
	}
	.perm-request {
		background: var(--rb-accent);
		border: 1px solid var(--rb-accent);
		border-radius: 2px;
		color: #fff;
		font-family: var(--rb-font);
		font-size: var(--rb-fs-label);
		padding: 3px 10px;
		cursor: pointer;
	}
	.perm-request:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.perm-error {
		margin: 0;
		color: var(--rb-red);
		font-size: var(--rb-fs-label);
		overflow-wrap: anywhere;
	}
</style>

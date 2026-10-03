<script lang="ts">
	// LV1 Create pairing freezes the loaded deck and EQ state at open, for the
	// deck picker's display only. Align hotcues and Reload sync (PAIR-03) read
	// LIVE deck state instead via getDeckState: the frozen PairingSnapshot
	// carries no stable_id/hot_cues/tempo, and both actions must act on
	// whatever is on the deck now, not what was loaded when the sheet opened.
	import { getDeckState } from '$lib/rb/audio-engine.svelte';
	import { dispatchPerformanceCommand, type PairingSnapshot } from '$lib/rb/performance-ipc.svelte';
	import type { DeckId } from '$lib/rb/deck-slots';
	import { alignHotcues, latestSyncSnapshot, PartialAlignmentError } from '$lib/rb/pairing-capture';
	import { pushToast } from '$lib/stores.svelte';

	let {
		open = $bindable(false),
		snapshot = $bindable(null)
	}: {
		open?: boolean;
		snapshot: PairingSnapshot | null;
	} = $props();

	let selected = $state<DeckId[]>([]);
	let busy = $state(false);
	let wasOpen = false;

	$effect(() => {
		if (open && !wasOpen && snapshot !== null) {
			selected = snapshot.decks.map((deck) => deck.deck_id).slice(0, 2);
		}
		wasOpen = open;
	});

	function _toggle(id: DeckId): void {
		if (selected.includes(id)) {
			selected = selected.filter((d) => d !== id);
			return;
		}
		if (selected.length >= 2) {
			selected = [selected[1], id];
			return;
		}
		selected = [...selected, id];
	}

	async function _removeAdjuster(deck: DeckId, band: 'low' | 'mid' | 'high'): Promise<void> {
		const state = await dispatchPerformanceCommand({ type: 'pairing_snapshot_remove_eq_adjuster', deck, band });
		snapshot = state.pairing_snapshot;
	}

	async function _save(): Promise<void> {
		if (busy || selected.length !== 2) return;
		busy = true;
		try {
			await dispatchPerformanceCommand({
				type: 'pairing_snapshot_save', from_deck: selected[0], to_deck: selected[1]
			});
		} catch {
			// The dispatcher already toasted the failure; keep the sheet open
			// so the DJ can retry without re-freezing the decks.
			return;
		} finally {
			busy = false;
		}
		open = false;
		pushToast('Pairing saved', 'info');
	}

	interface LoadedPair {
		da: DeckId;
		db: DeckId;
		stableA: string;
		stableB: string;
	}

	/** Both selected decks' live identity, or null when the pair isn't ready
	 * (not loaded, or the same track on both decks). Pushes the user-facing
	 * toast itself so both actions below share one message. */
	function _loadedPair(): LoadedPair | null {
		if (selected.length !== 2) return null;
		const [da, db] = selected;
		const a = getDeckState(da);
		const b = getDeckState(db);
		if (a.stable_id === null || b.stable_id === null) {
			pushToast('Both decks must be loaded', 'error');
			return null;
		}
		if (a.stable_id === b.stable_id) {
			pushToast('Select two different tracks', 'error');
			return null;
		}
		return { da, db, stableA: a.stable_id, stableB: b.stable_id };
	}

	async function _alignHotcues(): Promise<void> {
		if (busy) return;
		const pair = _loadedPair();
		if (pair === null) return;
		const { da, db, stableA, stableB } = pair;
		busy = true;
		try {
			const result = await alignHotcues(stableA, stableB, getDeckState(da).hot_cues, getDeckState(db).hot_cues);
			if (result.paired === 0) {
				pushToast('No matching hotcue letters on both decks', 'error');
				return;
			}
			pushToast(`Aligned ${result.paired} hotcue pair(s)`, 'info');
		} catch (exc) {
			const paired = exc instanceof PartialAlignmentError ? exc.paired : 0;
			pushToast(
				paired > 0 ? `Align failed after ${paired} pair(s): ${String(exc)}` : `Align failed: ${String(exc)}`,
				'error'
			);
		} finally {
			busy = false;
		}
	}

	/** Reload the newest sync snapshot for the two selected tracks onto their
	 * current decks. Uses dispatchPerformanceCommand (not the UI helper that
	 * swallows errors) so a failed seek never reaches the "reloaded" toast. */
	async function _reloadSync(): Promise<void> {
		if (busy) return;
		const pair = _loadedPair();
		if (pair === null) return;
		const { da, db, stableA, stableB } = pair;
		busy = true;
		try {
			const snap = await latestSyncSnapshot(stableA, stableB);
			if (snap === null) {
				pushToast('No sync snapshot for this pair', 'error');
				return;
			}
			await dispatchPerformanceCommand({ type: 'tempo', deck: da, ratio: snap.aTempoRatio });
			await dispatchPerformanceCommand({ type: 'tempo', deck: db, ratio: snap.bTempoRatio });
			await dispatchPerformanceCommand({ type: 'master', deck: snap.masterIsA ? da : db });
			await dispatchPerformanceCommand({ type: 'seek', deck: da, position_ms: Math.round(snap.aPositionMs) });
			await dispatchPerformanceCommand({ type: 'seek', deck: db, position_ms: Math.round(snap.bPositionMs) });
			pushToast('Pairing sync reloaded', 'info');
		} catch (exc) {
			pushToast(`Reload pairing failed: ${String(exc)}`, 'error');
		} finally {
			busy = false;
		}
	}

</script>

{#if open}
	<div class="sheet" role="dialog" aria-label="Create pairing">
		<header>
			<strong>Create pairing</strong>
			<button type="button" class="x" onclick={() => (open = false)}>×</button>
		</header>
		<p class="hint">This pairing is frozen at the moment you opened it.</p>
		<ul>
			{#each snapshot?.decks ?? [] as d (d.deck_id)}
				<li>
					<label>
						<input
							type="checkbox"
						checked={selected.includes(d.deck_id)}
						onchange={() => _toggle(d.deck_id)}
					/>
						CH{d.deck_id}
						<span class="title">{d.title}</span>
						<span class="timestamp">{d.timestamp.unit === 'beats' ? `${d.timestamp.value} beats` : `${Math.floor(d.timestamp.value / 60000)}:${String(Math.floor(d.timestamp.value / 1000) % 60).padStart(2, '0')} time`}</span>
					</label>
					{#if d.eq_adjusts.length > 0}
						<div class="adjusts" aria-label={`CH${d.deck_id} saved EQ adjustments`}>
							{#each d.eq_adjusts as adjust (adjust.band)}
								<button class="eq-dial" type="button" title={`Remove ${adjust.band} EQ adjustment`} onclick={() => _removeAdjuster(d.deck_id, adjust.band)}>
									<span class="dial-line" style={`transform: rotate(${(adjust.value - 0.5) * 270}deg)`}></span>
									<span class="remove">×</span><small>{adjust.band.toUpperCase()} LO/HI</small>
								</button>
							{/each}
						</div>
					{/if}
				</li>
			{:else}
				<li class="empty">No loaded decks</li>
			{/each}
		</ul>
		<footer>
			<button type="button" class="ghost" disabled={busy || selected.length !== 2} onclick={() => void _alignHotcues()}>
				Align hotcues
			</button>
			<button type="button" class="ghost" disabled={busy || selected.length !== 2} onclick={() => void _reloadSync()}>
				Reload sync
			</button>
			<button type="button" class="primary" disabled={busy || selected.length !== 2} onclick={() => void _save()}>
				Capture
			</button>
		</footer>
	</div>
{/if}

<style>
	.sheet {
		position: fixed;
		top: 40px;
		right: 12px;
		z-index: 9500;
		width: min(360px, calc(100vw - 24px));
		background: var(--rb-panel, #14171d);
		border: 1px solid var(--rb-border, #2a3038);
		border-radius: 4px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
		color: var(--rb-text, #c8cdd2);
		font-family: var(--rb-font, ui-sans-serif, system-ui, sans-serif);
		font-size: 12px;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 10px 12px;
		border-bottom: 1px solid var(--rb-border, #2a3038);
	}
	.x {
		border: none;
		background: transparent;
		color: var(--rb-text-dim);
		font-size: 18px;
		cursor: pointer;
	}
	.hint {
		margin: 8px 12px;
		color: var(--rb-text-dim);
		font-size: 11px;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0 8px 8px;
		max-height: 240px;
		overflow: auto;
	}
	li {
		padding: 4px;
	}
	label {
		display: flex;
		align-items: center;
		gap: 6px;
		cursor: pointer;
	}
	.title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.timestamp { color: var(--rb-text-dim); font-variant-numeric: tabular-nums; }
	.adjusts { display: flex; gap: 6px; margin: 5px 0 0 24px; }
	.eq-dial { position: relative; width: 30px; height: 38px; border: 0; background: transparent; color: #e8742d; cursor: pointer; }
	.dial-line { position: absolute; top: 12px; left: 4px; width: 21px; border-top: 2px solid currentColor; transform-origin: center; }
	.eq-dial::before { content: ''; position: absolute; top: 2px; left: 3px; width: 22px; height: 22px; border: 2px solid currentColor; border-radius: 50%; }
	.eq-dial small { position: absolute; top: 26px; left: -2px; font-size: 7px; white-space: nowrap; }
	.remove { display: none; position: absolute; z-index: 1; top: 3px; right: 2px; color: #ff4e43; font-size: 15px; }
	.eq-dial:hover .remove, .eq-dial:focus-visible .remove { display: block; }
	.tag {
		font-size: 9px;
		padding: 0 4px;
		border-radius: 2px;
		background: #e0cc6e;
		color: #14171d;
	}
	.tag.play {
		background: var(--rb-green, #35c04f);
	}
	.empty {
		color: var(--rb-text-dim);
		padding: 12px;
	}
	footer {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		padding: 10px 12px;
		border-top: 1px solid var(--rb-border, #2a3038);
	}
	footer button {
		border: 1px solid var(--rb-border);
		border-radius: 2px;
		padding: 4px 10px;
		font-size: 11px;
		cursor: pointer;
	}
	.ghost {
		background: transparent;
		color: var(--rb-text);
	}
	.primary {
		background: var(--rb-accent, #3d7dd9);
		border-color: var(--rb-accent, #3d7dd9);
		color: #fff;
	}
	footer button:disabled {
		opacity: 0.45;
		cursor: default;
	}
</style>

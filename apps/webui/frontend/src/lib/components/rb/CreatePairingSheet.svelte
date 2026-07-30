<script lang="ts">
	// LV1 Create pairing: exactly two decks, capture sync snapshot + edge.
	import { DECK_IDS, deckStates } from '$lib/rb/audio-engine.svelte';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import { RB_API_BASE } from '$lib/rb/api-rb';
	import { createHotcueAlignment } from '$lib/rb/pairing-alignments.svelte';
	import type { DeckId, HotCueSlot } from '$lib/rb/types';
	import { pushToast } from '$lib/stores.svelte';

	let {
		open = $bindable(false)
	}: {
		open?: boolean;
	} = $props();

	let selected = $state<DeckId[]>([]);
	let busy = $state(false);
	let step = $state<'pick' | 'done'>('pick');

	const loadedDecks = $derived(
		DECK_IDS.filter((d) => deckStates[d].stable_id !== null).map((d) => ({
			id: d,
			title: deckStates[d].title ?? deckStates[d].stable_id ?? `CH${d}`,
			playing: deckStates[d].playing,
			is_master: deckStates[d].is_master,
			stable_id: deckStates[d].stable_id as string
		}))
	);

	$effect(() => {
		if (!open) return;
		step = 'pick';
		const playing = loadedDecks.filter((d) => d.playing).map((d) => d.id);
		selected = playing.length >= 2 ? playing.slice(0, 2) : loadedDecks.map((d) => d.id).slice(0, 2);
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

	function _beatMeta(deck: DeckId): { n: number | null; phase: number | null } {
		const st = deckStates[deck];
		const beats = st.anlz?.beatgrid.beats;
		if (beats === undefined || beats.length < 2) return { n: null, phase: null };
		const t = st.position_ms / 1000;
		let lo = 0;
		let hi = beats.length - 1;
		while (lo < hi) {
			const mid = Math.ceil((lo + hi) / 2);
			if (beats[mid].t <= t) lo = mid;
			else hi = mid - 1;
		}
		if (beats[lo].t > t) return { n: null, phase: null };
		const cur = beats[lo];
		const next = beats[Math.min(lo + 1, beats.length - 1)];
		const span = Math.max(1e-6, next.t - cur.t);
		return { n: cur.n, phase: (t - cur.t) / span };
	}

	async function _capture(): Promise<void> {
		if (selected.length !== 2) {
			pushToast('Select exactly two decks', 'error');
			return;
		}
		const [da, db] = selected;
		const a = deckStates[da];
		const b = deckStates[db];
		if (a.stable_id === null || b.stable_id === null) {
			pushToast('Both decks must be loaded', 'error');
			return;
		}
		const masterSide: 'a' | 'b' = a.is_master ? 'a' : b.is_master ? 'b' : 'a';
		const aBeat = _beatMeta(da);
		const bBeat = _beatMeta(db);
		busy = true;
		try {
			const r = await fetch(`${RB_API_BASE}/api/v1/pairings/sync-snapshots`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
				body: JSON.stringify({
					stable_a: a.stable_id,
					stable_b: b.stable_id,
					master_side: masterSide,
					sync_mode: a.sync_mode === 'beat' || b.sync_mode === 'beat' ? 'beat' : 'bar',
					a_tempo_ratio: a.pitch,
					b_tempo_ratio: b.pitch,
					a_position_ms: a.position_ms,
					b_position_ms: b.position_ms,
					a_position_beat_n: aBeat.n,
					a_position_phase: aBeat.phase,
					b_position_beat_n: bBeat.n,
					b_position_phase: bBeat.phase,
					write_edge: true
				})
			});
			if (!r.ok) {
				const err = await r.json().catch(() => null);
				throw new Error(err?.detail?.message ?? `HTTP ${r.status}`);
			}
			pushToast(`Pairing captured: CH${da} + CH${db}`, 'info');
			step = 'done';
			open = false;
		} catch (exc) {
			pushToast(`Create pairing failed: ${String(exc)}`, 'error');
		} finally {
			busy = false;
		}
	}

	async function _alignHotcues(): Promise<void> {
		if (selected.length !== 2) return;
		const [da, db] = selected;
		const a = deckStates[da];
		const b = deckStates[db];
		if (a.stable_id === null || b.stable_id === null) return;
		const slots: HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];
		let paired = 0;
		busy = true;
		try {
			for (const slot of slots) {
				const ha = a.hot_cues.find((c) => c.slot === slot);
				const hb = b.hot_cues.find((c) => c.slot === slot);
				if (ha === undefined || hb === undefined) continue;
				await createHotcueAlignment({
					stable_a: a.stable_id,
					stable_b: b.stable_id,
					slot_a: slot,
					slot_b: slot,
					ms_a: ha.in_ms,
					ms_b: hb.in_ms,
					label: `HC ${slot}`
				});
				paired += 1;
			}
			if (paired === 0) {
				pushToast('No matching hotcue letters on both decks', 'error');
				return;
			}
			pushToast(`Aligned ${paired} hotcue pair(s)`, 'info');
		} catch (exc) {
			pushToast(`Align failed: ${String(exc)}`, 'error');
		} finally {
			busy = false;
		}
	}

	/** Reload last snapshot for the two selected tracks onto free/current decks. */
	async function _reloadLatest(): Promise<void> {
		if (selected.length !== 2) return;
		const [da, db] = selected;
		const aId = deckStates[da].stable_id;
		const bId = deckStates[db].stable_id;
		if (aId === null || bId === null) return;
		busy = true;
		try {
			const q = new URLSearchParams({ stable_a: aId, stable_b: bId, limit: '1' });
			const r = await fetch(`${RB_API_BASE}/api/v1/pairings/sync-snapshots?${q}`, {
				headers: { Accept: 'application/json' }
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const rows = (await r.json()) as Array<{
				stable_a: string;
				stable_b: string;
				master_side: 'a' | 'b';
				a_tempo_ratio: number;
				b_tempo_ratio: number;
				a_position_ms: number;
				b_position_ms: number;
			}>;
			if (rows.length === 0) {
				pushToast('No sync snapshot for this pair', 'error');
				return;
			}
			const snap = rows[0];
			const deckA = snap.stable_a === aId ? da : db;
			const deckB = snap.stable_b === bId ? db : da;
			await runPerformanceCommandFromUi({
				type: 'tempo',
				deck: deckA,
				ratio: snap.a_tempo_ratio
			});
			await runPerformanceCommandFromUi({
				type: 'tempo',
				deck: deckB,
				ratio: snap.b_tempo_ratio
			});
			const masterDeck = snap.master_side === 'a' ? deckA : deckB;
			await runPerformanceCommandFromUi({ type: 'master', deck: masterDeck });
			await runPerformanceCommandFromUi({
				type: 'seek',
				deck: deckA,
				position_ms: Math.round(snap.a_position_ms)
			});
			await runPerformanceCommandFromUi({
				type: 'seek',
				deck: deckB,
				position_ms: Math.round(snap.b_position_ms)
			});
			pushToast('Pairing sync reloaded', 'info');
			open = false;
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
		<p class="hint">Select exactly two loaded decks. Playing decks are pre-checked.</p>
		<ul>
			{#each loadedDecks as d (d.id)}
				<li>
					<label>
						<input
							type="checkbox"
							checked={selected.includes(d.id)}
							onchange={() => _toggle(d.id)}
						/>
						CH{d.id}
						{#if d.is_master}<span class="tag">MASTER</span>{/if}
						{#if d.playing}<span class="tag play">PLAY</span>{/if}
						<span class="title">{d.title}</span>
					</label>
				</li>
			{:else}
				<li class="empty">No loaded decks</li>
			{/each}
		</ul>
		<footer>
			<button type="button" class="ghost" disabled={busy || selected.length !== 2} onclick={() => void _alignHotcues()}>
				Align hotcues
			</button>
			<button type="button" class="ghost" disabled={busy || selected.length !== 2} onclick={() => void _reloadLatest()}>
				Reload sync
			</button>
			<button
				type="button"
				class="primary"
				disabled={busy || selected.length !== 2}
				onclick={() => void _capture()}
			>
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

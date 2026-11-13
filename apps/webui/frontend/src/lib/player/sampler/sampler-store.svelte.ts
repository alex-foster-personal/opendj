/**
 * Edge sampler UI store (spec `(private path)` 7.4, 10; issue #10784).
 *
 * Owns the catalogue (ranked by the server's compat sort against the master deck), the
 * sets (server meta + computed layout), per-edge state (set, search, filters, results)
 * and pins. Every exported action is what the agent-parity layer binds commands to;
 * every failure surfaces as a toast and rethrows. Playback goes through an injected
 * engine seam (sampler-engine-seam.ts): until the real engine registers, `engineReady`
 * is false and a play leaves the tab not-playing with an error toast.
 *
 * Requirements (mini-PRD):
 *   ✔︎ the catalogue loads every page of the compat sort and lays out sets of `perSet`
 *     [if] the server holds 45 slots [then] 3 sets of 20, 20, 5 and each edge shows one [else ⛔️]
 *   ✔︎ the catalogue re-ranks when the master changes, at most every 5 s (RERANK_MIN_MS)
 *   ✔︎ refs `S<set>-<nn>` / `R-<nn>` resolve to the slot on screen (resolveRef)
 *     [if] ref is S2-03 after load [then] it is the third slot of the second set [else ⛔️]
 *   ✔︎ pins and slot or set edits save through the API and roll back with a toast on failure
 *     [if] PUT /pins answers 500 [then] the pin list is back to what it was [else ⛔️]
 *   ✔︎ with no engine a play is refused out loud
 *     [if] a tab fires before an engine registers [then] it stays 'free' and an error toast shows [else ⛔️]
 *   → starter set (port map row 29) and loop capture / drag (row 28) land later
 */

import { untrack } from 'svelte';
import { pushToast } from '$lib/stores.svelte';
import { getDeckState } from '$lib/player/state.svelte';
import { DECK_IDS } from '$lib/player/constants';
import {
	computeNextOnlyRef,
	compatibleKeyChecked,
	type NextOnlyRef
} from '$lib/rb/next-only-filter';
import { setSamplerEnabledPref, uiPrefs } from '$lib/rb/prefs.svelte';
import {
	fetchSamplerPins,
	fetchSamplerSets,
	fetchSamplerSlots,
	patchSamplerSlot,
	putSamplerPin,
	putSamplerSet,
	type FetchFn,
	type SlotPatch
} from './api-sampler';
import { DEFAULT_WAVE, clickAllowed, tabRef } from './edge-wave';
import { NO_FILTERS, haystack, matchesSlot, passesFilters, type EdgeFilters } from './edge-search';
import { NO_ENGINE, type SamplerEngine, type SlotPhase } from './sampler-engine-seam';
import { pageWrap, type SampleSet } from './sampler-sets';
import {
	layoutSets,
	patchWithRollback,
	rerankDelay,
	resolveRefIn,
	sameMaster,
	slotIsCompatible,
	slotsQueryFor,
	toSearchable,
	withPin
} from './sampler-store-logic';
import { displayName, type Edge, type SamplerSlot, type SetMeta } from './sampler-types';

const PAGE_LIMIT = 500;

export type FilterId = keyof EdgeFilters;

export interface EdgeState {
	set: number;
	query: string;
	filters: EdgeFilters;
	/** "show all" search results, or null when the edge shows a set */
	results: SamplerSlot[] | null;
	rpage: number;
	/** written by the overlay's wave loop: pointer in the edge zone or a tab held */
	awake: boolean;
	searchFocus: boolean;
	/** performance.now() of the last flip, for the top-to-bottom pop */
	flipAt: number;
}

const _edge = (set: number): EdgeState => ({
	set,
	query: '',
	filters: { ...NO_FILTERS },
	results: null,
	rpage: 0,
	awake: false,
	searchFocus: false,
	flipAt: -1e9
});

export const sampler = $state({
	slots: [] as SamplerSlot[],
	metas: [] as SetMeta[],
	sets: [] as SampleSet<SamplerSlot>[],
	edges: { L: _edge(1), R: _edge(0) } as Record<Edge, EdgeState>,
	pinned: [] as string[],
	editing: null as string | null,
	/** the edge whose tab opened the editor; one modal even when both edges show the slot */
	editingEdge: null as Edge | null,
	lastEdge: 'R' as Edge,
	master: null as NextOnlyRef | null,
	loaded: false,
	perSet: DEFAULT_WAVE.perSet,
	/** the catalogue's rekordbox read status ("ok" or "unavailable: <reason>"), null until loaded */
	rekordbox: null as string | null,
	/** false until a real audio engine registers; plays are refused until then */
	engineReady: false
});

/** Lowercased search text per slot id, rebuilt on load and rename (one pass per keystroke). */
const _hay = new Map<string, string>();

//----- seams: transport and engine

let _fetch: FetchFn = (input, init) => fetch(input, init);
let _engine: SamplerEngine = NO_ENGINE;

/** Send the store's API calls through `fetchFn` (tests, fixtures); the global fetch until set. */
export function setSamplerFetch(fetchFn: FetchFn): void {
	_fetch = fetchFn;
}

/** Plug the audio engine in (the engine module's one registration line); NO_ENGINE unplugs it. */
export function registerSamplerEngine(engine: SamplerEngine): void {
	_engine = engine;
	sampler.engineReady = engine !== NO_ENGINE;
}

//----- reads

export const isPinned = (id: string): boolean => sampler.pinned.includes(id);

/** What the slot's voice is doing; always 'free' while no engine is registered. */
export const slotPhase = (id: string): SlotPhase => _engine.phase(id);

/** The tabs an edge shows now: its set, or its page of search results. */
export function edgeItems(edge: Edge): Array<SamplerSlot | null> {
	const e = sampler.edges[edge];
	if (e.results !== null)
		return e.results.slice(e.rpage * sampler.perSet, (e.rpage + 1) * sampler.perSet);
	return sampler.sets[e.set]?.items ?? [];
}

/** The top tab's name, page counter and description for an edge. */
export function edgeTitle(edge: Edge): { name: string; page: string; description: string } {
	const e = sampler.edges[edge];
	if (e.results !== null) {
		const n = Math.max(1, Math.ceil(e.results.length / sampler.perSet));
		return { name: 'Search results', page: `${e.rpage + 1} / ${n}`, description: '' };
	}
	const st = sampler.sets[e.set];
	return {
		name: st?.name ?? 'No samples',
		page: `set ${e.set + 1} / ${Math.max(1, sampler.sets.length)}`,
		description: st?.description ?? ''
	};
}

export const slotCompatible = (s: SamplerSlot): boolean =>
	slotIsCompatible(s, sampler.master, uiPrefs.compatible_filter);

/** Whether a slot passes the edge's query and filters. */
export function slotMatchesEdge(edge: Edge, s: SamplerSlot): boolean {
	const e = sampler.edges[edge];
	return matchesSlot(
		toSearchable(s, e.filters.compatible ? slotCompatible(s) : true),
		e.query,
		e.filters
	);
}

export const edgeFiltering = (edge: Edge): boolean => {
	const e = sampler.edges[edge];
	return e.query.trim() !== '' || Object.values(e.filters).some(Boolean);
};

/** "m in all sets": one pass over the precomputed lowercase text. */
export function countAllMatches(edge: Edge): number {
	const e = sampler.edges[edge];
	const q = e.query.trim().toLowerCase();
	let n = 0;
	for (const s of sampler.slots) {
		const searchable = toSearchable(s, e.filters.compatible ? slotCompatible(s) : true);
		if (passesFilters(searchable, e.filters) && (!q || (_hay.get(s.slot_id) ?? '').includes(q)))
			n++;
	}
	return n;
}

/** The ref a tab shows: `S<set>-<nn>`, or `R-<nn>` on search results. */
export function edgeRef(edge: Edge, index: number): string {
	const e = sampler.edges[edge];
	return e.results !== null ? `R-${String(index + 1).padStart(2, '0')}` : tabRef(e.set, index);
}

/** The slot a ref names on screen, or null. */
export function resolveRef(ref: string): SamplerSlot | null {
	const fromR = sampler.edges.R.results;
	const results = fromR ?? sampler.edges.L.results;
	return resolveRefIn(ref, sampler.sets, results);
}

//----- loading and ranking

function _toast(what: string, err: unknown): never {
	pushToast(
		`${what}: ${err instanceof Error ? err.message : String(err)}`,
		'error',
		undefined,
		err
	);
	throw err;
}

function _indexHay(): void {
	_hay.clear();
	for (const s of sampler.slots) _hay.set(s.slot_id, haystack(toSearchable(s, true)));
}

function _relayout(): void {
	sampler.sets = layoutSets(sampler.slots, sampler.perSet, sampler.metas);
	const n = sampler.sets.length;
	for (const e of Object.values(sampler.edges)) e.set = n === 0 ? 0 : Math.min(e.set, n - 1);
	_warmVisible();
}

function _warmVisible(): void {
	const visible = [...edgeItems('L'), ...edgeItems('R')].filter(
		(s): s is SamplerSlot => s !== null
	);
	if (visible.length > 0) _engine.warm(visible);
}

/** The master deck reference the compat sort ranks against, or null with no usable deck. */
export function masterRef(): NextOnlyRef | null {
	const slices = DECK_IDS.map((d) => {
		const s = getDeckState(d);
		return {
			is_master: s.is_master,
			playing: s.playing,
			stable_id: s.stable_id,
			key: s.key,
			bpm: s.bpm,
			pitch: s.pitch,
			bpm_confidence: null,
			key_shift_semitones: s.key_shift_semitones
		};
	});
	return computeNextOnlyRef(slices, compatibleKeyChecked(uiPrefs.compatible_filter));
}

async function _fetchCatalogue(master: NextOnlyRef | null): Promise<SamplerSlot[]> {
	const all: SamplerSlot[] = [];
	for (let offset = 0; ; offset += PAGE_LIMIT) {
		const page = await fetchSamplerSlots(slotsQueryFor(master, offset, PAGE_LIMIT), _fetch);
		all.push(...page.slots);
		sampler.rekordbox = page.rekordbox;
		if (page.slots.length === 0 || all.length >= page.total) return all;
	}
}

/** Load the catalogue, set metas and pins, then lay the sets out; toasts and rethrows on failure. */
export async function loadSampler(): Promise<void> {
	try {
		const master = untrack(masterRef);
		const [slots, metas, pins] = await Promise.all([
			_fetchCatalogue(master),
			fetchSamplerSets(_fetch),
			fetchSamplerPins(_fetch)
		]);
		sampler.pinned = pins;
		sampler.master = master;
		sampler.slots = slots;
		sampler.metas = metas;
		_lastRankAt = performance.now();
		_indexHay();
		_relayout();
		sampler.loaded = true;
	} catch (err) {
		_toast('Edge sampler could not load samples', err);
	}
}

let _lastRankAt = -1e9;
let _rerankTimer: ReturnType<typeof setTimeout> | null = null;

async function _rerank(): Promise<void> {
	_rerankTimer = null;
	try {
		sampler.slots = await _fetchCatalogue(sampler.master);
		_lastRankAt = performance.now();
		_indexHay();
		_relayout();
	} catch (err) {
		_toast('Edge sampler could not re-rank for the new master', err);
	}
}

function _onMaster(next: NextOnlyRef | null): void {
	if (sameMaster(next, sampler.master)) return;
	sampler.master = next;
	if (!sampler.loaded || _rerankTimer !== null) return;
	_rerankTimer = setTimeout(
		() => void _rerank().catch(() => undefined),
		rerankDelay(_lastRankAt, performance.now())
	);
}

/** Start the store: load, then follow the master. Returns the teardown. */
export function startSampler(): () => void {
	void loadSampler().catch(() => undefined); // toasted inside
	const stop = $effect.root(() => {
		$effect(() => {
			const ref = masterRef();
			untrack(() => _onMaster(ref));
		});
	});
	return () => {
		stop();
		if (_rerankTimer !== null) clearTimeout(_rerankTimer);
		_rerankTimer = null;
	};
}

//----- actions (agent parity binds to these names)

/** Turn the sampler on or off; off closes the editor and puts both edges to sleep (voices keep going). */
export function setSamplerEnabled(on: boolean): void {
	setSamplerEnabledPref(on);
	if (!on) {
		sampler.editing = null;
		for (const e of Object.values(sampler.edges)) e.awake = false;
	}
}

/** Page an edge by `delta` sets (or result pages), wrapping; the other edge does not move. */
export function flipSet(edge: Edge, delta: number): void {
	const e = sampler.edges[edge];
	if (e.results !== null)
		e.rpage = pageWrap(e.rpage, delta, Math.max(1, Math.ceil(e.results.length / sampler.perSet)));
	else e.set = pageWrap(e.set, delta, sampler.sets.length);
	e.flipAt = performance.now();
	sampler.lastEdge = edge;
	_warmVisible();
}

/** Show set `setIndex` on an edge; throws when it is out of range. */
export function setEdgeSet(edge: Edge, setIndex: number): void {
	if (!Number.isInteger(setIndex) || setIndex < 0 || setIndex >= sampler.sets.length) {
		throw new Error(`setEdgeSet: set index ${setIndex} out of range 0..${sampler.sets.length - 1}`);
	}
	const e = sampler.edges[edge];
	e.results = null;
	e.set = setIndex;
	e.flipAt = performance.now();
	sampler.lastEdge = edge;
	_warmVisible();
}

/** Type into an edge's search; clearing it drops the edge back to its set. */
export function setEdgeSearch(edge: Edge, query: string): void {
	const e = sampler.edges[edge];
	e.query = query;
	if (query.trim() === '' && e.results !== null) clearEdgeResults(edge);
	sampler.lastEdge = edge;
}

/** Tick or untick one of an edge's filter chips. */
export function setEdgeFilter(edge: Edge, filterId: FilterId, on: boolean): void {
	if (!(filterId in NO_FILTERS)) throw new Error(`setEdgeFilter: unknown filter ${filterId}`);
	sampler.edges[edge].filters[filterId] = on;
	sampler.lastEdge = edge;
}

/** "show all": swap the edge to paged search results across every set. */
export function showEdgeResults(edge: Edge): void {
	const e = sampler.edges[edge];
	e.results = sampler.slots.filter((s) => slotMatchesEdge(edge, s));
	e.rpage = 0;
	e.flipAt = performance.now();
	sampler.lastEdge = edge;
	_warmVisible();
}

/** Leave search results and show the edge's set again. */
export function clearEdgeResults(edge: Edge): void {
	const e = sampler.edges[edge];
	e.results = null;
	e.flipAt = performance.now();
}

/** Pin or unpin a tab at once, persist it in sampler.db, roll back with a toast on failure. */
export async function pinSlot(id: string, pinned: boolean): Promise<void> {
	const before = sampler.pinned;
	sampler.pinned = withPin(before, id, pinned);
	try {
		sampler.pinned = await putSamplerPin(id, pinned, _fetch);
	} catch (err) {
		sampler.pinned = before;
		_toast(pinned ? 'Pin not saved, change undone' : 'Unpin not saved, change undone', err);
	}
}

/** A click on an edge tab pins or unpins it; false (nothing done) for an empty, narrow or just-woken tab. */
export function pinEdgeTabOnClick(
	edge: Edge,
	index: number,
	widthPx: number,
	msSinceWake: number
): boolean {
	const s = edgeItems(edge)[index];
	if (!s || !clickAllowed(widthPx, msSinceWake)) return false;
	void pinSlot(s.slot_id, !isPinned(s.slot_id)).catch(() => undefined); // toasted inside
	return true;
}

/** `edge` is the edge whose pencil was clicked; omitted (agent bus), the R edge wins when both show the slot. */
export function openSlotEditor(id: string | null, edge?: Edge): void {
	if (id !== null && !sampler.slots.some((s) => s.slot_id === id))
		throw new Error(`openSlotEditor: no slot ${id}`);
	sampler.editing = id;
	sampler.editingEdge =
		id === null ? null : (edge ?? (edgeItems('R').some((s) => s?.slot_id === id) ? 'R' : 'L'));
}

function _getSlot(id: string): SamplerSlot | null {
	return sampler.slots.find((s) => s.slot_id === id) ?? null;
}

function _putSlot(next: SamplerSlot): void {
	const i = sampler.slots.findIndex((s) => s.slot_id === next.slot_id);
	if (i < 0) throw new Error(`updateSlot: slot ${next.slot_id} vanished`);
	sampler.slots[i] = next;
	for (const st of sampler.sets)
		st.items = st.items.map((x) => (x?.slot_id === next.slot_id ? next : x));
	for (const e of Object.values(sampler.edges))
		if (e.results) e.results = e.results.map((x) => (x.slot_id === next.slot_id ? next : x));
	_hay.set(next.slot_id, haystack(toSearchable(next, true)));
}

/** Save a slot's name, description or settings at once; rolls back with a toast on failure. */
export async function updateSlot(id: string, patch: SlotPatch): Promise<SamplerSlot> {
	try {
		return await patchWithRollback(_getSlot, _putSlot, id, patch, (sid, p) =>
			patchSamplerSlot(sid, p, _fetch)
		);
	} catch (err) {
		_toast('Sample not saved, change undone', err);
	}
}

/** Name and describe a set (PUT /sets/{id}), keeping its saved layout. */
export async function nameSet(setId: string, name: string, description: string): Promise<void> {
	const clean = name.trim();
	if (clean === '') throw new Error('A set needs a name');
	const st = sampler.sets.find((s) => s.id === setId);
	if (st === undefined) throw new Error(`nameSet: no set ${setId}`);
	const meta = sampler.metas.find((m) => m.id === setId);
	try {
		const saved = await putSamplerSet(
			setId,
			{ name: clean, description: description.trim(), slot_ids: meta?.slot_ids ?? null },
			_fetch
		);
		sampler.metas = [...sampler.metas.filter((m) => m.id !== setId), saved];
		st.name = saved.name;
		st.description = saved.description;
		pushToast(`Set named ${saved.name}`);
	} catch (err) {
		_toast('Set name not saved', err);
	}
}

//----- engine wrappers (every rejection is a toast)

/** Play or stop a slot on the master; with no engine it stays not-playing and an error toasts. */
export function fireSlot(slot: SamplerSlot): Promise<void> {
	return _engine
		.toggle(slot)
		.catch((err: unknown) =>
			pushToast(`Could not play ${displayName(slot)}: ${String(err)}`, 'error', undefined, err)
		);
}

/** Stop one slot's voice. */
export function stopSampleSlot(id: string): void {
	try {
		_engine.stop(id);
	} catch (err) {
		pushToast(`Could not stop the sample: ${String(err)}`, 'error', undefined, err);
	}
}

/** Start the cue-bus preview of a slot, or stop the one that is previewing. */
export function togglePreview(slot: SamplerSlot, previewing: boolean): Promise<void> {
	if (previewing) {
		try {
			_engine.stopPreview();
		} catch (err) {
			pushToast(`Could not stop the preview: ${String(err)}`, 'error', undefined, err);
		}
		return Promise.resolve();
	}
	return _engine
		.preview(slot)
		.catch((err: unknown) =>
			pushToast(`Could not preview ${displayName(slot)}: ${String(err)}`, 'error', undefined, err)
		);
}

/** Fire the tab at `index` of the edge's shown set; an empty slot does nothing. */
export function fireEdgeTab(edge: Edge, index: number): void {
	const s = edgeItems(edge)[index];
	if (s) void fireSlot(s);
}

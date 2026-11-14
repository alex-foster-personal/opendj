/**
 * The ui-mirror's `sampler` section ((private path) section 10, agent parity; #10786).
 *
 * Requirements (mini-PRD):
 *   ✔︎ an agent reads what the edges show without the UI: on/off, engine, each edge's set or
 *     results page, query, filters and wake state, the pins and the open editor
 *     [if] the R edge shows search results [then] `edges.R.results` is their count [else ⛔️]
 *   ✔︎ `voices` lists only slots that are armed or playing, `preview` the slot on the cue bus
 *     [if] one slot plays and one previews [then] voices has one row and preview names the other [else ⛔️]
 *   ✔︎ the section is plain JSON: no live store proxies leak into the published document
 * Pure: the store and its phase read are passed in, so node tests load it directly.
 */

import type { EdgeFilters } from './edge-search';
import type { SlotPhase } from './sampler-engine-seam';
import type { Edge } from './sampler-types';

/** The store fields the mirror reads (a structural slice of sampler-store's `sampler`). */
export interface SamplerMirrorSource {
	slots: ReadonlyArray<{ slot_id: string }>;
	edges: Record<
		Edge,
		{
			set: number;
			query: string;
			filters: EdgeFilters;
			results: readonly unknown[] | null;
			rpage: number;
			awake: boolean;
		}
	>;
	pinned: readonly string[];
	editing: string | null;
	loaded: boolean;
	engineReady: boolean;
}

export interface SamplerEdgeMirror {
	set: number;
	/** count of "show all" search results, or null when the edge shows its set */
	results: number | null;
	rpage: number;
	query: string;
	filters: EdgeFilters;
	awake: boolean;
}

export interface SamplerMirror {
	enabled: boolean;
	loaded: boolean;
	engine_ready: boolean;
	edges: Record<Edge, SamplerEdgeMirror>;
	pinned: string[];
	editing: string | null;
	voices: Array<{ slot_id: string; phase: 'armed' | 'playing' }>;
	preview: string | null;
}

function _edge(e: SamplerMirrorSource['edges'][Edge]): SamplerEdgeMirror {
	return {
		set: e.set,
		results: e.results === null ? null : e.results.length,
		rpage: e.rpage,
		query: e.query,
		filters: { ...e.filters },
		awake: e.awake
	};
}

/** Build the plain-JSON `sampler` mirror section from the store slice and the engine phase read. */
export function buildSamplerMirror(
	source: SamplerMirrorSource,
	enabled: boolean,
	phaseOf: (slotId: string) => SlotPhase
): SamplerMirror {
	const voices: SamplerMirror['voices'] = [];
	let preview: string | null = null;
	for (const { slot_id } of source.slots) {
		const phase = phaseOf(slot_id);
		if (phase === 'armed' || phase === 'playing') voices.push({ slot_id, phase });
		else if (phase === 'previewing') preview = slot_id;
	}
	return {
		enabled,
		loaded: source.loaded,
		engine_ready: source.engineReady,
		edges: { L: _edge(source.edges.L), R: _edge(source.edges.R) },
		pinned: [...source.pinned],
		editing: source.editing,
		voices,
		preview
	};
}

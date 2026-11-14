/**
 * Edge sampler: the clip pool (spec 5.3 residency). Issue #10788 (S5, S11), ported from the v1
 * engine's hot pool so the engine keeps only voices and the master follow.
 *
 * - Hot tier: decoded AudioBuffers, warm tier: fetched clip bytes, both in one byte-budgeted LRU
 *   (64 MB). Pinned slots (sounding, armed or previewing) are never evicted.
 * - Stems (EDGESAMP-11872h): non-unity stem settings on a stems_ready slot decode the stem-set's
 *   part clips (cut by the server from the existing stem-set, never separated here) and sum them
 *   offline at the vector's part gains into ONE buffer, keyed by the stem settings hash. Unity
 *   stems, or a track with no stem-set yet, play the mix clip.
 * - One fetch per clip in flight; a second request for the same clip shares it.
 * - Residency (#10782, sampler-residency.ts): the budget may be a function (the perf tier's hot
 *   budget, read at each eviction), and `promote` runs a residency plan in order, decoding hot
 *   slots and fetching warm ones until the budget is reached. A hit is touched, so slots the plan
 *   no longer names age out first (scrolling away demotes by LRU).
 *
 * Requirements (mini-PRD):
 *   ✔︎ ✅ 🎯 the pool stays inside its byte budget and never drops a pinned slot
 *     [if] a decode pushes the pool over budget [then] the least recently used unpinned clip goes [else ⛔️]
 *     [if] the oldest clip is pinned [then] the next oldest goes instead [else ⛔️]
 *   ✔︎ ✅ 🎯 a hit is not fetched or decoded again, and a warm clip decodes from its bytes
 *     [if] a slot is asked for twice [then] one fetch and one decode happen [else ⛔️]
 *   ✔︎ ✅ 🎯 a residency plan promotes in order and stops at the budget
 *     [if] the plan names more hot slots than fit [then] the first ones decode and the rest wait [else ⛔️]
 *     [if] the plan is empty (closed panel) [then] nothing is fetched or decoded [else ⛔️]
 *   ✔︎ ✅ 🎯 unity stems fetch the mix clip only
 *     [if] the vector's stems are unity [then] no part clip is fetched [else ⛔️]
 */

import { CLIP_GUARD_MS } from './api-sampler';
import type { Tier } from './sampler-residency';
import type { SamplerSlot } from './sampler-types';
import {
	effectivePartGains,
	playsStems,
	stemMixPoolKey,
	type VectorStemPart
} from './sampler-stem-mix';

/** Hot pool budget (decoded buffers plus warm bytes). */
export const HOT_POOL_BYTES = 64 * 1024 * 1024;

/** Fetches one clip's FLAC bytes; `part` asks for a stem part cut from the stem-set. */
export type ClipFetch = (slotId: string, part?: VectorStemPart) => Promise<ArrayBuffer>;

/** The decode side of an audio context (a real one in the app, a fake in tests). */
export type DecodeContext = Pick<BaseAudioContext, 'sampleRate' | 'decodeAudioData'>;

interface Cached {
	bytes: ArrayBuffer | null;
	buffer: AudioBuffer | null;
}

/** Bytes one decoded buffer holds (32-bit float per sample per channel). */
export function bufferBytes(b: Pick<AudioBuffer, 'length' | 'numberOfChannels'>): number {
	return b.length * b.numberOfChannels * 4;
}

/** Decoded bytes a slot's clip will hold: region plus guards, stereo float32 (spec 5.1). */
export function hotBytesEstimate(
	slot: Pick<SamplerSlot, 'in_ms' | 'out_ms'>,
	sampleRate: number
): number {
	const sec = (slot.out_ms - slot.in_ms + 2 * CLIP_GUARD_MS) / 1000;
	return Math.ceil(sec * sampleRate) * 2 * 4;
}

/** The slot id a pool key belongs to (`<slot_id>@<rate>` or `<slot_id>@<rate>#stems-<hash>`). */
export function slotIdOfPoolKey(key: string): string {
	return key.slice(0, key.lastIndexOf('@'));
}

export class SamplerClipPool {
	readonly #pool = new Map<string, Cached>();
	readonly #inflight = new Map<string, Promise<ArrayBuffer>>();
	readonly #fetch: ClipFetch;
	readonly #pinned: () => ReadonlySet<string>;
	readonly #budgetOf: () => number;

	/**
	 * `pinned` returns the slot ids that must stay resident (sounding, armed, previewing, pad
	 * pins); `budget` is bytes, or a function read at each eviction (the perf tier's hot budget).
	 */
	constructor(
		fetch: ClipFetch,
		pinned: () => ReadonlySet<string>,
		budget: number | (() => number) = HOT_POOL_BYTES
	) {
		this.#fetch = fetch;
		this.#pinned = pinned;
		this.#budgetOf = typeof budget === 'number' ? () => budget : budget;
	}

	get #budget(): number {
		return this.#budgetOf();
	}

	/**
	 * Run a residency plan in order: hot slots decode, warm slots fetch bytes, until the plan's
	 * own hot slots would fill the budget (so a later slot never evicts an earlier one).
	 * Sequential, so the first visible slot is ready first. An empty plan does nothing.
	 */
	async promote(
		plan: readonly { slot: SamplerSlot; tier: Tier }[],
		ctx: DecodeContext,
		onError: (e: unknown) => void
	): Promise<void> {
		let planned = 0;
		for (const { slot, tier } of plan) {
			const resident = this.#residentTier(slot, ctx.sampleRate);
			if (tier === 'hot') planned += hotBytesEstimate(slot, ctx.sampleRate);
			if (resident === 'hot' || (resident === 'warm' && tier === 'warm')) {
				this.#touchKey(stemMixPoolKey(slot.slot_id, ctx.sampleRate, null));
				continue;
			}
			if (planned > this.#budget || this.bytes() >= this.#budget) return;
			try {
				if (tier === 'hot') await this.buffer(slot, ctx);
				else if (tier === 'warm') this.warm([slot], ctx.sampleRate, onError);
				else {
					const _exhaustive: never = tier;
					throw new Error(`Unhandled tier: ${_exhaustive}`);
				}
			} catch (e) {
				onError(e);
			}
		}
	}

	/** 'hot' when the slot's mix clip is decoded, 'warm' when only its bytes are held, else null. */
	#residentTier(slot: SamplerSlot, sampleRate: number): Tier | null {
		const c = this.#pool.get(stemMixPoolKey(slot.slot_id, sampleRate, null));
		if (c === undefined) return null;
		return c.buffer !== null ? 'hot' : 'warm';
	}

	#touchKey(key: string): void {
		const c = this.#pool.get(key);
		if (c === undefined) return;
		this.#pool.delete(key);
		this.#pool.set(key, c);
	}

	/** Bytes held: warm clip bytes plus decoded buffers. */
	bytes(): number {
		let n = 0;
		for (const c of this.#pool.values())
			n += (c.bytes?.byteLength ?? 0) + (c.buffer === null ? 0 : bufferBytes(c.buffer));
		return n;
	}

	/** Pool keys, least recently used first. */
	keys(): string[] {
		return [...this.#pool.keys()];
	}

	/** The buffer a slot plays on `ctx`: its stem mix when stems apply, else the mix clip. */
	async buffer(slot: SamplerSlot, ctx: DecodeContext): Promise<AudioBuffer> {
		if (playsStems(slot)) {
			const key = stemMixPoolKey(slot.slot_id, ctx.sampleRate, slot.vector!.stems);
			const hit = this.#pool.get(key);
			if (hit?.buffer) return this.#touch(key, hit);
			const buffer = await this.#stemMixBuffer(slot, ctx);
			this.#store(key, { bytes: null, buffer });
			return buffer;
		}
		const key = stemMixPoolKey(slot.slot_id, ctx.sampleRate, null);
		const hit = this.#pool.get(key);
		if (hit?.buffer) return this.#touch(key, hit);
		const warm = [...this.#pool.entries()].find(
			([k, c]) => slotIdOfPoolKey(k) === slot.slot_id && c.bytes !== null
		)?.[1].bytes;
		const bytes = warm ?? (await this.#fetchBytes(slot.slot_id));
		// decodeAudioData detaches its input; keep the warm copy.
		const buffer = await ctx.decodeAudioData(bytes.slice(0));
		this.#store(key, { bytes: null, buffer });
		return buffer;
	}

	/** Prefetch clip bytes (warm tier) for upcoming slots, stopping at the byte budget. No decode. */
	warm(slots: readonly SamplerSlot[], sampleRate: number, onError: (e: unknown) => void): void {
		for (const slot of slots) {
			if (this.keys().some((k) => slotIdOfPoolKey(k) === slot.slot_id)) continue;
			if (this.#inflight.has(slot.slot_id)) continue;
			if (this.bytes() >= this.#budget) return;
			void this.#fetchBytes(slot.slot_id).then((bytes) => {
				if (this.bytes() + bytes.byteLength > this.#budget) return;
				this.#pool.set(stemMixPoolKey(slot.slot_id, sampleRate, null), { bytes, buffer: null });
			}, onError);
		}
	}

	#touch(key: string, c: Cached): AudioBuffer {
		this.#pool.delete(key);
		this.#pool.set(key, c);
		if (c.buffer === null) throw new Error(`sampler pool ${key} touched without a buffer`);
		return c.buffer;
	}

	#store(key: string, c: Cached): void {
		this.#touch(key, c);
		this.#evict();
	}

	/** Evict least-recently-used entries until the pool fits; pinned slots never go. */
	#evict(): void {
		const pinned = this.#pinned();
		for (const key of this.keys()) {
			if (this.bytes() <= this.#budget) return;
			if (!pinned.has(slotIdOfPoolKey(key))) this.#pool.delete(key);
		}
	}

	#fetchBytes(slotId: string, part?: VectorStemPart): Promise<ArrayBuffer> {
		const k = part === undefined ? slotId : `${slotId}?part=${part}`;
		let p = this.#inflight.get(k);
		if (p === undefined) {
			p = this.#fetch(slotId, part).finally(() => this.#inflight.delete(k));
			this.#inflight.set(k, p);
		}
		return p;
	}

	/** Decode the stem-set's part clips and sum them, at the vector's part gains, into one buffer. */
	async #stemMixBuffer(slot: SamplerSlot, ctx: DecodeContext): Promise<AudioBuffer> {
		const stems = slot.vector?.stems;
		if (!stems || !slot.stem_parts)
			throw new Error(`sampler ${slot.slot_id}: stem mix needs stems and stem_parts`);
		const gains = effectivePartGains(stems, slot.stem_parts);
		const parts = await Promise.all(
			slot.stem_parts.map(async (p) => ({
				gain: gains[p] ?? 0,
				buf: await ctx.decodeAudioData(await this.#fetchBytes(slot.slot_id, p))
			}))
		);
		const length = Math.max(...parts.map((x) => x.buf.length));
		const off = new OfflineAudioContext(2, length, ctx.sampleRate);
		for (const { gain, buf } of parts) {
			if (gain === 0) continue;
			const src = off.createBufferSource();
			src.buffer = buf;
			const g = off.createGain();
			g.gain.value = gain;
			src.connect(g).connect(off.destination);
			src.start(0);
		}
		return off.startRendering();
	}
}

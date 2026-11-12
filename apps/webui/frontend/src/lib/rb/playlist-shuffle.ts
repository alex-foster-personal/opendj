/**
 * Playlist membership shuffle (issue #11325, pin 3b217ec7698c): seeded
 * weighted permutation and persistence via POST items:move (LIBM-22).
 */

import { movePlaylistItems, type PlaylistWriteResult } from './playlist-write';

/** Tunable recency skew; higher pushes high play-count tracks later. */
export const PLAYLIST_SHUFFLE_RECENCY_DIVISOR = 1;

export interface PlaylistShuffleMember {
	item_id: string;
	stable_id: string;
}

/** Mulberry32 PRNG; seed is a 32-bit unsigned int. */
export function mulberry32(seed: number): () => number {
	let t = seed >>> 0;
	return () => {
		t += 0x6d2b79f5;
		let x = t;
		x = Math.imul(x ^ (x >>> 15), x | 1);
		x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
		return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
	};
}

/** Weight for Open DJ play count only (rekordbox imports do not affect shuffle). */
export function opendjShuffleWeight(opendjPlayCount: number): number {
	if (!Number.isFinite(opendjPlayCount) || opendjPlayCount < 0) {
		throw new Error(`opendj play count ${opendjPlayCount} is not a non-negative number`);
	}
	return 1 / (1 + opendjPlayCount / PLAYLIST_SHUFFLE_RECENCY_DIVISOR);
}

/**
 * Efraimidis-Spirakis weighted permutation: higher weight tends toward the front.
 */
export function weightedShuffleItemIds(
	members: readonly PlaylistShuffleMember[],
	opendjPlayCountOf: (stableId: string) => number,
	seed: number
): string[] {
	if (members.length === 0) return [];
	const random = mulberry32(seed);
	const keyed = members.map((member) => {
		const weight = opendjShuffleWeight(opendjPlayCountOf(member.stable_id));
		const key = Math.pow(random(), 1 / weight);
		return { item_id: member.item_id, key };
	});
	keyed.sort((a, b) => b.key - a.key);
	return keyed.map((row) => row.item_id);
}

/** Apply a target item_id order using single-row items:move calls (LIBM-22). */
export async function reorderItemIdsViaMove(
	playlistId: string,
	etag: string,
	currentItemIds: readonly string[],
	targetItemIds: readonly string[],
	move: (
		playlistId: string,
		etag: string,
		body: Parameters<typeof movePlaylistItems>[2]
	) => Promise<PlaylistWriteResult & { renumbered?: boolean }> = movePlaylistItems
): Promise<{ etag: string; moves: number }> {
	if (currentItemIds.length !== targetItemIds.length) {
		throw new Error(
			`shuffle move: current length ${currentItemIds.length} != target ${targetItemIds.length}`
		);
	}
	if (new Set(currentItemIds).size !== currentItemIds.length) {
		throw new Error('shuffle move: duplicate item_id in current order');
	}
	if (new Set(targetItemIds).size !== targetItemIds.length) {
		throw new Error('shuffle move: duplicate item_id in target order');
	}
	if (currentItemIds.join('\0') === targetItemIds.join('\0')) {
		return { etag, moves: 0 };
	}
	let order = [...currentItemIds];
	let fresh = etag;
	let moves = 0;
	for (let i = 0; i < targetItemIds.length; i += 1) {
		const want = targetItemIds[i]!;
		if (order[i] === want) continue;
		const from = order.indexOf(want);
		if (from < 0) {
			throw new Error(`shuffle move: target item ${want} is not in the playlist`);
		}
		const moved = order[from]!;
		const result = await move(playlistId, fresh, {
			range_start: moved,
			range_length: 1,
			range_end: moved,
			before_item_id: order[i]!
		});
		fresh = result.etag;
		moves += 1;
		order.splice(from, 1);
		order.splice(i, 0, moved);
	}
	return { etag: fresh, moves };
}

/**
 * Minimal typed fetch wrappers over the music-dj-tools FastAPI daemon.
 *
 * Paths are string-literal so we do not need the generated
 * api-types.ts during hot-path development; `pnpm api:gen` regenerates
 * `src/lib/api-types.ts` for strict usage.
 */

const BASE = typeof window === 'undefined' ? 'http://127.0.0.1:8585' : '';

export interface Track {
	stable_id: string;
	title: string | null;
	artist: string | null;
	album: string | null;
	bpm: number | null;
	key: string | null;
	rating: number | null;
	tags: string[];
	notes: string | null;
	last_played_at: string | null;
	created_at: string;
	updated_at: string;
	provenance: Record<string, { value: unknown; source: string; confidence: number | null; modified_at: string }>;
}

export interface TracksPage {
	items: Track[];
	next_cursor: string | null;
}

export interface PlaylistSummary {
	playlist_id: string;
	name: string;
	vendor: string;
	track_count: number;
	updated_at: string;
	/** Rekordbox custom tree position (flattened ParentID/Seq walk);
	 * null for non-rekordbox playlists - never invent an order for those. */
	seq: number | null;
}

export interface PlaylistDetail extends Omit<PlaylistSummary, 'track_count' | 'seq'> {
	items: string[];
	diff: {
		rb_only: string[];
		djay_only: string[];
		both: string[];
		conflicts: { stable_id: string; rb_position: number; djay_position: number }[];
	};
}

export interface Pairing {
	pairing_id: string;
	from_stable_id: string;
	to_stable_id: string;
	direction: '->' | '<->';
	source: 'manual' | 'learned' | 'ai';
	notes: string | null;
	created_at: string;
	updated_at: string;
}

export interface QueueItem {
	stable_id: string;
	kind: string;
	payload: Record<string, unknown>;
}

export interface QueueOut {
	items: QueueItem[];
	note: string | null;
}

export interface HealthOut {
	status: 'ok';
	state_db: { path: string; tracks: number; playlists: number; pairings: number };
	cloud: { lock_holder: { holder?: string; expires_at?: string } | null };
	syncthing: null | { peers_connected: number; folder_state: string };
	bind_host: string;
	version: string;
}

export class ConflictError extends Error {
	constructor(public current: Track, public etag: string) {
		super('If-Match mismatch');
	}
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
	return fetch(`${BASE}${path}`, {
		...init,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(init.headers || {})
		}
	});
}

export async function listTracks(params: Record<string, string | number | undefined | null> = {}): Promise<TracksPage> {
	const qs = Object.entries(params)
		.filter(([, v]) => v !== undefined && v !== null && v !== '')
		.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
		.join('&');
	const r = await request(`/api/v1/tracks${qs ? '?' + qs : ''}`);
	return r.json();
}

export async function getTrack(stable_id: string): Promise<{ track: Track; etag: string }> {
	const r = await request(`/api/v1/tracks/${encodeURIComponent(stable_id)}`);
	return { track: await r.json(), etag: r.headers.get('etag') ?? '' };
}

export async function patchTrack(
	stable_id: string,
	etag: string,
	patch: { rating?: number; tags_add?: string[]; tags_remove?: string[]; notes?: string }
): Promise<{ track: Track; etag: string }> {
	const r = await request(`/api/v1/tracks/${encodeURIComponent(stable_id)}`, {
		method: 'PATCH',
		headers: { 'If-Match': etag },
		body: JSON.stringify(patch)
	});
	if (r.status === 409) {
		const body = await r.json();
		throw new ConflictError(body.current as Track, body.etag as string);
	}
	if (!r.ok) throw new Error(`PATCH failed: ${r.status}`);
	return { track: await r.json(), etag: r.headers.get('etag') ?? '' };
}

export async function listPlaylists(): Promise<PlaylistSummary[]> {
	const r = await request('/api/v1/playlists');
	return r.json();
}

export async function getPlaylist(id: string): Promise<PlaylistDetail> {
	const r = await request(`/api/v1/playlists/${encodeURIComponent(id)}`);
	return r.json();
}

export async function listPairings(source?: string): Promise<Pairing[]> {
	const r = await request(`/api/v1/pairings${source ? `?source=${encodeURIComponent(source)}` : ''}`);
	return r.json();
}

export async function createPairing(body: {
	from_stable_id: string;
	to_stable_id: string;
	direction?: '->' | '<->';
	source?: 'manual' | 'learned' | 'ai';
	notes?: string;
}): Promise<Pairing> {
	const r = await request('/api/v1/pairings', { method: 'POST', body: JSON.stringify(body) });
	if (!r.ok) throw new Error(`create pairing failed: ${r.status}`);
	return r.json();
}

export async function deletePairing(pairing_id: string, etag: string): Promise<void> {
	const r = await request(`/api/v1/pairings/${encodeURIComponent(pairing_id)}`, {
		method: 'DELETE',
		headers: { 'If-Match': etag }
	});
	if (r.status !== 204) throw new Error(`delete failed: ${r.status}`);
}

export async function getQueue(kind: string): Promise<QueueOut> {
	const r = await request(`/api/v1/queues/${encodeURIComponent(kind)}`);
	return r.json();
}

export async function getHealth(): Promise<{ health: HealthOut; bindWarning: string | null }> {
	const r = await request('/api/v1/health');
	return {
		health: await r.json(),
		bindWarning: r.headers.get('x-bind-warning')
	};
}

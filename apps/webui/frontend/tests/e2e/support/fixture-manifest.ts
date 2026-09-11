/**
 * Reader for the JSON manifest `deckload_fixture.py --manifest` writes.
 *
 * The performance suite used to hardcode `REAL_FLAWLESS_STABLE_ID`, a row
 * from the maintainer's personal library, in three specs. Since the performance
 * config now builds its own throwaway fixture (see
 * `playwright.performance.config.ts`), those specs need the SAME stable_id
 * this run's fixture actually produced -- fixture stable_ids are content
 * hashes, so they are stable across runs on unchanged audio but are not a
 * value anyone should hardcode a second time.
 *
 * This is the one shared reader every consuming spec imports, rather than
 * three copies of `JSON.parse(readFileSync(...))` drifting apart. It fails
 * loudly (never falls back to a hardcoded id) when the manifest is missing
 * or the fixture came up short, because a spec silently degrading to "some
 * other track" would fail confusingly deep inside a page evaluation instead
 * of here, at the one place that actually knows what went wrong.
 */
import { existsSync, readFileSync } from 'node:fs';

export type FixtureTrack = {
	stable_id: string;
	title: string | null;
	file_path: string | null;
};

export type FixtureManifest = {
	fixture_revision: number;
	populated_playlist_id: string;
	empty_playlist_id: string;
	autoplay_chain_playlist_id: string | null;
	autoplay_chain_playlist_name: string | null;
	autoplay_hunt_playlist_a_id?: string | null;
	autoplay_hunt_playlist_a_name?: string | null;
	autoplay_hunt_playlist_b_id?: string | null;
	autoplay_hunt_playlist_b_name?: string | null;
	tracks: FixtureTrack[];
};

function _isFixtureTrack(value: unknown): value is FixtureTrack {
	if (typeof value !== 'object' || value === null) return false;
	const row = value as Record<string, unknown>;
	return (
		typeof row.stable_id === 'string' &&
		row.stable_id.length > 0 &&
		(row.title === null || typeof row.title === 'string') &&
		(row.file_path === null || typeof row.file_path === 'string')
	);
}

function _isFixtureManifest(value: unknown): value is FixtureManifest {
	if (typeof value !== 'object' || value === null) return false;
	const manifest = value as Record<string, unknown>;
	return (
		typeof manifest.fixture_revision === 'number' &&
		typeof manifest.populated_playlist_id === 'string' &&
		typeof manifest.empty_playlist_id === 'string' &&
		(manifest.autoplay_chain_playlist_id === null ||
			typeof manifest.autoplay_chain_playlist_id === 'string') &&
		(manifest.autoplay_chain_playlist_name === null ||
			typeof manifest.autoplay_chain_playlist_name === 'string') &&
		(manifest.autoplay_hunt_playlist_a_id === undefined ||
			manifest.autoplay_hunt_playlist_a_id === null ||
			typeof manifest.autoplay_hunt_playlist_a_id === 'string') &&
		(manifest.autoplay_hunt_playlist_a_name === undefined ||
			manifest.autoplay_hunt_playlist_a_name === null ||
			typeof manifest.autoplay_hunt_playlist_a_name === 'string') &&
		(manifest.autoplay_hunt_playlist_b_id === undefined ||
			manifest.autoplay_hunt_playlist_b_id === null ||
			typeof manifest.autoplay_hunt_playlist_b_id === 'string') &&
		(manifest.autoplay_hunt_playlist_b_name === undefined ||
			manifest.autoplay_hunt_playlist_b_name === null ||
			typeof manifest.autoplay_hunt_playlist_b_name === 'string') &&
		Array.isArray(manifest.tracks) &&
		manifest.tracks.every(_isFixtureTrack)
	);
}

/**
 * Whether a manifest exists at all. The one legitimate "missing" case is
 * REAL-LIBRARY MODE (`PERFORMANCE_E2E_FIXTURE=0`), where the suite runs
 * against a real library and the specs that need generated-fixture ids skip
 * themselves with a named reason. Every other caller still gets a throw
 * from `readFixtureManifest`, which is what keeps a genuinely broken build
 * loud.
 */
export function fixtureManifestExists(manifestPath: string): boolean {
	return existsSync(manifestPath);
}

/**
 * Read and validate the manifest at `manifestPath`. Throws rather than
 * returning `null` or a partial object: every caller needs a real fixture id
 * to proceed, so there is no legitimate use for a value that means "missing".
 */
export function readFixtureManifest(manifestPath: string): FixtureManifest {
	if (!existsSync(manifestPath)) {
		throw new Error(
			`fixture manifest missing at ${manifestPath} -- the fixture builder must run ` +
				'(with --manifest) before this spec, and its data dir must match MDT_DATA_DIR'
		);
	}
	const raw: unknown = JSON.parse(readFileSync(manifestPath, 'utf-8'));
	if (!_isFixtureManifest(raw)) {
		throw new Error(`fixture manifest at ${manifestPath} is malformed: ${JSON.stringify(raw)}`);
	}
	return raw;
}

/**
 * The manifest's first track, standing in for the old hardcoded
 * `REAL_FLAWLESS_STABLE_ID`. Any single-track fixture need is satisfied by
 * this; specs that need more than one track's identity should read
 * `.tracks` off the manifest directly.
 */
export function primaryFixtureStableId(manifestPath: string): string {
	const manifest = readFixtureManifest(manifestPath);
	const first = manifest.tracks[0];
	if (first === undefined) {
		throw new Error(`fixture manifest at ${manifestPath} has zero tracks`);
	}
	return first.stable_id;
}

/**
 * The dedicated AutoPlay-chain playlist name and its member stable_ids, for
 * specs that need a real, non-null-key/BPM multi-track chain (>= 3 tracks).
 * Throws if the manifest was built without `--seed-autoplay-chain`.
 */
export function autoplayChainFixture(manifestPath: string): {
	playlistName: string;
	stableIds: string[];
} {
	const manifest = readFixtureManifest(manifestPath);
	if (manifest.autoplay_chain_playlist_id === null || manifest.autoplay_chain_playlist_name === null) {
		throw new Error(
			`fixture manifest at ${manifestPath} has no autoplay chain playlist -- ` +
				'the fixture must be built with --seed-autoplay-chain'
		);
	}
	if (manifest.tracks.length < 3) {
		throw new Error(
			`fixture manifest at ${manifestPath} has ${manifest.tracks.length} track(s), ` +
				'need >= 3 for an AutoPlay chain of more than one hop'
		);
	}
	return {
		playlistName: manifest.autoplay_chain_playlist_name,
		stableIds: manifest.tracks.map((track) => track.stable_id)
	};
}

export type AutoplayHuntPlaylist = {
	id: string;
	name: string;
};

/**
 * The two dedicated hunt playlists and their member stable_ids, for the
 * AutoPlay/mixing error hunt (#1853). Throws if the manifest was built
 * without `--seed-autoplay-hunt` or came up short of 6 tracks.
 */
export function autoplayHuntFixture(manifestPath: string): {
	playlistA: AutoplayHuntPlaylist;
	playlistB: AutoplayHuntPlaylist;
	stableIds: string[];
} {
	const manifest = readFixtureManifest(manifestPath);
	const aId = manifest.autoplay_hunt_playlist_a_id;
	const aName = manifest.autoplay_hunt_playlist_a_name;
	const bId = manifest.autoplay_hunt_playlist_b_id;
	const bName = manifest.autoplay_hunt_playlist_b_name;
	if (
		aId === undefined ||
		aId === null ||
		aName === undefined ||
		aName === null ||
		bId === undefined ||
		bId === null ||
		bName === undefined ||
		bName === null
	) {
		throw new Error(
			`fixture manifest at ${manifestPath} has no autoplay hunt playlists -- ` +
				'the fixture must be built with --seed-autoplay-hunt'
		);
	}
	if (manifest.tracks.length < 6) {
		throw new Error(
			`UNKNOWN: fixture manifest at ${manifestPath} has ${manifest.tracks.length} track(s), ` +
				'need >= 6 for the AutoPlay/mixing error hunt'
		);
	}
	return {
		playlistA: { id: aId, name: aName },
		playlistB: { id: bId, name: bName },
		stableIds: manifest.tracks.map((track) => track.stable_id)
	};
}

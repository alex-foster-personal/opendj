/** Hermetic USB stick library payloads for usb-stick-pane-refresh e2e. */

export const STICK_VOLUME_UUID = 'AAAAAAAA-0000-4000-8000-00000000000A';
export const STICK_VOLUME_ID = `vol:${STICK_VOLUME_UUID}`;

type UsbTrack = {
	id: string;
	pdb_id: number;
	title: string;
	artist: string | null;
	album: string | null;
	genre: string | null;
	key: string | null;
	bpm: number | null;
	duration_s: number | null;
	rating: number;
	play_count: number;
	file_path: string;
	file_present: boolean;
	has_analysis: boolean;
	has_artwork: boolean;
	date_added: string | null;
};

function track(pdbId: number, title: string): UsbTrack {
	return {
		id: `usb-${STICK_VOLUME_UUID}-${pdbId}`,
		pdb_id: pdbId,
		title,
		artist: 'E2E Artist',
		album: 'E2E Album',
		genre: 'Techno',
		key: '1A',
		bpm: 120,
		duration_s: 200,
		rating: 0,
		play_count: 0,
		file_path: `/Contents/e2e/${pdbId}.mp3`,
		file_present: true,
		has_analysis: true,
		has_artwork: false,
		date_added: null
	};
}

function libraryBody(tracks: UsbTrack[]) {
	return {
		volume_id: STICK_VOLUME_ID,
		volume_uuid: STICK_VOLUME_UUID,
		name: 'E2E STICK',
		mount_path: '/Volumes/E2E-STICK',
		tracks,
		playlists: [
			{
				id: 'pl-1',
				pdb_id: 1,
				name: 'Warmup',
				parent_id: null,
				is_folder: false,
				sort_order: 1,
				track_ids: tracks.slice(0, 2).map((t) => t.id)
			}
		],
		history: [],
		counts: {
			tracks: tracks.length,
			playlists: 1,
			playlist_entries: Math.min(2, tracks.length),
			history_playlists: 0
		},
		read_ms: 1.2,
		has_pdb: true,
		has_onelibrary: false,
		browse_source: 'export.pdb',
		cache_hit: false
	};
}

export const USB_STICK_LIBRARY_INITIAL = libraryBody([
	track(1, 'Initial Alpha'),
	track(2, 'Initial Beta'),
	track(3, 'Initial Gamma')
]);

export const USB_STICK_LIBRARY_REPLACED = libraryBody([
	track(1, 'Replaced Alpha'),
	track(3, 'Replaced Gamma'),
	track(4, 'Replaced Delta'),
	track(5, 'Replaced Epsilon')
]);

export const USB_STICK_VOLUMES_LIST = {
	volumes: [
		{
			id: STICK_VOLUME_ID,
			name: 'E2E STICK',
			mount_path: '/Volumes/E2E-STICK',
			kind: 'rekordbox',
			present: true,
			simulated: true,
			is_music: true,
			role: 'usb_stick',
			protocol: 'USB',
			hide_reason: null,
			access: 'ok',
			vendor_name: null,
			vendor_source: null,
			has_pdb: true,
			has_onelibrary: false
		}
	],
	scanned_at: 1,
	watching: true
};

export function usbStickNotMountedDetail() {
	return {
		detail: {
			code: 'USB_STICK_NOT_MOUNTED',
			message: `no mounted stick ${STICK_VOLUME_UUID}`,
			volume_uuid: STICK_VOLUME_UUID
		}
	};
}

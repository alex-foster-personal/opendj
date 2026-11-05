/**
 * Command ids behind every right-click menu item (W28, CTXMENU-02).
 *
 * The agent-native parity rule says a UI action is incomplete until an agent
 * can reach the same thing over IPC, CLI or HTTP. So a menu item never names a
 * closure: it names one of these ids, and each id names the real command an
 * agent would send. Two kinds exist:
 *
 * - `ipc`: a `PerformanceCommand['type']` of the shared dispatcher in
 *   `$lib/rb/performance-ipc.svelte.ts`. The unit harness reads that union and
 *   fails on a type it does not contain. (Not a type import: that module is
 *   already the most-imported file in the app, and the quality ratchet caps
 *   its fan-in.)
 * - `http`: a method + path of the FastAPI daemon. The unit harness checks
 *   each one against `apps/webui/openapi.json`, so a removed route fails the
 *   suite rather than leaving a menu item that 404s.
 *
 * Pure data: no Svelte, no engine import, so node tests can load it.
 */
export type IpcCommand = { via: 'ipc'; type: string };
export type HttpCommand = {
	via: 'http';
	method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
	path: `/api/v1/${string}`;
	/** The CLI twin, when one exists, shown beside the route in the tooltip. */
	cli?: string;
};
export type MenuCommand = IpcCommand | HttpCommand;

function ipc(type: string): IpcCommand {
	return { via: 'ipc', type };
}

function http(method: HttpCommand['method'], path: HttpCommand['path'], cli?: string): HttpCommand {
	return cli === undefined ? { via: 'http', method, path } : { via: 'http', method, path, cli };
}

export const MENU_COMMANDS = {
	// Decks and transport.
	'deck.load': ipc('load'),
	'deck.unload': ipc('unload'),
	'deck.play': ipc('play'),
	'deck.loop.exit': ipc('loop'),
	'deck.beat_sync': ipc('beat_sync'),
	'deck.master': ipc('master'),
	// AGENT-50: auto_play_next_arm with out_deck, in_deck and from_ms.
	'deck.transition_out_here': ipc('auto_play_next_arm'),
	// Hot cues.
	'hot_cue.trigger': ipc('hot_cue_trigger'),
	'hot_cue.save': ipc('hot_cue_save'),
	'hot_cue.clear': ipc('hot_cue_clear'),
	// Mixer.
	'mixer.eq': ipc('eq'),
	'mixer.filter': ipc('filter'),
	'mixer.channel_cue': ipc('channel_cue'),
	'mixer.assign': ipc('assign'),
	// ANIM-01..03: a dial animation is an AGENT-03 ramp order on the command bus.
	'mixer.animate': http('POST', '/api/v1/commands', 'opendj animate <deck> <control> "<expr>"'),
	// Feedback pins (vocal-lane Fix / add comment, FB-12).
	'feedback.comment': http('POST', '/api/v1/feedback/comments'),
	// Tracks.
	'track.add_to_playlist': http('POST', '/api/v1/playlists/{playlist_id}/items:add'),
	'track.edit': http('PATCH', '/api/v1/tracks/{stable_id}'),
	// LIBUX-56: the Comment cell editor saves through the same PATCH.
	'track.comment': http('PATCH', '/api/v1/tracks/{stable_id}', 'opendj track comment <stable_id> set <text>'),
	'track.relocate': http('POST', '/api/v1/relocate/{stable_id}/apply'),
	'track.reveal': http('POST', '/api/v1/tracks/{stable_id}:reveal'),
	'track.playlists': http('GET', '/api/v1/tracks/{stable_id}/playlists'),
	'track.read': http('GET', '/api/v1/tracks/{stable_id}'),
	'track.transition_in.set': http(
		'PUT',
		'/api/v1/tracks/{stable_id}/transition-in',
		'opendj track transition-in <stable_id> set <ms>'
	),
	'track.reanalyze': http(
		'POST',
		'/api/v1/analysis/backfill/enqueue',
		'python -m apps.analysis.queue_cli enqueue --lane beatgrid --backend own_beatgrid.backfill'
	),
	'track.library_job': http('POST', '/api/v1/library-jobs/enqueue'),
	'track.stems.generate': http('POST', '/api/v1/stems/generate'),
	'track.remove_from_library': http('POST', '/api/v1/tracks/{stable_id}:remove'),
	'work.link_many': http('POST', '/api/v1/works:link'),
	'work.edit': http('PATCH', '/api/v1/tracks/{stable_id}/classical'),
	'work.link_candidates': http('GET', '/api/v1/works/link-candidates'),
	// OFFLOAD (spec e8da14b8): upload, verify, delete local; restore 24 h or for good.
	'track.offload': http('POST', '/api/v1/offload/tracks', 'opendj offload tracks <stable_id>...'),
	'track.offload_restore': http(
		'POST',
		'/api/v1/offload/restore',
		'opendj offload restore <stable_id>... --for-24h|--permanent'
	),
	'playlist.offload': http('POST', '/api/v1/offload/playlists/{playlist_id}', 'opendj offload playlist <playlist_id>'),
	'playlist.offload_restore': http(
		'POST',
		'/api/v1/offload/playlists/{playlist_id}/restore',
		'opendj offload restore-playlist <playlist_id> --for-24h|--permanent'
	),
	// PAIR-10: a loose pair (no snapshot) of exactly two selected rows.
	'track.pair': http('POST', '/api/v1/pairings'),
	// LIBUX-8679: toggle "Skip in set" on a playlist row.
	'set.skip': http(
		'PUT',
		'/api/v1/playlists/{playlist_id}/set-skips/{stable_id}',
		'opendj set skip <playlist_id> <stable_id> [off]'
	),
	// Playlists and smartlists.
	'playlist.create': http('POST', '/api/v1/playlists'),
	'playlist.update': http('PATCH', '/api/v1/playlists/{playlist_id}'),
	'playlist.delete': http('DELETE', '/api/v1/playlists/{playlist_id}'),
	'playlist.duplicate': http('POST', '/api/v1/playlists/{playlist_id}/duplicate'),
	'playlist.remove_items': http('POST', '/api/v1/playlists/{playlist_id}/items:remove'),
	'playlist.select': ipc('browser_select_playlist'),
	'section.add': http('POST', '/api/v1/sections', 'opendj api POST /api/v1/sections'),
	'section.update': http('PATCH', '/api/v1/sections/{section_id}', 'opendj api PATCH /api/v1/sections/{id}'),
	'section.delete': http('DELETE', '/api/v1/sections/{section_id}', 'opendj api DELETE /api/v1/sections/{id}'),
	'playlist_tree.order': http('PUT', '/api/v1/playlist-tree/order'),
	'smartlist.create': http('POST', '/api/v1/smartlists'),
	'smartlist.update': http('PUT', '/api/v1/smartlists/{smartlist_id}'),
	'smartlist.delete': http('DELETE', '/api/v1/smartlists/{smartlist_id}'),
	'smartlist.duplicate': http('POST', '/api/v1/smartlists/{smartlist_id}/duplicate'),
	// USB sticks (USBEXP-04).
	'usb.export.plan': http('POST', '/api/v1/usb-export/plan', 'python -m apps.sync.usb.pioneer.export_workflow plan'),
	'usb.eject': http('POST', '/api/v1/usb/eject'),
	'usb.volumes': http('GET', '/api/v1/usb/volumes')
} as const satisfies Record<string, MenuCommand>;

export type MenuCommandId = keyof typeof MENU_COMMANDS;

/** One-line `title` text naming the command an agent would send. */
export function describeCommand(id: MenuCommandId): string {
	const command: MenuCommand = MENU_COMMANDS[id];
	if (command.via === 'ipc') return `IPC ${command.type}`;
	const route = `${command.method} ${command.path}`;
	return command.cli === undefined ? route : `${route} (\`${command.cli}\`)`;
}

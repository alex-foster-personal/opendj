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
	// Hot cues.
	'hot_cue.trigger': ipc('hot_cue_trigger'),
	'hot_cue.save': ipc('hot_cue_save'),
	'hot_cue.clear': ipc('hot_cue_clear'),
	// Mixer.
	'mixer.eq': ipc('eq'),
	'mixer.filter': ipc('filter'),
	'mixer.channel_cue': ipc('channel_cue'),
	'mixer.assign': ipc('assign'),
	// Feedback pins (vocal-lane Fix / add comment, FB-12).
	'feedback.comment': http('POST', '/api/v1/feedback/comments'),
	// Tracks.
	'track.add_to_playlist': http('POST', '/api/v1/playlists/{playlist_id}/items:add'),
	'track.edit': http('PATCH', '/api/v1/tracks/{stable_id}'),
	'track.relocate': http('POST', '/api/v1/relocate/{stable_id}/apply'),
	'track.reveal': http('POST', '/api/v1/tracks/{stable_id}:reveal'),
	'track.playlists': http('GET', '/api/v1/tracks/{stable_id}/playlists'),
	'track.read': http('GET', '/api/v1/tracks/{stable_id}'),
	'track.reanalyze': http(
		'POST',
		'/api/v1/analysis/backfill/enqueue',
		'python -m apps.analysis.queue_cli enqueue --lane beatgrid --backend own_beatgrid.backfill'
	),
	'track.library_job': http('POST', '/api/v1/library-jobs/enqueue'),
	'track.stems.generate': http('POST', '/api/v1/stems/generate'),
	'track.remove_from_library': http('POST', '/api/v1/tracks/{stable_id}:remove'),
	// Playlists and smartlists.
	'playlist.create': http('POST', '/api/v1/playlists'),
	'playlist.update': http('PATCH', '/api/v1/playlists/{playlist_id}'),
	'playlist.delete': http('DELETE', '/api/v1/playlists/{playlist_id}'),
	'playlist.duplicate': http('POST', '/api/v1/playlists/{playlist_id}/duplicate'),
	'playlist.remove_items': http('POST', '/api/v1/playlists/{playlist_id}/items:remove'),
	'playlist.select': ipc('browser_select_playlist'),
	'playlist_tree.order': http('PUT', '/api/v1/playlist-tree/order'),
	'smartlist.create': http('POST', '/api/v1/smartlists'),
	'smartlist.update': http('PUT', '/api/v1/smartlists/{smartlist_id}'),
	'smartlist.delete': http('DELETE', '/api/v1/smartlists/{smartlist_id}'),
	'smartlist.duplicate': http('POST', '/api/v1/smartlists/{smartlist_id}/duplicate')
} as const satisfies Record<string, MenuCommand>;

export type MenuCommandId = keyof typeof MENU_COMMANDS;

/** One-line `title` text naming the command an agent would send. */
export function describeCommand(id: MenuCommandId): string {
	const command: MenuCommand = MENU_COMMANDS[id];
	if (command.via === 'ipc') return `IPC ${command.type}`;
	const route = `${command.method} ${command.path}`;
	return command.cli === undefined ? route : `${route} (\`${command.cli}\`)`;
}

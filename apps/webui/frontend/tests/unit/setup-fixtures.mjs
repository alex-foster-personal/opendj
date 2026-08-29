/** Fixtures shared by the setup-surface tests (setup-wizard.test.mjs,
 * first-run-overlay.test.mjs). Both harnesses stub globalThis.fetch against
 * the same engine, so the response wrapper and the health payload must stay
 * byte-identical between them -- one copy here instead of a clone in each. */

export function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

export function engineHealth() {
	return {
		status: 'ok',
		state_db: {
			path: 'data/state/state.db',
			tracks: 0,
			playlists: 0,
			pairings: 0,
			last_writer_hostname: null,
			last_writer_at: null
		},
		cloud: { lock_holder: null },
		syncthing: null,
		bind_host: '127.0.0.1',
		version: '0.1.0',
		contract_rev: 'sha256:2f6c',
		engine_version: '0.1.0',
		boot_id: 'boot-1'
	};
}

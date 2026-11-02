#!/usr/bin/env node
// A real process that honors the engine launch contract, for the shell's own
// tests: `--data-dir D --host H --port P`, answers GET /api/v1/health.
// FAKE_ENGINE_MODE picks a behavior:
//   healthy      serve health 200 (default)
//   exit         print a line and exit 3 before serving
//   never-ready  run but never listen
//   ignore-term  serve health, and ignore SIGTERM (forces SIGKILL escalation)
//   leader-exit-child-lives
//                serve health, start a child in this same process group, write
//                its pid to FAKE_ENGINE_CHILD_PID_FILE, then exit 0 as the
//                leader while the child keeps running (card EL-17-F9, #7127)
// It also prints its argv and the env names the shell must strip or set, so
// tests can read them back out of the log the shell pumped.
'use strict';
const http = require('node:http');

const args = process.argv.slice(2);
const flag = (name) => {
	const index = args.indexOf(name);
	return index === -1 ? undefined : args[index + 1];
};
const port = Number(flag('--port'));
const mode = process.env.FAKE_ENGINE_MODE || 'healthy';
const watched = [
	'MDT_DATA_DIR',
	'MDT_REKORDBOX_WRITEBACK_ENABLED',
	'MUSIC_DJ_STATE_BACKEND',
	'WEB_CONCURRENCY',
	'OPENDJ_PARENT_PID',
	'OPENDJ_ENGINE_WARN_LOG',
	'OPENDJ_ENGINE_LOG_BOOT_ID',
	'ODJ_AUDIO_BIN'
];
process.stdout.write(`fake-engine argv ${JSON.stringify(args)}\n`);
for (const name of watched) {
	process.stdout.write(`fake-engine env ${name}=${process.env[name] === undefined ? '<unset>' : process.env[name]}\n`);
}
process.stderr.write('fake-engine stderr line\n');

if (mode === 'exit') {
	process.exit(3);
}
if (mode === 'ignore-term') {
	process.on('SIGTERM', () => process.stdout.write('fake-engine ignoring SIGTERM\n'));
}
if (mode === 'never-ready') {
	setInterval(() => {}, 1000);
} else {
	http
		.createServer((req, res) => {
			if (req.url === '/api/v1/health') {
				res.writeHead(200, { 'content-type': 'application/json' });
				res.end('{"status":"ok"}');
			} else if (req.url === '/performance') {
				// Stands in for the SPA in the shell's e2e: a real loopback page.
				res.writeHead(200, { 'content-type': 'text/html' });
				res.end('<!doctype html><html><head><title>fake engine</title></head><body><p id="fake">fake engine page</p></body></html>');
			} else {
				res.writeHead(404);
				res.end();
			}
		})
		.listen(port, '127.0.0.1');
}

if (mode === 'leader-exit-child-lives' || mode === 'leader-term-child-ignores-term') {
	// Two shapes of the same hazard:
	//  - leader-exit-child-lives: the leader exits on its own, the child lives on.
	//  - leader-term-child-ignores-term: the leader honors SIGTERM and exits
	//    inside the grace, while the child ignores SIGTERM and outlives it.
	//
	// The survivor is a Node child rather than `/bin/sh -c "trap '' TERM"`: a
	// registered SIGTERM handler keeps the runtime alive on POSIX, and the same
	// file then runs on Windows too (AGENTS.md: portable by default, and no
	// literal `/bin/...` in an argv).
	//
	// The child writes the pid file itself, AFTER it installs its handler. The
	// leader must not report the pid any earlier than that, or a test can send
	// SIGTERM in the window where the handler does not exist yet and read a
	// survivor that died by accident as a survivor that was swept.
	const pidFile = process.env.FAKE_ENGINE_CHILD_PID_FILE;
	if (!pidFile) {
		process.stderr.write(`FAKE_ENGINE_CHILD_PID_FILE is required for ${mode}\n`);
		process.exit(4);
	}
	const survivorScript = [
		mode === 'leader-term-child-ignores-term' ? 'process.on("SIGTERM", () => {})' : '',
		`require('node:fs').writeFileSync(${JSON.stringify(pidFile)}, String(process.pid))`,
		'setInterval(() => {}, 1000)'
	]
		.filter((line) => line !== '')
		.join('; ');
	const survivor = require('node:child_process').spawn(process.execPath, ['-e', survivorScript], {
		stdio: 'ignore',
		env: process.env
	});
	survivor.on('error', (error) => {
		process.stderr.write(`fake-engine could not start its survivor: ${error.message}\n`);
		process.exit(5);
	});
	process.stdout.write('fake-engine child left in group\n');
	if (mode === 'leader-exit-child-lives') {
		const exitAfterMs = Number(process.env.FAKE_ENGINE_LEADER_EXIT_MS || 1500);
		setTimeout(() => process.exit(0), exitAfterMs);
	}
	if (mode === 'leader-term-child-ignores-term' && process.env.FAKE_ENGINE_LEADER_SIGTERM_DELAY_MS) {
		// A leader that honors SIGTERM slowly, inside the shell's grace: it
		// spends most of the budget before the group is swept (#7275 P2).
		const delayMs = Number(process.env.FAKE_ENGINE_LEADER_SIGTERM_DELAY_MS);
		process.on('SIGTERM', () => setTimeout(() => process.exit(0), delayMs));
	}
}

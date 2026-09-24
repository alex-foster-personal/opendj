#!/usr/bin/env node
// A real process that honors the engine launch contract, for the shell's own
// tests: `--data-dir D --host H --port P`, answers GET /api/v1/health.
// FAKE_ENGINE_MODE picks a behavior:
//   healthy      serve health 200 (default)
//   exit         print a line and exit 3 before serving
//   never-ready  run but never listen
//   ignore-term  serve health, and ignore SIGTERM (forces SIGKILL escalation)
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
	'OPENDJ_ENGINE_LOG_BOOT_ID'
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
			} else {
				res.writeHead(404);
				res.end();
			}
		})
		.listen(port, '127.0.0.1');
}

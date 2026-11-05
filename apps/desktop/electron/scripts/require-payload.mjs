// Refuses to pack a Windows build around the tracked placeholder payload.
//
// `payload/` is committed as a README only, so electron-builder would happily
// produce an app with no engine in it. The packaged shell expects
// bin/opendj-engine.cmd (engineLauncher in src/platform.ts); when it is absent
// the pack stops here instead of shipping an app that cannot start.
//
// Usage: node scripts/require-payload.mjs [payload-dir]

import * as fs from 'node:fs';
import * as path from 'node:path';

const payloadDir = path.resolve(process.argv[2] ?? path.join(import.meta.dirname, '..', 'payload'));
const launcher = path.join(payloadDir, 'bin', 'opendj-engine.cmd');

function isFile(file) {
	try {
		return fs.statSync(file).isFile();
	} catch (error) {
		if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return false;
		throw error;
	}
}

if (!isFile(launcher)) {
	console.error(
		`require-payload: ${launcher} does not exist, so this would pack an empty payload (an app with no engine). ` +
			'Build the payload first: set ODJ_PAYLOAD_SRC to the repo checkout to bundle and run `pnpm pack:win`, ' +
			'which builds it and then packs. (pack:win arrives with the engine payload PR.)'
	);
	process.exit(1);
}

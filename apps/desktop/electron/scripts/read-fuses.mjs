// Reads the Electron fuse wire out of a packaged binary and checks it against
// build.electronFuses in package.json. The binary is the source of truth: a
// config that was never applied (or applied to the wrong file) fails here.
//
// Usage: node scripts/read-fuses.mjs <path-to-Open DJ.exe | path-to-Electron Framework.framework/Electron Framework>
// On macOS the fuse wire lives in the Electron Framework binary, not in Contents/MacOS.

import * as fs from 'node:fs';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';

const SENTINEL = 'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX';
const WIRE_VERSION = 1;
const STATE_BYTES = { 48: 'disabled', 49: 'enabled', 114: 'removed' };

/** Index order of the V1 wire (config key names, as electron-builder spells them). Electron 44's binary carries 9; @electron/fuses 1.8 only names the first 8, so index 8 was read off the packaged exe. */
export const FUSE_ORDER = [
	'runAsNode',
	'enableCookieEncryption',
	'enableNodeOptionsEnvironmentVariable',
	'enableNodeCliInspectArguments',
	'enableEmbeddedAsarIntegrityValidation',
	'onlyLoadAppFromAsar',
	'loadBrowserProcessSpecificV8Snapshot',
	'grantFileProtocolExtraPrivileges',
	'wasmTrapHandlers'
];

/** fuse name -> enabled | disabled | removed, from the first sentinel in the binary; throws when there is none. */
export function readFuseWire(binary) {
	const at = binary.indexOf(SENTINEL);
	if (at === -1) throw new Error('no fuse sentinel in the binary, so it is not an Electron 12+ executable');
	const version = binary[at + SENTINEL.length];
	if (version !== WIRE_VERSION) throw new Error(`fuse wire version ${version} is not the ${WIRE_VERSION} this reader understands`);
	const length = binary[at + SENTINEL.length + 1];
	const wire = {};
	for (let index = 0; index < length; index++) {
		const name = FUSE_ORDER[index];
		if (name === undefined) throw new Error(`the binary has ${length} fuses but this reader knows ${FUSE_ORDER.length}; update FUSE_ORDER`);
		const byte = binary[at + SENTINEL.length + 2 + index];
		const state = STATE_BYTES[byte];
		if (state === undefined) throw new Error(`fuse ${name} holds unexpected byte ${byte}`);
		wire[name] = state;
	}
	return wire;
}

/** One line per fuse whose state in the binary differs from `expected` (name -> boolean); empty means hardened. */
export function fuseViolations(binary, expected) {
	const wire = readFuseWire(binary);
	return Object.entries(expected).flatMap(([name, wanted]) => {
		const actual = wire[name];
		if (actual === undefined) return [`${name} is not a fuse in this binary`];
		return actual === (wanted ? 'enabled' : 'disabled') ? [] : [`${name} is ${actual}, expected ${wanted ? 'enabled' : 'disabled'}`];
	});
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
	const exe = process.argv[2];
	if (exe === undefined) {
		console.error('read-fuses: pass the path to the packaged binary that carries the fuse wire');
		process.exit(2);
	}
	const pkg = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, '..', 'package.json'), 'utf8'));
	const binary = fs.readFileSync(exe);
	// electronFuses also carries electron-builder options that are not fuses (resetAdHocDarwinSignature).
	const expected = Object.fromEntries(Object.entries(pkg.build.electronFuses).filter(([name]) => FUSE_ORDER.includes(name)));
	const violations = fuseViolations(binary, expected);
	for (const [name, state] of Object.entries(readFuseWire(binary))) console.log(`  ${name}: ${state}`);
	if (violations.length > 0) {
		console.error(`read-fuses: ${exe} is not hardened:\n${violations.map((line) => `  ${line}`).join('\n')}`);
		process.exit(1);
	}
	console.log(`read-fuses: ${exe} matches build.electronFuses`);
}

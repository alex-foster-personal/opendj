// The post-pack identity check the Tauri dmg recipe runs (EL-17-F12): the
// payload the packed app actually ships must name the commit the shell was
// stamped with. Run after electron-builder by `pack:dir`.
//
// usage: node scripts/verify-pack.mjs [<packed payload dir> <stamp json>]

import * as fs from 'node:fs';
import * as path from 'node:path';

const root = path.resolve(import.meta.dirname, '..');
const [payloadDir, stampPath] =
	process.argv.length >= 4
		? process.argv.slice(2, 4)
		: [path.join(root, 'out', 'mac-arm64', 'Open DJ.app', 'Contents', 'Resources', 'payload'), path.join(root, 'dist', 'build-identity.json')];

if (!fs.existsSync(payloadDir)) {
	console.error(`verify-pack: no packed payload at ${payloadDir}; the pack did not produce one`);
	process.exit(1);
}
const stamp = JSON.parse(fs.readFileSync(stampPath, 'utf8'));
const manifestPath = path.join(payloadDir, 'manifest.json');
if (!fs.existsSync(manifestPath)) {
	// Only the tracked placeholder (payload/README.md alone, byte for byte) is "nothing to compare". Anything else
	// without a manifest is a pack that lost it, and must not pass as the placeholder.
	const entries = fs.readdirSync(payloadDir).sort();
	const tracked = fs.readFileSync(path.join(root, 'payload', 'README.md'));
	const isPlaceholder =
		entries.length === 1 && entries[0] === 'README.md' && fs.readFileSync(path.join(payloadDir, 'README.md')).equals(tracked);
	if (!isPlaceholder) {
		console.error(`verify-pack: ${payloadDir} has no manifest.json and is not the tracked placeholder payload (holds ${entries.join(', ') || 'nothing'})`);
		process.exit(1);
	}
	console.log(`verify-pack: UNKNOWN, placeholder payload packed; shell stamped ${stamp.git_sha} from the checkout`);
	process.exit(0);
}
const shipped = JSON.parse(fs.readFileSync(manifestPath, 'utf8')).identity?.git_sha_full;
if (shipped !== stamp.git_sha_full) {
	console.error(`verify-pack: payload says ${shipped}, shell was stamped ${stamp.git_sha_full}`);
	process.exit(1);
}
console.log(`verify-pack: [OK] payload and shell both ${stamp.git_sha}`);

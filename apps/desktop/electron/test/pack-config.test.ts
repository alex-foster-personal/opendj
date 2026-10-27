import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

const packageRoot = path.join(__dirname, '..', '..');
const pkg = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8')) as {
	scripts: { 'pack:dir': string };
	build: { files: string[]; extraResources: { from: string; to: string }[]; mac: { icon?: unknown } };
};

/** electron-builder refuses a mac icon below this edge, and would otherwise ship its stock one. */
const MIN_ICON_EDGE_PX = 512;
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

/** Width and height from a PNG's IHDR chunk; throws when the bytes are not a PNG. */
function pngSize(file: string): { width: number; height: number } {
	const bytes = fs.readFileSync(file);
	assert.ok(bytes.subarray(0, 8).equals(PNG_SIGNATURE), `${file} is not a PNG`);
	assert.equal(bytes.toString('latin1', 12, 16), 'IHDR', `${file} has no IHDR chunk`);
	return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

test('pack:dir stamps build-identity before electron-builder', () => {
	assert.match(pkg.scripts['pack:dir'], /node scripts\/stamp-build\.mjs/);
	const stampAt = pkg.scripts['pack:dir'].indexOf('stamp-build');
	const builderAt = pkg.scripts['pack:dir'].indexOf('electron-builder');
	assert.ok(stampAt !== -1 && builderAt !== -1 && stampAt < builderAt);
});

test('pack:dir checks the packed payload against the stamp after electron-builder (EL-17-F12)', () => {
	const builderAt = pkg.scripts['pack:dir'].indexOf('electron-builder');
	const verifyAt = pkg.scripts['pack:dir'].indexOf('node scripts/verify-pack.mjs');
	assert.ok(builderAt !== -1 && verifyAt > builderAt, pkg.scripts['pack:dir']);
});

test('the packaged app ships the bootstrap setup directory', () => {
	assert.ok(pkg.build.extraResources.some((entry) => entry.from === '../setup' && entry.to === 'setup'));
});

test('the packaged bundle icon is the Open DJ mark, not the stock Electron one', () => {
	const icon = pkg.build.mac.icon;
	assert.ok(typeof icon === 'string' && icon !== '', 'build.mac.icon is missing, so the bundle would carry electron.icns');
	const file = path.join(packageRoot, icon);
	assert.ok(fs.existsSync(file), `build.mac.icon points at ${icon}, which does not exist`);
	const { width, height } = pngSize(file);
	assert.equal(width, height, `${icon} is ${width}x${height}, not square`);
	assert.ok(width >= MIN_ICON_EDGE_PX, `${icon} is ${width}px wide; electron-builder needs ${MIN_ICON_EDGE_PX}px or more`);
});

test('the packaged app ships the assets the runtime dock icon reads', () => {
	const icon = pkg.build.mac.icon as string;
	const dir = path.dirname(icon);
	assert.ok(pkg.build.files.includes(`${dir}/**/*`), `build.files does not pack ${dir}/, so main.ts cannot load ${icon}`);
});

// Packs the Windows app WITH a built engine payload.
//
// `pack:dir:win` alone ships the tracked placeholder payload/ (a README). This
// wrapper points electron-builder's extraResources at a real payload built by
// `python -m scripts.build_engine_payload_win`, so the pack step is what
// populates resources/payload, not a hand copy afterwards. The payload path
// comes from the caller (ODJ_PAYLOAD_SRC) and from nowhere else; a payload
// without the launcher the shell starts, or without the audio engine, stops
// the pack rather than shipping an app that cannot boot its engine.

import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = path.resolve(import.meta.dirname, '..');
export const REQUIRED_PAYLOAD_FILES = ['bin/opendj-engine.cmd', 'bin/odj-audio.exe', 'runtime/python.exe', 'manifest.json'];

/** The electron-builder config with the payload resource pointed at `payloadSrc`; throws if it is not a complete payload. */
export function configWithPayload(baseBuild, payloadSrc) {
	const missing = REQUIRED_PAYLOAD_FILES.filter((rel) => !fs.existsSync(path.join(payloadSrc, rel)));
	if (missing.length > 0) {
		throw new Error(`${payloadSrc} is not a complete Windows payload; missing: ${missing.join(', ')}`);
	}
	const resources = baseBuild.extraResources.map((entry) => (entry.to === 'payload' ? { ...entry, from: payloadSrc } : entry));
	if (!resources.some((entry) => entry.to === 'payload' && entry.from === payloadSrc)) {
		throw new Error('package.json build.extraResources has no entry that ships to "payload"');
	}
	return { ...baseBuild, extraResources: resources };
}

/** Build an argument-preserving invocation of the package manager that launched this package script. */
export function buildPackWinInvocation(configPath, environment = process.env) {
	const packageManagerPath = environment.npm_execpath;
	if (packageManagerPath === undefined || packageManagerPath.trim() === '') {
		throw new Error('pack-win: npm_execpath is unavailable; run pnpm pack:win so pnpm supplies its resolved entrypoint.');
	}
	return {
		executable: process.execPath,
		args: [path.resolve(packageManagerPath), 'exec', 'electron-builder', '--dir', '--win', '--x64', '--config', configPath],
		options: { cwd: root, stdio: 'inherit', shell: false }
	};
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
	const payloadSrc = process.env.ODJ_PAYLOAD_SRC;
	if (payloadSrc === undefined || payloadSrc.trim() === '') {
		console.error('pack-win: ODJ_PAYLOAD_SRC must name a built payload dir (python -m scripts.build_engine_payload_win --out ...).');
		process.exit(1);
	}
	const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
	const config = configWithPayload(pkg.build, path.resolve(payloadSrc));
	const configPath = path.join(root, 'dist', 'electron-builder.win.json');
	fs.mkdirSync(path.dirname(configPath), { recursive: true });
	fs.writeFileSync(configPath, JSON.stringify({ ...config, directories: { ...config.directories, app: root } }, null, 2));
	const invocation = buildPackWinInvocation(configPath);
	const result = spawnSync(invocation.executable, invocation.args, invocation.options);
	if (result.error !== undefined) {
		throw result.error;
	}
	process.exit(result.status ?? 1);
}

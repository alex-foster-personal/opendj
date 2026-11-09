/**
 * Vite's eager `import.meta.glob`, modelled for esbuild-based test bundlers.
 *
 * Self-registering modules (the device maps in src/lib/rb/midi/maps/entries.ts and the
 * practice fragments in practice-devices.ts, #10560) discover their siblings with
 * `import.meta.glob(patterns, { eager: true })`. Vite rewrites that call at build time;
 * esbuild knows nothing about it, so the bundle would die on `import.meta.glob is not a
 * function`. This plugin performs the same rewrite: each matching file becomes a
 * namespace import, and the call becomes an object keyed by the file's relative path,
 * as Vite's is.
 *
 * Only the form those modules use is modelled: literal patterns (a string or an array),
 * one `*` in the last path segment, `!` exclusions, and exactly `{ eager: true }`.
 * Any other `import.meta.glob` call fails the bundle loudly instead of guessing.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

const GLOB_CALL = /import\.meta\.glob(?:<[^>()]*>)?\(([^)]*)\)/g;
const EAGER_CALL = /^\s*(\[[^\]]*\]|'[^']*')\s*,\s*\{\s*eager:\s*true\s*\}\s*$/;

function _patternsOf(literal) {
	const list = literal.trim().startsWith('[') ? literal.trim().slice(1, -1) : literal;
	return [...list.matchAll(/'([^']*)'/g)].map((m) => m[1]);
}

function _globToRegExp(pattern) {
	const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace('*', '[^/]*');
	return new RegExp(`^${escaped}$`);
}

/** Relative paths (as written in the pattern, e.g. `./ddj-400.ts`) the patterns select. */
export function expandEagerGlob(fromDir, patterns) {
	const keep = patterns.filter((p) => !p.startsWith('!'));
	const drop = patterns.filter((p) => p.startsWith('!')).map((p) => p.slice(1));
	const found = new Set();
	for (const pattern of keep) {
		if (!pattern.startsWith('./') || pattern.indexOf('*') !== pattern.lastIndexOf('*')) {
			throw new Error(`import.meta.glob pattern ${pattern} is not modelled by the test bundler`);
		}
		const dir = pattern.slice(0, pattern.lastIndexOf('/'));
		const matcher = _globToRegExp(pattern);
		for (const name of readdirSync(resolve(fromDir, dir))) {
			const rel = `${dir}/${name}`;
			if (matcher.test(rel) && !drop.includes(rel)) found.add(rel);
		}
	}
	return [...found].sort();
}

export const viteEagerGlobPlugin = {
	name: 'vite-eager-glob',
	setup(build) {
		build.onLoad({ filter: /\.ts$/ }, (args) => {
			const source = readFileSync(args.path, 'utf8');
			if (!source.includes('import.meta.glob(') && !source.includes('import.meta.glob<')) {
				return undefined;
			}
			const imports = [];
			const contents = source.replace(GLOB_CALL, (call, inner) => {
				const eager = inner.match(EAGER_CALL);
				if (!eager) {
					throw new Error(`${args.path}: ${call} is not an eager glob the test bundler models`);
				}
				const entries = expandEagerGlob(dirname(args.path), _patternsOf(eager[1])).map((rel) => {
					const id = `__viteEagerGlob${imports.length}`;
					imports.push(`import * as ${id} from ${JSON.stringify(rel)};`);
					return `${JSON.stringify(rel)}: ${id}`;
				});
				return `{ ${entries.join(', ')} }`;
			});
			return { contents: `${imports.join('\n')}\n${contents}`, loader: 'ts' };
		});
	}
};

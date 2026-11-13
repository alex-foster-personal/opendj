/** Capture the pre-B5 gothic golden from the pre-B5 painter itself (issues #11396, #13446).
 *
 * The oracle for the self-decoded (legacy-p99) curve must never come from the
 * code under test, so this loads render.ts from a detached checkout of
 * PRE_B5_COMMIT (main before DECKUX-38 / B5) and calls its blockHeights the way
 * its drawWaveRow did: p99 band norms, VARIANT_DEFAULT (blend) with the
 * window's beat period, and the no-grid fallback (kick) without one.
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

/** Last main commit with the pre-B5 blocks painter. */
export const PRE_B5_COMMIT = 'ef76df4099';

const ROW_W = 1200;
const WINDOW_S = 24;
const FRONTEND = fileURLToPath(new URL('../../../', import.meta.url));

/** Heights of the fixture window painted by the pre-B5 render module `render`. */
export function preB5Heights(render, fx) {
	const d = fx.detail;
	const det = { low: d.low, mid: d.mid, high: d.high, length: d.low.length };
	const norms = render.bandNormsFor({ detail: det });
	const variant = render.BLOCKS_CFG.VARIANT_DEFAULT;
	const bpb = (render.beatPeriodS(fx.beats) * (ROW_W / WINDOW_S)) / render.BLOCK_PITCH_PX;
	return {
		grid: Array.from(render.blockHeights(det, ROW_W, norms, variant, bpb)),
		noGrid: Array.from(render.blockHeights(det, ROW_W, norms, variant, null))
	};
}

/** Check out PRE_B5_COMMIT detached, load its render.ts, and capture the golden. */
export async function captureGothicGolden(fixtureUrl) {
	const git = (...args) => execFileSync('git', args, { cwd: FRONTEND, stdio: 'pipe' });
	const dir = mkdtempSync(join(tmpdir(), 'pre-b5-'));
	const tree = join(dir, 'tree');
	git('worktree', 'add', '-q', '--detach', tree, PRE_B5_COMMIT);
	try {
		const oldFrontend = join(tree, 'apps/webui/frontend');
		symlinkSync(join(FRONTEND, 'node_modules'), join(oldFrontend, 'node_modules'));
		const loader = pathToFileURL(join(oldFrontend, 'tests/unit/load-typescript.mjs')).href;
		const { loadTypeScriptModule } = await import(loader);
		const render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
		if ('blocksCurveFor' in render) throw new Error(`${PRE_B5_COMMIT} is not a pre-B5 painter`);
		return preB5Heights(render, JSON.parse(readFileSync(fixtureUrl, 'utf8')));
	} finally {
		git('worktree', 'remove', '--force', tree);
		rmSync(dir, { recursive: true, force: true });
	}
}

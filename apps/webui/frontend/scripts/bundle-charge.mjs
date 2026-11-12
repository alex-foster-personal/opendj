// scripts/bundle-charge.mjs -- write this branch's bundle budget charges before a push (#10886).
//
// `pnpm bundle:charge`, run from apps/webui/frontend on a feature branch. It measures every
// surface on this branch and at the merge base with origin/main, the same two readings the CI
// gate compares, and writes budgets/<surface>/charges/<branch>.json for each surface whose
// growth is beyond the gate's build-noise floor. The gate reads a branch-named charge as this
// PR's own (`--own`, budgets/README.md). It never edits a base.json or another PR's charge, and
// it deletes only this branch's own charge for a surface that no longer grows.
//
// Usage:
//   pnpm bundle:charge                         # build this tree, build the merge base, write charges
//   pnpm bundle:charge --skip-build            # reuse ./build instead of rebuilding this tree
//   pnpm bundle:charge --baseline <json>       # main's published measurement (bundle-measure-<sha>)
//                                              # instead of building the merge base
//   pnpm bundle:charge --current <json>        # a measurement of this tree already taken
//   pnpm bundle:charge --branch <name>         # charge file name; default: the current git branch

import { execFileSync } from 'node:child_process';
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	unlinkSync,
	writeFileSync
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BUDGETS_DIR, ceilToKiB, chargeBranchId, checkCharge } from './bundle-budget-load.mjs';

export { chargeBranchId };

// Same floor as PR_DELTA_NOISE_BYTES in scripts/bundle-budget.mjs: the gate passes a delta up to
// the PR's own charges plus this, so growth at or under it needs no charge.
export const NOISE_BYTES = 128;
const FRONTEND = fileURLToPath(new URL('../', import.meta.url));
const GATE = fileURLToPath(new URL('./bundle-budget.mjs', import.meta.url));
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// ---------------------------------------------------------------- core ---

/** `Ddd D Mon YYYY` in UTC, the charge schema's date form. */
export function chargeDate(now = new Date()) {
	return `${WEEKDAYS[now.getUTCDay()]} ${now.getUTCDate()} ${MONTHS[now.getUTCMonth()]} ${now.getUTCFullYear()}`;
}

/**
 * Pure: one verdict per surface in `current.totals`. A surface whose growth over `baseline` is
 * beyond NOISE_BYTES gets a schema-valid charge (growth ceiled to KiB); every other surface gets
 * `charge: null`. A surface missing from the baseline is new and is left to the rebaseline rule.
 */
export function planCharges({ baseline, current, branch, sha, date }) {
	return Object.entries(current.totals).map(([surface, { bytes }]) => {
		const before = baseline.totals[surface]?.bytes;
		if (before === undefined) return { surface, delta: null, charge: null };
		const delta = bytes - before;
		if (delta <= NOISE_BYTES) return { surface, delta, charge: null };
		const charge = {
			bytes: ceilToKiB(delta),
			measured_main: before,
			measured_with_pr: bytes,
			sha,
			date,
			reason: `branch ${branch} grows the ${surface} surface; measured +${delta} B gzip over main; ceiled to KiB.`,
			payback: "none planned; the growth is the change's own code on this surface"
		};
		checkCharge(`${surface}/charges/${branch}.json`, charge);
		return { surface, delta, charge };
	});
}

/** Writes or removes only `<budgets>/<surface>/charges/<branch>.json`; returns the printed lines. */
export function applyCharges(plan, { budgetsDir, branch }) {
	if (!/^[A-Za-z0-9._-]+$/.test(branch) || /^\d+$/.test(branch))
		throw new Error(
			`branch name ${branch} cannot name a charge file (letters, digits, . _ - only, not a bare PR number)`
		);
	return plan.map(({ surface, delta, charge }) => {
		const dir = join(budgetsDir, surface, 'charges');
		const path = join(dir, `${branch}.json`);
		if (charge) {
			mkdirSync(dir, { recursive: true });
			writeFileSync(path, `${JSON.stringify(charge, null, 2)}\n`);
			return `${surface} +${delta} charged`;
		}
		if (existsSync(path)) unlinkSync(path);
		if (delta === null) return `${surface} new surface, no baseline`;
		return `${surface} ${delta >= 0 ? '+' : ''}${delta} within noise`;
	});
}

// ---------------------------------------------------------------- shell ---

function _git(cwd, ...args) {
	return execFileSync('git', args, { cwd, encoding: 'utf8' }).trim();
}

function _measure(frontendDir, sha, outPath) {
	execFileSync(
		process.execPath,
		[
			GATE,
			'--budgets',
			join(frontendDir, 'budgets'),
			'--report-only',
			'--sha',
			sha,
			'--measure',
			outPath
		],
		{
			cwd: frontendDir,
			stdio: ['ignore', 'ignore', 'inherit']
		}
	);
	return JSON.parse(readFileSync(outPath, 'utf8'));
}

function _measureMergeBase(baseSha, scratch) {
	const tree = join(scratch, 'base-tree');
	_git(FRONTEND, 'worktree', 'add', '--detach', tree, baseSha);
	try {
		const dir = join(tree, 'apps/webui/frontend');
		execFileSync('pnpm', ['install', '--frozen-lockfile'], { cwd: dir, stdio: 'inherit' });
		execFileSync('pnpm', ['build'], { cwd: dir, stdio: 'inherit' });
		return _measure(dir, baseSha, join(scratch, 'base-measure.json'));
	} finally {
		_git(FRONTEND, 'worktree', 'remove', '--force', tree);
	}
}

function main(argv) {
	const value = (flag) => {
		const index = argv.indexOf(flag);
		if (index < 0) return null;
		if (argv[index + 1] === undefined || argv[index + 1].startsWith('--'))
			throw new Error(`${flag} needs a value`);
		return argv[index + 1];
	};
	const gitBranch = value('--branch') ?? _git(FRONTEND, 'rev-parse', '--abbrev-ref', 'HEAD');
	if (gitBranch === 'HEAD' || gitBranch === 'main')
		throw new Error(`run on a feature branch, not ${gitBranch}`);
	const branch = chargeBranchId(gitBranch);
	const sha = _git(FRONTEND, 'rev-parse', '--short=10', 'HEAD');
	const scratch = mkdtempSync(join(tmpdir(), 'bundle-charge-'));
	try {
		let current;
		if (value('--current')) current = JSON.parse(readFileSync(value('--current'), 'utf8'));
		else {
			if (!argv.includes('--skip-build'))
				execFileSync('pnpm', ['build'], { cwd: FRONTEND, stdio: 'inherit' });
			current = _measure(FRONTEND, sha, join(scratch, 'measure.json'));
		}
		let baseline;
		if (value('--baseline')) baseline = JSON.parse(readFileSync(value('--baseline'), 'utf8'));
		else {
			_git(FRONTEND, 'fetch', '-q', 'origin', 'main');
			const baseSha = _git(FRONTEND, 'merge-base', 'HEAD', 'origin/main');
			console.log(
				`no --baseline: building the merge base ${baseSha.slice(0, 10)} in a temp worktree`
			);
			baseline = _measureMergeBase(baseSha, scratch);
		}
		const plan = planCharges({ baseline, current, branch: gitBranch, sha, date: chargeDate() });
		for (const line of applyCharges(plan, { budgetsDir: BUDGETS_DIR, branch })) console.log(line);
	} finally {
		rmSync(scratch, { recursive: true, force: true });
	}
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
	try {
		main(process.argv.slice(2));
	} catch (error) {
		console.error(`[ERROR] ${error.message}`);
		process.exit(1);
	}
}

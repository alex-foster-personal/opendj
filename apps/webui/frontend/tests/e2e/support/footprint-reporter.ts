import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import type {
	FullConfig,
	FullResult,
	Reporter,
	Suite,
	TestCase,
	TestResult
} from '@playwright/test/reporter';

/**
 * Records when each spec file's tests ran, so the dev server's request log
 * (`vite-e2e-footprints.ts`) can be cut into one footprint per spec file
 * (TEST-BUDGET round 3). Pass it on the CLI next to the list and junit
 * reporters: `--reporter=list,junit,./tests/e2e/support/footprint-reporter.ts`.
 *
 * Output, in `$E2E_FOOTPRINTS_DIR`:
 *   - `tests.jsonl`: one line per test result, `{"spec": "tests/e2e/x.spec.ts",
 *     the path relative to the frontend root whatever the config's testDir,
 *     "start": <ms>, "end": <ms>, "status": "passed"}`. `start` and `end` are
 *     the worker's own clock for that test (`result.startTime` + `duration`),
 *     the same clock the dev server stamps requests with, on the same host.
 *   - `collected.json`: every spec file the config collected, written at
 *     `onBegin`, so a spec that never got to run is still known to the builder
 *     as collected (it is recorded unmeasured, which the selector treats as
 *     always-run).
 *
 * The reporter refuses to run without the directory variable rather than
 * writing somewhere by default: a run that collects footprints is a run the
 * caller set up for it, and a silent default would leave the Vite half off.
 */
const DIR_VARIABLE = 'E2E_FOOTPRINTS_DIR';
// support -> e2e -> tests -> the frontend root; the named gates' configs set testDir '.'.
const FRONTEND_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
const TESTS_LOG = 'tests.jsonl';
const COLLECTED = 'collected.json';

class FootprintReporter implements Reporter {
	private dir = '';

	onBegin(_config: FullConfig, suite: Suite): void {
		const dir = process.env[DIR_VARIABLE];
		if (!dir) {
			throw new Error(`footprint-reporter: ${DIR_VARIABLE} is unset; nowhere to record footprints`);
		}
		this.dir = dir;
		mkdirSync(dir, { recursive: true });
		const specs = new Set<string>();
		for (const test of suite.allTests()) specs.add(this.specOf(test));
		writeFileSync(join(dir, COLLECTED), JSON.stringify([...specs].sort(), null, 2) + '\n');
		writeFileSync(join(dir, TESTS_LOG), '');
	}

	onTestEnd(test: TestCase, result: TestResult): void {
		const start = result.startTime.getTime();
		const line = {
			spec: this.specOf(test),
			start,
			end: start + result.duration,
			status: result.status
		};
		appendFileSync(join(this.dir, TESTS_LOG), JSON.stringify(line) + '\n');
	}

	onEnd(_result: FullResult): void {
		// Nothing to flush: every line was appended as its test ended.
	}

	printsToStdio(): boolean {
		return false;
	}

	private specOf(test: TestCase): string {
		return relative(FRONTEND_ROOT, test.location.file).split('\\').join('/');
	}
}

export default FootprintReporter;

import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/job-progress.svelte.ts');
});

describe('job-progress helpers', () => {
	it('maps distinct process colors including load', () => {
		const { ANALYSIS_COLORS } = mod;
		assert.equal(ANALYSIS_COLORS.vocals, '#5b8cff');
		assert.equal(ANALYSIS_COLORS.load, '#4cc9f0');
		assert.notEqual(ANALYSIS_COLORS.load, ANALYSIS_COLORS.vocals);
		assert.notEqual(ANALYSIS_COLORS.load, ANALYSIS_COLORS.cues);
	});

	it('keeps load out of analysis dot slots', () => {
		const { ANALYSIS_DOT_SLOTS } = mod;
		assert.equal(ANALYSIS_DOT_SLOTS.includes('load'), false);
		assert.equal(ANALYSIS_DOT_SLOTS.filter((k) => k !== null).length, 9);
	});

	it('clamps progress for CSS vars (floor 2%, cap 100%)', () => {
		const { clampJobProgress, jobRowStyleVars } = mod;
		assert.equal(clampJobProgress(-1), 0.02);
		assert.equal(clampJobProgress(0), 0.02);
		assert.equal(clampJobProgress(0.5), 0.5);
		assert.equal(clampJobProgress(2), 1);
		assert.equal(clampJobProgress(Number.NaN), 0.02);
		assert.equal(
			jobRowStyleVars({ kind: 'vocals', progress: 0.4 }),
			'--job-color:#5b8cff; --job-pct:40%'
		);
		assert.equal(
			jobRowStyleVars({ kind: 'load', progress: 0 }),
			'--job-color:#4cc9f0; --job-pct:2%'
		);
	});

	it('fakeProgressAt starts low, eases toward cap, never reaches 1', () => {
		const { fakeProgressAt, FAKE_PROGRESS_START, FAKE_PROGRESS_CAP } = mod;
		assert.equal(fakeProgressAt(0), FAKE_PROGRESS_START);
		assert.equal(fakeProgressAt(-10), FAKE_PROGRESS_START);
		assert.equal(fakeProgressAt(Number.NaN), FAKE_PROGRESS_START);
		const mid = fakeProgressAt(8_000);
		assert.ok(mid > FAKE_PROGRESS_START);
		assert.ok(mid < FAKE_PROGRESS_CAP);
		const late = fakeProgressAt(60_000);
		assert.ok(late > mid);
		assert.ok(late <= FAKE_PROGRESS_CAP);
		assert.ok(late < 1);
		assert.equal(fakeProgressAt(1e12), FAKE_PROGRESS_CAP);
		assert.ok(fakeProgressAt(100, { start: 0.1, cap: 0.85, tauMs: 50 }) <= 0.85);
	});

	it('isJobStuck / sweepStuck clear after TTL and leave fresh jobs', () => {
		const { jobProgress, isJobStuck, JOB_STUCK_TTL_MS, sweepStuck } = mod;
		const sid = `ttl-${Date.now()}`;
		const started = Date.now() - JOB_STUCK_TTL_MS - 1;
		jobProgress.upsert({
			stable_id: sid,
			kind: 'load',
			phase: 'running',
			progress: 0.2,
			label: 'prefetch',
			real: false,
			started_at: started
		});
		const job = jobProgress.activeFor(sid);
		assert.ok(job);
		assert.equal(isJobStuck(job, Date.now(), JOB_STUCK_TTL_MS), true);
		assert.equal(isJobStuck(job, started + 1_000, JOB_STUCK_TTL_MS), false);
		const cleared = sweepStuck(Date.now(), JOB_STUCK_TTL_MS);
		assert.ok(cleared.some((k) => k === `load:${sid}`));
		assert.equal(jobProgress.activeFor(sid), null);

		const fresh = `fresh-${Date.now()}`;
		jobProgress.upsert({
			stable_id: fresh,
			kind: 'vocals',
			phase: 'running',
			real: false,
			started_at: Date.now()
		});
		assert.equal(sweepStuck(Date.now(), JOB_STUCK_TTL_MS).length, 0);
		assert.ok(jobProgress.activeFor(fresh));
		jobProgress.clear(fresh, 'vocals');
	});

	it('ranks running over queued; skips done/error; newer wins ties', () => {
		const { pickActiveJob } = mod;
		const sid = 'track-a';
		assert.equal(pickActiveJob([], sid), null);
		assert.equal(
			pickActiveJob(
				[{ stable_id: sid, kind: 'vocals', phase: 'done', progress: 1, updated_at: 1, started_at: 1 }],
				sid
			),
			null
		);
		assert.equal(
			pickActiveJob(
				[{ stable_id: sid, kind: 'vocals', phase: 'error', progress: 0, updated_at: 1, started_at: 1 }],
				sid
			),
			null
		);
		const queued = {
			stable_id: sid,
			kind: 'load',
			phase: 'queued',
			progress: 0.05,
			updated_at: 10,
			started_at: 10
		};
		const running = {
			stable_id: sid,
			kind: 'vocals',
			phase: 'running',
			progress: 0.3,
			updated_at: 1,
			started_at: 1
		};
		assert.equal(pickActiveJob([queued, running], sid), running);
		const older = {
			stable_id: sid,
			kind: 'stems',
			phase: 'running',
			progress: 0.2,
			updated_at: 5,
			started_at: 5
		};
		const newer = {
			stable_id: sid,
			kind: 'key',
			phase: 'running',
			progress: 0.2,
			updated_at: 9,
			started_at: 9
		};
		assert.equal(pickActiveJob([older, newer], sid), newer);
		assert.equal(
			pickActiveJob([{ ...queued, stable_id: 'other' }, running], sid),
			running
		);
	});

	it('real upserts cap below 100% until done; fake never claims 1', () => {
		const { jobProgress, FAKE_PROGRESS_CAP } = mod;
		const sid = `cap-${Date.now()}`;
		jobProgress.upsert({
			stable_id: sid,
			kind: 'vocals',
			phase: 'running',
			progress: 0.99,
			real: true
		});
		assert.ok(jobProgress.activeFor(sid).progress <= FAKE_PROGRESS_CAP);
		jobProgress.upsert({
			stable_id: sid,
			kind: 'vocals',
			phase: 'done',
			progress: 1,
			real: true
		});
		// done is not active for the row bar
		assert.equal(jobProgress.activeFor(sid), null);
		jobProgress.clear(sid, 'vocals');
	});

	it('jobSimulateAllowed respects URL flag', () => {
		const { jobSimulateAllowed } = mod;
		assert.equal(jobSimulateAllowed(''), false);
		assert.equal(jobSimulateAllowed('?simulateJob=1'), true);
		assert.equal(jobSimulateAllowed('?foo=1&simulateJob=1'), true);
	});

	it('builds ribbon from active jobs of one kind', () => {
		const { ribbonFromJobs } = mod;
		assert.equal(ribbonFromJobs([]), null);
		const r = ribbonFromJobs([
			{ stable_id: 'a', kind: 'vocals', phase: 'running', progress: 0.5, updated_at: 1, started_at: 1 },
			{ stable_id: 'b', kind: 'vocals', phase: 'queued', progress: 0, updated_at: 2, started_at: 2 },
			{ stable_id: 'c', kind: 'load', phase: 'running', progress: 0.25, updated_at: 3, started_at: 3 }
		]);
		assert.equal(r.kind, 'vocals');
		assert.equal(r.n, 2);
		assert.equal(r.label, 'vocals 1/2');
		assert.ok(Math.abs(r.progress - (0.5 + 0.02) / 2) < 1e-9);
	});
});

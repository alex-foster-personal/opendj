// requirement: SKIN-8871D
// requirement: SKIN-11086
// The /practice route starts a performance command session, so replica mixer moves land.
//
//   if a fader command is refused on /practice for want of a command session then broken
//   if uninstall leaves the command session active after the route unmounts then broken
//   if leaving /practice while it owns the engine leaves audio playing then broken
//   if leaving /practice after /performance reclaimed the engine disposes it then broken
//   if a second practice visit never disposes then broken

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

async function settleTeardown(m) {
	await m.gigRuntimeReleaseSettled();
}

function withDisposeSpy(m, run) {
	const original = m.engine.dispose;
	const counts = { dispose: 0 };
	// Node has no AudioContext: the spy counts calls without invoking the real dispose.
	m.engine.dispose = async () => {
		counts.dispose += 1;
	};
	return run(counts).finally(() => {
		m.engine.dispose = original;
	});
}

test('practice session lets a mixer move dispatch, and uninstall ends it', async () => {
	const m = await loadTypeScriptModule('tests/unit/fixtures/practice-session-entry.ts');
	globalThis.window = {};
	m.resetLibraryModeRuntimeForTest();
	try {
		// Negative control: no session, the dispatcher refuses.
		await assert.rejects(m.dispatchPerformanceCommand({ type: 'fader', deck: 1, value: 0.3 }));
		const uninstall = m.installPracticeSession();
		try {
			assert.notEqual(m.activePerformanceCommandSession(), null);
			await m.dispatchPerformanceCommand({ type: 'fader', deck: 1, value: 0.3 });
			assert.equal(m.queryPerformanceState().mixer.channels[1].fader, 0.3);
		} finally {
			uninstall();
			await settleTeardown(m);
		}
		assert.equal(m.activePerformanceCommandSession(), null);
	} finally {
		delete globalThis.window;
	}
});

test('leaving practice while it owns the engine disposes it once', async () => {
	const m = await loadTypeScriptModule('tests/unit/fixtures/practice-session-entry.ts');
	globalThis.window = {};
	m.resetLibraryModeRuntimeForTest();
	try {
		await withDisposeSpy(m, async (counts) => {
			const uninstall = m.installPracticeSession();
			uninstall();
			await settleTeardown(m);
			assert.equal(counts.dispose, 1);
			assert.equal(m.activePerformanceCommandSession(), null);
		});
	} finally {
		delete globalThis.window;
	}
});

test('leaving practice after /performance claimed the engine leaves it running', async () => {
	const m = await loadTypeScriptModule('tests/unit/fixtures/practice-session-entry.ts');
	globalThis.window = {};
	m.resetLibraryModeRuntimeForTest();
	try {
		await withDisposeSpy(m, async (counts) => {
			const uninstall = m.installPracticeSession();
			m.claimPerformanceMount();
			uninstall();
			await settleTeardown(m);
			assert.equal(counts.dispose, 0);
		});
	} finally {
		delete globalThis.window;
	}
});

test('a second practice visit still disposes', async () => {
	const m = await loadTypeScriptModule('tests/unit/fixtures/practice-session-entry.ts');
	globalThis.window = {};
	m.resetLibraryModeRuntimeForTest();
	try {
		await withDisposeSpy(m, async (counts) => {
			const uninstall1 = m.installPracticeSession();
			uninstall1();
			await settleTeardown(m);
			const uninstall2 = m.installPracticeSession();
			uninstall2();
			await settleTeardown(m);
			assert.equal(counts.dispose, 2);
		});
	} finally {
		delete globalThis.window;
	}
});

test('practice teardown dispose failure is reported like the performance route', async () => {
	const m = await loadTypeScriptModule('tests/unit/fixtures/practice-session-entry.ts');
	globalThis.window = {};
	m.resetLibraryModeRuntimeForTest();
	const originalError = console.error;
	const reported = [];
	console.error = (...args) => {
		reported.push(args);
		originalError(...args);
	};
	try {
		const original = m.engine.dispose;
		m.engine.dispose = async () => {
			throw new Error('dispose broke');
		};
		try {
			const uninstall = m.installPracticeSession();
			uninstall();
			await settleTeardown(m);
			assert.ok(
				reported.some(
					(args) =>
						typeof args[0] === 'string' && args[0].startsWith('performance unmount dispose failed')
				)
			);
		} finally {
			m.engine.dispose = original;
		}
	} finally {
		console.error = originalError;
		delete globalThis.window;
	}
});

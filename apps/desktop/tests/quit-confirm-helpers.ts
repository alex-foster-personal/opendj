/**
 * Shared helpers for INSTALL-21 real-shell quit specs.
 */
import { browser } from '@wdio/globals';

const PERFORMANCE_SESSION_KEY = 'mdt.rb.performance-session.v1';

/** Same probe protocol as real-shell-smoke Cmd+, without sending Cmd-Q on an empty session. */
export async function probeDriverDeliversMetaChords(): Promise<boolean> {
	await browser.execute(() => {
		const w = window as { __mdtKeyProbe?: string[] };
		w.__mdtKeyProbe = [];
		window.addEventListener(
			'keydown',
			(e) => w.__mdtKeyProbe?.push(`${e.metaKey ? 'Meta+' : ''}${e.key}`),
			true
		);
	});
	await browser.keys(['Meta', ',', 'Meta']);
	const probeSaw = await browser.execute(
		() => (window as { __mdtKeyProbe?: string[] }).__mdtKeyProbe ?? []
	);
	return probeSaw.some((k) => k === 'Meta+,');
}

export async function dispatchCmdQ(driverDelivers: boolean): Promise<void> {
	if (driverDelivers) {
		await browser.keys(['Meta', 'q', 'Meta']);
		return;
	}
	await browser.execute(() => {
		window.dispatchEvent(
			new KeyboardEvent('keydown', { key: 'q', metaKey: true, bubbles: true })
		);
	});
}

/**
 * When the embedded driver cannot synthesize Meta+q, Rust never sees ExitRequested.
 * Eval the same hook Rust calls after intercepting a real Cmd-Q.
 */
export async function invokeShellQuitHook(): Promise<void> {
	await browser.execute(() => {
		const hook = (globalThis as { __OPENDJ_requestQuit?: () => void }).__OPENDJ_requestQuit;
		if (typeof hook !== 'function') {
			throw new Error('__OPENDJ_requestQuit is not installed in the desktop shell webview');
		}
		hook();
	});
}

export async function requestQuitThroughShell(driverDelivers: boolean): Promise<void> {
	await dispatchCmdQ(driverDelivers);
	if (!driverDelivers) {
		await invokeShellQuitHook();
	}
}

export async function waitForAppExit(timeoutMs: number): Promise<void> {
	await browser.waitUntil(
		async () => {
			try {
				await browser.execute(() => true);
				return false;
			} catch {
				return true;
			}
		},
		{
			timeout: timeoutMs,
			timeoutMsg: 'confirmed quit did not terminate the desktop shell process'
		}
	);
}

export async function staleSessionSnapshot(ageMs: number): Promise<number | null> {
	return browser.execute(
		(key: string, age: number) => {
			const raw = window.localStorage.getItem(key);
			if (raw === null) return null;
			const parsed = JSON.parse(raw) as { captured_at_ms?: number };
			if (typeof parsed.captured_at_ms !== 'number') return null;
			const stale = Date.now() - age;
			parsed.captured_at_ms = stale;
			window.localStorage.setItem(key, JSON.stringify(parsed));
			return stale;
		},
		PERFORMANCE_SESSION_KEY,
		ageMs
	);
}

/** Enter confirms the dialog; read the flushed snapshot in the same turn before async shell exit. */
export async function confirmQuitViaEnter(): Promise<number | null> {
	return browser.execute((key: string) => {
		window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		const raw = window.localStorage.getItem(key);
		if (raw === null) return null;
		const parsed = JSON.parse(raw) as { captured_at_ms?: number };
		return typeof parsed.captured_at_ms === 'number' ? parsed.captured_at_ms : null;
	}, PERFORMANCE_SESSION_KEY);
}

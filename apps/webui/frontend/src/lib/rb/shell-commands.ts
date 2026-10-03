/**
 * Desktop-shell command poll (issue #2924, ADR-0050).
 *
 * Only the installed shell (`OPENDJ_ENGINE_ORIGIN`) consumes
 * `GET /api/v1/commands/next?consumer=shell`; browser tabs must not install
 * updates.
 */
import { API_BASE } from '$lib/api/base';
import { applyUpdate, type ApplyOutcome } from '$lib/rb/update-channel';
import { detectSurface } from '$lib/rb/usage-heartbeat';

const NEXT_PATH = '/api/v1/commands/next?consumer=shell';
const POLL_MS = 1000;

export type ShellCommandDeps = {
	applyUpdate: (
		onProgress?: (progress: import('$lib/rb/update-channel').ApplyProgress) => void
	) => Promise<ApplyOutcome>;
};

const defaultDeps = (): ShellCommandDeps => ({ applyUpdate });

type ShellNextBody = {
	id?: string;
	kind?: string;
	command?: { type?: string; available_version?: string };
};

function shellResultFromOutcome(outcome: ApplyOutcome): {
	status: 'succeeded' | 'failed';
	outcome: ApplyOutcome['kind'];
	error?: string;
} {
	if (outcome.kind === 'installed') {
		return { status: 'succeeded', outcome: 'installed' };
	}
	if (outcome.kind === 'no-update') {
		return {
			status: 'failed',
			outcome: 'no-update',
			error: 'the Tauri updater reported no update after apply was requested'
		};
	}
	return { status: 'failed', outcome: 'refused', error: outcome.reason };
}

export async function dispatchShellCommand(
	body: ShellNextBody,
	deps: ShellCommandDeps = defaultDeps()
): Promise<{ status: 'succeeded' | 'failed'; outcome: string; error?: string }> {
	const command = body.command;
	if (command == null || typeof command.type !== 'string') {
		return {
			status: 'failed',
			outcome: 'refused',
			error: 'shell command missing type'
		};
	}
	if (command.type === 'apply-update') {
		return shellResultFromOutcome(await deps.applyUpdate());
	}
	return {
		status: 'failed',
		outcome: 'refused',
		error: `unknown shell command type: ${command.type}`
	};
}

export function installShellCommandPoll(deps: ShellCommandDeps = defaultDeps()): () => void {
	if (typeof window === 'undefined') {
		return () => {};
	}
	if (detectSurface(window as Window & { OPENDJ_ENGINE_ORIGIN?: unknown }) !== 'desktop-shell') {
		return () => {};
	}

	let handling = false;

	const tick = async (): Promise<void> => {
		if (handling) return;
		handling = true;
		try {
			let response: Response;
			try {
				response = await fetch(`${API_BASE}${NEXT_PATH}`);
			} catch {
				// Engine unreachable: same class as usage-heartbeat, not an
				// unhandledrejection (Safari `Load failed`).
				return;
			}
			if (!response.ok) return;
			const body = (await response.json()) as ShellNextBody | null;
			if (body == null || typeof body.id !== 'string' || body.kind !== 'shell') {
				return;
			}
			const result = await dispatchShellCommand(body, deps);
			try {
				await fetch(`${API_BASE}/api/v1/commands/${body.id}/result`, {
					method: 'POST',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify(result)
				});
			} catch {
				return;
			}
		} finally {
			handling = false;
		}
	};

	const intervalId = setInterval(() => {
		void tick();
	}, POLL_MS);
	void tick();
	return () => clearInterval(intervalId);
}

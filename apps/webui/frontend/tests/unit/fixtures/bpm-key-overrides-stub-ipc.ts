/**
 * Stub performance-ipc for the library BPM/key cell commit tests: records each
 * command and answers with the `ok` the test sets per call.
 */

export type StubCommand = { type: string; [key: string]: unknown };

export const sent: StubCommand[] = [];
let answers: boolean[] = [];

export function answerWith(next: boolean[]): void {
	answers = [...next];
	sent.length = 0;
}

export async function runPerformanceCommandFromUi(command: StubCommand): Promise<{ ok: boolean }> {
	sent.push(command);
	return { ok: answers.length === 0 ? true : (answers.shift() as boolean) };
}

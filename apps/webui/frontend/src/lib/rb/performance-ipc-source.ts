/**
 * SETREC (#12602, (private path) section 5): the command sources the
 * typed page dispatcher accepts. A UI call that names none dispatches as
 * `user-ui`, an agent order as `agent-command`, the replay player as `replay`;
 * the recorder maps them to v2 `user` / `agent` / `replay`. Anything else is
 * refused before dispatch, so a capture point never sees a guessed source.
 */
import type { CommandSource } from './deck-stop-log';

/** Keyed by the union, so adding a CommandSource fails the type check here. */
const _SUPPORTED: Record<CommandSource, true> = {
	'user-ui': true,
	'agent-command': true,
	'autoplay-handoff': true,
	'app-command': true,
	engine: true,
	replay: true
};

/** Every source dispatchPerformanceCommand accepts, in declaration order. */
export const PAGE_COMMAND_SOURCES = Object.keys(_SUPPORTED) as readonly CommandSource[];

/** Return `source` as a CommandSource, or throw naming it and the supported list. */
export function assertPageCommandSource(source: unknown): CommandSource {
	if (typeof source === 'string' && Object.hasOwn(_SUPPORTED, source)) {
		return source as CommandSource;
	}
	throw new Error(
		`performance command source ${JSON.stringify(source)} is not supported; expected one of ${PAGE_COMMAND_SOURCES.join(', ')}`
	);
}

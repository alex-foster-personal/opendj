import type { AgentPlanStatus } from './agent-plan';

/**
 * AGENT-29: while an agent plan is scheduled or running, AutoPlay makes no master promotion and no handoff. A handoff
 * would replace the cued track or promote a third deck mid-plan, and the page does not coordinate the two.
 */
export function autoPlayYieldsToPlan(status: AgentPlanStatus | null | undefined): boolean {
	return status === 'scheduled' || status === 'running';
}

// The plan runner registers its status here, so AutoPlay reads it without importing the plan module into its bundle.
let readPlanStatus: () => AgentPlanStatus | null = () => null;

export function registerAgentPlanStatus(read: () => AgentPlanStatus | null): void {
	readPlanStatus = read;
}

/** Whether AutoPlay must stand aside right now. No registered runner means no plan can be running. */
export function autoPlayYieldsNow(): boolean {
	return autoPlayYieldsToPlan(readPlanStatus());
}

// AutoPlay registers whether one of its handoffs is loading or starting a deck, so the plan runner refuses a plan that
// would share that deck, without importing AutoPlay into its bundle.
let readHandoffInFlight: () => boolean = () => false;

export function registerAutoPlayHandoffInFlight(read: () => boolean): void {
	readHandoffInFlight = read;
}

/** Whether a handoff is in flight right now. No registered AutoPlay means none can be. */
export function autoPlayHandoffInFlight(): boolean {
	return readHandoffInFlight();
}

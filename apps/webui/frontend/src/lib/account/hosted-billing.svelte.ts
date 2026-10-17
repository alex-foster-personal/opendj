// Hosted billing state only. The network calls live in hosted-billing-api.ts,
// which the account overlay loads on mount so they stay off the initial bundle.

// The hub's lifecycle states (apps/entitlements/lifecycle.py LifecycleState).
export const LIFECYCLE_STATES = ['active', 'past_due', 'read_only', 'archived'] as const;
export type LifecycleState = (typeof LIFECYCLE_STATES)[number];

export type FeatureStanding = {
	feature_id: string;
	state: LifecycleState | null;
};

export type HostedBillingState = {
	eligible: boolean;
	provider: string | null;
	standings: FeatureStanding[];
	loading: boolean;
	error: string | null;
};

function initialState(): HostedBillingState {
	return {
		eligible: false,
		provider: null,
		standings: [],
		loading: false,
		error: null
	};
}

export const hostedBilling = $state<HostedBillingState>(initialState());

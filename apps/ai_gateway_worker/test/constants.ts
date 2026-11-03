// Shared by vitest.config.ts (bindings) and the tests (assertions).

declare module "vitest" {
  export interface ProvidedContext {
    /** Base URL of the loopback upstream started by upstream.global.ts. */
    upstreamUrl: string;
  }
}

export const TEST_ADMIN_TOKEN = "test-admin-token";
export const TEST_AIG_TOKEN = "test-aig-token";
/** free's cap admits two fixture streams (usage.cost 0.0042 each), not three. */
export const TEST_PLANS = {
  free: {
    daily_cap_usd: 0.005,
    models: ["google/gemini-3.7-flash", "test/upstream-500", "test/no-usage", "test/disconnect"],
    max_output_tokens: 256,
  },
  pro: { daily_cap_usd: 1, models: ["google/gemini-3.7-flash", "google/gemini-3.7-pro"], max_output_tokens: 4096 },
};

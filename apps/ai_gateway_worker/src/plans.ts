// The PLANS var: what each plan may spend, call and generate per UTC day.

export interface Plan {
  daily_cap_usd: number;
  models: string[];
  max_output_tokens: number;
}

export type Plans = Record<string, Plan>;

export const DEFAULT_PLAN = "free";

const isPlan = (p: unknown): p is Plan => {
  const v = p as Plan;
  return (
    typeof v === "object" &&
    v !== null &&
    typeof v.daily_cap_usd === "number" &&
    v.daily_cap_usd >= 0 &&
    Number.isInteger(v.max_output_tokens) &&
    v.max_output_tokens > 0 &&
    Array.isArray(v.models) &&
    v.models.every((m) => typeof m === "string" && m.length > 0)
  );
};

/** PLANS validated whole, or the reason it is unusable. Wrangler vars may arrive as an object or a JSON string. */
export function parsePlans(raw: unknown): Plans | string {
  let value = raw;
  if (typeof raw === "string") {
    try {
      value = JSON.parse(raw);
    } catch {
      return "PLANS is not valid JSON";
    }
  }
  if (typeof value !== "object" || value === null) return "PLANS is not an object";
  const bad = Object.entries(value).filter(([, p]) => !isPlan(p)).map(([name]) => name);
  if (bad.length) return `PLANS entries malformed: ${bad.join(", ")}`;
  if (!(DEFAULT_PLAN in value)) return `PLANS has no '${DEFAULT_PLAN}' plan`;
  return value as Plans;
}

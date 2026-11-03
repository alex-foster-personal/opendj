// Open DJ AI gateway: an OpenAI-compatible /v1/chat/completions that meters per install,
// forwarding to OpenRouter through Cloudflare AI Gateway. Refusals use the engine's
// envelope {detail: {code, message}} so apps/engine_core/assistant can surface them verbatim.
import { lastUsage } from "./meter";
import { DEFAULT_PLAN, type Plan, type Plans, parsePlans } from "./plans";
import * as store from "./store";

export interface Env {
  DB: D1Database;
  /** JSON object of plan name -> {daily_cap_usd, models, max_output_tokens}. */
  PLANS: unknown;
  /** OpenAI-compatible base, no trailing slash; /chat/completions is appended. */
  UPSTREAM_BASE_URL: string;
  /** Secret: the AI Gateway token, sent as cf-aig-authorization. */
  CF_AIG_TOKEN: string;
  /** Secret: bearer for /admin/*. */
  ADMIN_TOKEN: string;
}

const USAGE_DAYS_MAX = 90;
const UPSTREAM_ERROR_BODY_MAX = 2000;
const FORWARDED_HEADERS = ["x-title", "http-referer"];
/** What the engine's agent loop sends (apps/engine_core/assistant/agent.py) plus harmless sampling params. */
const FORWARDED_FIELDS = [
  "model",
  "messages",
  "stream",
  "tools",
  "tool_choice",
  "parallel_tool_calls",
  "temperature",
  "top_p",
  "stop",
  "seed",
];

// ----- responses -----------------------------------------------------------
const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const refuse = (status: number, code: string, message: string, extra: object = {}): Response =>
  json({ detail: { code, message, ...extra } }, status);

const bearer = (request: Request): string | null =>
  request.headers.get("authorization")?.match(/^Bearer\s+(\S+)$/i)?.[1] ?? null;

async function bodyJson(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const body = await request.json();
    return typeof body === "object" && body !== null && !Array.isArray(body) ? (body as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

// ----- config --------------------------------------------------------------
function loadConfig(env: Env): Plans | Response {
  const missing = (["UPSTREAM_BASE_URL", "CF_AIG_TOKEN", "ADMIN_TOKEN"] as const).filter((k) => !env[k]?.trim());
  if (missing.length) return refuse(503, "gateway_misconfigured", `gateway is missing ${missing.join(", ")}`);
  const plans = parsePlans(env.PLANS);
  return typeof plans === "string" ? refuse(503, "gateway_misconfigured", plans) : plans;
}

// ----- chat proxy ----------------------------------------------------------
const requestedMaxTokens = (body: Record<string, unknown>): number | null | undefined =>
  (body.max_tokens ?? body.max_completion_tokens) as number | null | undefined;

const validMaxTokens = (asked: unknown): boolean => asked == null || (Number.isInteger(asked) && (asked as number) > 0);

function forwardedBody(body: Record<string, unknown>, plan: Plan): Record<string, unknown> {
  const asked = requestedMaxTokens(body) ?? plan.max_output_tokens;
  const kept = Object.fromEntries(FORWARDED_FIELDS.filter((k) => k in body).map((k) => [k, body[k]]));
  return {
    ...kept,
    n: 1,
    max_tokens: Math.min(asked, plan.max_output_tokens),
    usage: { include: true },
  };
}

async function chatCompletions(request: Request, env: Env, ctx: ExecutionContext, plans: Plans): Promise<Response> {
  const token = bearer(request);
  const install = token ? await store.installForToken(env.DB, token) : null;
  if (!install) return refuse(401, "gateway_token_invalid", "install token is missing, unknown or revoked");
  const plan = plans[install.plan];
  if (!plan) return refuse(503, "gateway_misconfigured", `install plan '${install.plan}' is not in PLANS`);

  const body = await bodyJson(request);
  if (!body || typeof body.model !== "string") return refuse(400, "gateway_bad_request", "body must be JSON with a model");
  if (body.stream !== true) return refuse(400, "gateway_stream_required", "the gateway meters streams only; set stream: true");
  if (!validMaxTokens(requestedMaxTokens(body))) return refuse(400, "gateway_bad_request", "max_tokens must be a positive integer");
  if (!plan.models.includes(body.model)) {
    return refuse(403, "gateway_model_not_allowed", `model '${body.model}' is not on the '${install.plan}' plan`, {
      allowed_models: plan.models,
    });
  }
  const day = store.utcDay();
  const spent = await store.spendOn(env.DB, install.install_id, day);
  if (spent >= plan.daily_cap_usd) {
    return refuse(429, "gateway_daily_cap_reached", `daily cap of $${plan.daily_cap_usd} reached for ${day} (UTC)`, {
      spent_usd: spent,
      cap_usd: plan.daily_cap_usd,
    });
  }

  const headers: Record<string, string> = {
    "content-type": "application/json",
    "cf-aig-authorization": `Bearer ${env.CF_AIG_TOKEN}`,
    "cf-aig-metadata": JSON.stringify({ install: install.install_id, plan: install.plan }),
  };
  for (const name of FORWARDED_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers[name] = value;
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${env.UPSTREAM_BASE_URL.replace(/\/+$/, "")}/chat/completions`, {
      method: "POST",
      headers,
      body: JSON.stringify(forwardedBody(body, plan)),
    });
  } catch (err) {
    return refuse(502, "gateway_upstream_error", `upstream unreachable: ${String(err)}`, { upstream_status: null });
  }
  if (!upstream.ok || !upstream.body) {
    const text = (await upstream.text()).slice(0, UPSTREAM_ERROR_BODY_MAX);
    return refuse(502, "gateway_upstream_error", `upstream returned ${upstream.status}: ${text}`, {
      upstream_status: upstream.status,
    });
  }

  const [toClient, toMeter] = upstream.body.tee();
  ctx.waitUntil(
    (async () => {
      let usage: store.Usage | null = null;
      try {
        usage = await lastUsage(toMeter);
      } catch (err) {
        console.error(`upstream stream broke for ${install.install_id}: ${String(err)}`);
      }
      if (typeof usage?.cost !== "number") console.error(`unmetered stream for ${install.install_id}`);
      await store.recordUsage(env.DB, install.install_id, day, usage);
    })(),
  );
  return new Response(toClient, {
    headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
  });
}

// ----- admin ---------------------------------------------------------------
async function adminAuthorized(request: Request, env: Env): Promise<boolean> {
  const given = bearer(request);
  if (!given) return false;
  const enc = (s: string) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  const [a, b] = await Promise.all([enc(given), enc(env.ADMIN_TOKEN)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

const installNotFound = (id: string) => refuse(404, "gateway_install_not_found", `no install '${id}'`);

async function admin(request: Request, env: Env, plans: Plans, path: string[]): Promise<Response> {
  if (!(await adminAuthorized(request, env))) return refuse(401, "gateway_admin_unauthorized", "admin token required");
  const [, resource, id, action] = path;
  if (resource !== "installs") return refuse(404, "gateway_not_found", "no such admin route");

  if (request.method === "POST" && !id) {
    const body = await bodyJson(request);
    if (!body) return refuse(400, "gateway_bad_request", "body must be a JSON object");
    if (body.plan != null && typeof body.plan !== "string") return refuse(400, "gateway_bad_request", "plan must be a string");
    if (body.label != null && typeof body.label !== "string") return refuse(400, "gateway_bad_request", "label must be a string");
    const plan = (body.plan as string | null | undefined) ?? DEFAULT_PLAN;
    if (!plans[plan]) return refuse(400, "gateway_unknown_plan", `plan '${plan}' is not in PLANS`);
    const label = (body.label as string | null | undefined) ?? null;
    return json(await store.mintInstall(env.DB, plan, label), 201);
  }
  if (!id) return refuse(405, "gateway_method_not_allowed", "use POST /admin/installs to mint");

  if (request.method === "POST" && action === "revoke") {
    const install = await store.revokeInstall(env.DB, id);
    return install ? json(install) : installNotFound(id);
  }
  if (request.method === "PUT" && action === "plan") {
    const body = await bodyJson(request);
    const plan = body?.plan;
    if (typeof plan !== "string" || !plans[plan]) return refuse(400, "gateway_unknown_plan", `plan '${plan}' is not in PLANS`);
    const install = await store.setPlan(env.DB, id, plan);
    return install ? json(install) : installNotFound(id);
  }
  if (request.method === "GET" && action === "usage") {
    const days = Number(new URL(request.url).searchParams.get("days") ?? "7");
    if (!Number.isInteger(days) || days < 1 || days > USAGE_DAYS_MAX) {
      return refuse(400, "gateway_bad_request", `days must be an integer 1..${USAGE_DAYS_MAX}`);
    }
    const install = await store.getInstall(env.DB, id);
    if (!install) return installNotFound(id);
    const since = store.utcDay(new Date(Date.now() - (days - 1) * 86_400_000));
    return json({ ...install, days: await store.recentSpend(env.DB, id, since) });
  }
  return refuse(404, "gateway_not_found", "no such admin route");
}

// ----- entry ---------------------------------------------------------------
export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const config = loadConfig(env);
    if (config instanceof Response) return config;
    const { pathname } = new URL(request.url);
    if (request.method === "POST" && pathname === "/v1/chat/completions") {
      return chatCompletions(request, env, ctx, config);
    }
    const path = pathname.split("/").filter(Boolean);
    if (path[0] === "admin") return admin(request, env, config, path);
    return refuse(404, "gateway_not_found", `no route ${request.method} ${pathname}`);
  },
} satisfies ExportedHandler<Env>;

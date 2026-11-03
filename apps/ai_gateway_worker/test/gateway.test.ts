// The Worker in the real Workers runtime (workerd via Miniflare) with a real local D1, calling a
// real loopback upstream started by upstream.global.ts. No mock functions anywhere.
import { createExecutionContext, env, waitOnExecutionContext } from "cloudflare:test";
import { describe, expect, inject, it } from "vitest";
import worker, { type Env } from "../src/index";
import { TEST_ADMIN_TOKEN, TEST_AIG_TOKEN, TEST_PLANS } from "./constants";
import FRAMES from "./fixtures/openrouter-stream.sse?raw";

const BASE = "https://gateway.test";
const FLASH = "google/gemini-3.7-flash";
const PRO_ONLY = "google/gemini-3.7-pro";
const FIXTURE_USAGE = JSON.parse(FRAMES.split("\n").find((l) => l.includes('"usage"'))!.slice(5)).usage;

/** One agent round exactly as apps/engine_core/assistant/agent.py builds it: history with a tool round, plus tools. */
const ENGINE_AGENT_BODY = {
  model: FLASH,
  stream: true,
  messages: [
    { role: "system", content: "You are the Open DJ assistant." },
    { role: "user", content: "How many tracks are in my library?" },
    {
      role: "assistant",
      content: "",
      tool_calls: [{ id: "call_1", type: "function", function: { name: "library_stats", arguments: "{}" } }],
    },
    { role: "tool", tool_call_id: "call_1", content: '{"tracks": 8355}' },
  ],
  tools: [
    {
      type: "function",
      function: {
        name: "library_stats",
        description: "Counts of tracks, playlists and analysed tracks in the library.",
        parameters: { type: "object", properties: {}, additionalProperties: false },
      },
    },
  ],
};

// ----- helpers -------------------------------------------------------------
async function call(request: Request, overrides: Partial<Env> = {}) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(request, { ...env, ...overrides }, ctx);
  const text = await res.text();
  await waitOnExecutionContext(ctx);
  return { status: res.status, headers: res.headers, text, json: () => JSON.parse(text) };
}

const admin = (method: string, path: string, body?: unknown, token = TEST_ADMIN_TOKEN) =>
  call(
    new Request(`${BASE}/admin${path}`, {
      method,
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );

async function mint(plan?: string): Promise<{ install_id: string; token: string }> {
  const res = await admin("POST", "/installs", plan ? { plan, label: "vitest" } : {});
  expect(res.status).toBe(201);
  return res.json();
}

const chat = (token: string | null, body: Record<string, unknown> = {}) =>
  call(
    new Request(`${BASE}/v1/chat/completions`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-title": "Open DJ",
        "http-referer": "https://example.invalid/open-dj",
        ...(token ? { authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ model: FLASH, stream: true, messages: [{ role: "user", content: "hi" }], ...body }),
    }),
  );

const mintRaw = (body: string) =>
  call(
    new Request(`${BASE}/admin/installs`, {
      method: "POST",
      headers: { authorization: `Bearer ${TEST_ADMIN_TOKEN}`, "content-type": "application/json" },
      body,
    }),
  );

const usage = async (installId: string) => (await admin("GET", `/installs/${installId}/usage?days=1`)).json();

const lastSeen = async () => {
  const seen = (await (await fetch(`${inject("upstreamUrl")}/seen`)).json()) as {
    headers: Record<string, string>;
    body: Record<string, unknown>;
  }[];
  return seen.at(-1)!;
};

const code = (res: { json: () => { detail: { code: string } } }) => res.json().detail.code;

// ----- chat proxy ----------------------------------------------------------
describe("POST /v1/chat/completions", () => {
  it("streams the upstream frames byte-for-byte and meters the usage chunk's cost", async () => {
    const { install_id, token } = await mint();
    const res = await chat(token);
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    expect(res.text).toBe(FRAMES);
    const [day] = (await usage(install_id)).days;
    expect(day).toMatchObject({
      cost_usd: FIXTURE_USAGE.cost,
      requests: 1,
      prompt_tokens: FIXTURE_USAGE.prompt_tokens,
      completion_tokens: FIXTURE_USAGE.completion_tokens,
      unmetered: 0,
    });
  });

  it("forwards the engine's agent request (tools included) through an allow-list, pinning n and clamping max_tokens", async () => {
    const { token } = await mint();
    const smuggled = {
      n: 5,
      functions: [{ name: "legacy" }],
      function_call: "auto",
      response_format: { type: "json_object" },
      stream_options: { include_usage: false },
      logit_bias: { "50256": 100 },
      max_completion_tokens: 100_000,
      provider: { order: ["anything"] },
    };
    const sampling = { tool_choice: "auto", parallel_tool_calls: false, temperature: 0.2, top_p: 0.9, stop: ["\n\n"], seed: 7 };
    expect((await chat(token, { ...ENGINE_AGENT_BODY, ...sampling, ...smuggled })).status).toBe(200);
    const { body } = await lastSeen();
    expect(body).toEqual({
      ...ENGINE_AGENT_BODY,
      ...sampling,
      n: 1,
      max_tokens: TEST_PLANS.free.max_output_tokens,
      usage: { include: true },
    });
  });

  it("forwards gateway auth and install metadata, never the install token", async () => {
    const { install_id, token } = await mint();
    await chat(token);
    const { headers } = await lastSeen();
    expect(headers["cf-aig-authorization"]).toBe(`Bearer ${TEST_AIG_TOKEN}`);
    expect(JSON.parse(headers["cf-aig-metadata"])).toEqual({ install: install_id, plan: "free" });
    expect(headers["x-title"]).toBe("Open DJ");
    expect(headers["http-referer"]).toBe("https://example.invalid/open-dj");
    expect(headers.authorization).toBeUndefined();
  });

  it("keeps a smaller requested max_tokens", async () => {
    const { token } = await mint();
    await chat(token, { max_tokens: 32 });
    expect((await lastSeen()).body.max_tokens).toBe(32);
  });

  it("refuses an invalid max_tokens rather than replacing it with the plan cap", async () => {
    const { token } = await mint();
    for (const max_tokens of [0, -1, 1.5, "32"]) {
      const res = await chat(token, { max_tokens });
      expect(res.status).toBe(400);
      expect(code(res)).toBe("gateway_bad_request");
    }
    const viaAlias = await chat(token, { max_completion_tokens: 0 });
    expect(code(viaAlias)).toBe("gateway_bad_request");
  });

  it("refuses 401 for a missing, unknown or revoked token", async () => {
    expect(code(await chat(null))).toBe("gateway_token_invalid");
    expect((await chat("odj_not-a-real-token")).status).toBe(401);
    const { install_id, token } = await mint();
    expect((await chat(token)).status).toBe(200);
    expect((await admin("POST", `/installs/${install_id}/revoke`)).json().revoked_at).toBeTruthy();
    const refused = await chat(token);
    expect(refused.status).toBe(401);
    expect(code(refused)).toBe("gateway_token_invalid");
  });

  it("refuses 403 for a model off the plan's allow-list, and the plan seam lifts it", async () => {
    const { install_id, token } = await mint();
    const refused = await chat(token, { model: PRO_ONLY });
    expect(refused.status).toBe(403);
    expect(refused.json().detail).toMatchObject({ code: "gateway_model_not_allowed", allowed_models: TEST_PLANS.free.models });
    expect((await admin("PUT", `/installs/${install_id}/plan`, { plan: "pro" })).json().plan).toBe("pro");
    expect((await chat(token, { model: PRO_ONLY })).status).toBe(200);
  });

  it("refuses 429 once the UTC day's spend reaches the plan cap", async () => {
    const { token } = await mint();
    expect((await chat(token)).status).toBe(200);
    expect((await chat(token)).status).toBe(200);
    const refused = await chat(token);
    expect(refused.status).toBe(429);
    expect(refused.json().detail).toMatchObject({
      code: "gateway_daily_cap_reached",
      cap_usd: TEST_PLANS.free.daily_cap_usd,
      spent_usd: 2 * FIXTURE_USAGE.cost,
    });
  });

  it("reports an upstream failure as 502 with its status and meters nothing", async () => {
    const { install_id, token } = await mint();
    const res = await chat(token, { model: "test/upstream-500" });
    expect(res.status).toBe(502);
    expect(res.json().detail).toMatchObject({ code: "gateway_upstream_error", upstream_status: 500 });
    expect(res.json().detail.message).toContain("provider down");
    expect((await usage(install_id)).days).toEqual([]);
  });

  it("counts a stream that ends without a usage chunk as unmetered, not free", async () => {
    const { install_id, token } = await mint();
    expect((await chat(token, { model: "test/no-usage" })).status).toBe(200);
    expect((await usage(install_id)).days[0]).toMatchObject({ cost_usd: 0, requests: 1, unmetered: 1 });
  });

  it("records an upstream that drops the socket mid-stream (no [DONE]) as an unmetered call", async () => {
    const { install_id, token } = await mint();
    const ctx = createExecutionContext();
    const request = new Request(`${BASE}/v1/chat/completions`, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
      body: JSON.stringify({ model: "test/disconnect", stream: true, messages: [{ role: "user", content: "hi" }] }),
    });
    const res = await worker.fetch(request, env, ctx);
    expect(res.status).toBe(200);
    await expect(res.text()).rejects.toThrow();
    await waitOnExecutionContext(ctx);
    expect((await usage(install_id)).days[0]).toMatchObject({ cost_usd: 0, requests: 1, unmetered: 1 });
  });

  it("refuses a non-streaming or modelless body with 400", async () => {
    const { token } = await mint();
    expect(code(await chat(token, { stream: false }))).toBe("gateway_stream_required");
    expect(code(await chat(token, { model: 7 }))).toBe("gateway_bad_request");
  });
});

// ----- admin and config ----------------------------------------------------
describe("admin endpoints", () => {
  it("refuse without the admin token", async () => {
    expect(code(await admin("POST", "/installs", {}, "wrong"))).toBe("gateway_admin_unauthorized");
  });

  it("mint defaults to the free plan and refuses an unknown plan", async () => {
    const minted = (await admin("POST", "/installs", {})).json();
    expect(minted).toMatchObject({ plan: "free", label: null });
    expect(minted.token).toMatch(/^odj_[0-9a-f]{64}$/);
    expect(code(await admin("POST", "/installs", { plan: "gold" }))).toBe("gateway_unknown_plan");
  });

  it("mint refuses a malformed body or a wrongly typed field, and mints nothing", async () => {
    for (const body of ["{not json", "[]", '{"plan": 1}', '{"label": 7}']) {
      const res = await mintRaw(body);
      expect(res.status).toBe(400);
      expect(code(res)).toBe("gateway_bad_request");
    }
  });

  it("404 for an unknown install", async () => {
    expect(code(await admin("GET", "/installs/inst_nope/usage"))).toBe("gateway_install_not_found");
    expect(code(await admin("POST", "/installs/inst_nope/revoke"))).toBe("gateway_install_not_found");
  });

  it("stores only the token hash", async () => {
    const { install_id, token } = await mint();
    const row = await env.DB.prepare("SELECT * FROM install_tokens WHERE install_id = ?").bind(install_id).first<Record<string, string>>();
    expect(Object.values(row!)).not.toContain(token);
    expect(row!.token_hash).toMatch(/^[0-9a-f]{64}$/);
  });
});

describe("configuration", () => {
  it("refuses 503 when PLANS lacks the free plan or a secret is unset", async () => {
    const noFree = await call(new Request(`${BASE}/v1/chat/completions`, { method: "POST" }), { PLANS: { pro: TEST_PLANS.pro } });
    expect(noFree.status).toBe(503);
    expect(noFree.json().detail.message).toContain("'free'");
    const noSecret = await call(new Request(`${BASE}/admin/installs`, { method: "POST" }), { CF_AIG_TOKEN: "" });
    expect(noSecret.json().detail).toMatchObject({ code: "gateway_misconfigured", message: "gateway is missing CF_AIG_TOKEN" });
  });
});

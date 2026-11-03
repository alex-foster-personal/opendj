// D1 access: install tokens (hashed) and per-install UTC-day spend.

export interface Install {
  install_id: string;
  plan: string;
  label: string | null;
  created_at: string;
  revoked_at: string | null;
}

export interface DaySpend {
  day: string;
  cost_usd: number;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  unmetered: number;
}

export interface Usage {
  cost?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
}

const INSTALL_COLS = "install_id, plan, label, created_at, revoked_at";
const SPEND_COLS = "day, cost_usd, requests, prompt_tokens, completion_tokens, unmetered";

// ----- helpers -------------------------------------------------------------
export const utcDay = (at: Date = new Date()): string => at.toISOString().slice(0, 10);

const nowIso = (): string => new Date().toISOString();

const randomHex = (bytes: number): string =>
  [...crypto.getRandomValues(new Uint8Array(bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ----- install tokens ------------------------------------------------------
export async function mintInstall(db: D1Database, plan: string, label: string | null) {
  const install_id = `inst_${randomHex(12)}`;
  const token = `odj_${randomHex(32)}`;
  const created_at = nowIso();
  await db
    .prepare("INSERT INTO install_tokens (install_id, token_hash, plan, label, created_at) VALUES (?, ?, ?, ?, ?)")
    .bind(install_id, await sha256Hex(token), plan, label, created_at)
    .run();
  return { install_id, token, plan, label, created_at };
}

/** The live (unrevoked) install a raw bearer token belongs to, or null. */
export async function installForToken(db: D1Database, token: string): Promise<Install | null> {
  return db
    .prepare(`SELECT ${INSTALL_COLS} FROM install_tokens WHERE token_hash = ? AND revoked_at IS NULL`)
    .bind(await sha256Hex(token))
    .first<Install>();
}

export async function getInstall(db: D1Database, installId: string): Promise<Install | null> {
  return db.prepare(`SELECT ${INSTALL_COLS} FROM install_tokens WHERE install_id = ?`).bind(installId).first<Install>();
}

export async function revokeInstall(db: D1Database, installId: string): Promise<Install | null> {
  await db
    .prepare("UPDATE install_tokens SET revoked_at = ? WHERE install_id = ? AND revoked_at IS NULL")
    .bind(nowIso(), installId)
    .run();
  return getInstall(db, installId);
}

export async function setPlan(db: D1Database, installId: string, plan: string): Promise<Install | null> {
  await db.prepare("UPDATE install_tokens SET plan = ? WHERE install_id = ?").bind(plan, installId).run();
  return getInstall(db, installId);
}

// ----- spend ---------------------------------------------------------------
export async function spendOn(db: D1Database, installId: string, day: string): Promise<number> {
  const row = await db
    .prepare("SELECT cost_usd FROM daily_spend WHERE install_id = ? AND day = ?")
    .bind(installId, day)
    .first<{ cost_usd: number }>();
  return row?.cost_usd ?? 0;
}

export async function recentSpend(db: D1Database, installId: string, sinceDay: string): Promise<DaySpend[]> {
  const { results } = await db
    .prepare(`SELECT ${SPEND_COLS} FROM daily_spend WHERE install_id = ? AND day >= ? ORDER BY day DESC`)
    .bind(installId, sinceDay)
    .all<DaySpend>();
  return results;
}

/** Add one finished stream to the day's row; a missing cost counts as unmetered, never as free. */
export async function recordUsage(db: D1Database, installId: string, day: string, usage: Usage | null) {
  const metered = typeof usage?.cost === "number";
  await db
    .prepare(
      `INSERT INTO daily_spend (install_id, day, cost_usd, requests, prompt_tokens, completion_tokens, unmetered)
       VALUES (?, ?, ?, 1, ?, ?, ?)
       ON CONFLICT (install_id, day) DO UPDATE SET
         cost_usd = cost_usd + excluded.cost_usd,
         requests = requests + 1,
         prompt_tokens = prompt_tokens + excluded.prompt_tokens,
         completion_tokens = completion_tokens + excluded.completion_tokens,
         unmetered = unmetered + excluded.unmetered`,
    )
    .bind(
      installId,
      day,
      metered ? usage!.cost! : 0,
      usage?.prompt_tokens ?? 0,
      usage?.completion_tokens ?? 0,
      metered ? 0 : 1,
    )
    .run();
}

-- Install tokens are stored only as SHA-256 hex; the raw token is shown once at mint.
CREATE TABLE install_tokens (
  install_id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  plan TEXT NOT NULL DEFAULT 'free',
  label TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);

-- One row per install per UTC day. unmetered counts streams that ended without a usage chunk.
CREATE TABLE daily_spend (
  install_id TEXT NOT NULL REFERENCES install_tokens (install_id),
  day TEXT NOT NULL,
  cost_usd REAL NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  unmetered INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (install_id, day)
);

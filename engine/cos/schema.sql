-- Chief of Staff, local state. Everything else is a view onto this.
PRAGMA journal_mode = WAL;

-- ── raw source material ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,          -- outlook_inbox | outlook_sent | notion_call
  source_id     TEXT NOT NULL,          -- Outlook EntryID / Notion page id
  ts            TEXT NOT NULL,          -- ISO8601, when the doc happened
  title         TEXT,
  participants  TEXT,                   -- JSON list of {name,email,role}
  direction     TEXT,                   -- in | out | meeting
  body          TEXT,
  meta          TEXT,                   -- JSON (conversation id, folder, url…)
  ingested_at   TEXT NOT NULL,
  processed_at  TEXT,                   -- NULL = extraction still owes us
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_doc_ts        ON documents(ts);
CREATE INDEX IF NOT EXISTS ix_doc_unproc    ON documents(processed_at) WHERE processed_at IS NULL;

-- ── entities: the thing that makes context free at dispatch time ───────
CREATE TABLE IF NOT EXISTS entities (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL,         -- company | person
  canonical_name TEXT NOT NULL,
  domain         TEXT,
  affinity_id    TEXT,
  meta           TEXT,
  created_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_entity ON entities(kind, canonical_name);

-- surface form -> entity. Seeded from Affinity, grown by your corrections.
CREATE TABLE IF NOT EXISTS aliases (
  id         INTEGER PRIMARY KEY,
  surface    TEXT NOT NULL,             -- normalized, lowercase
  entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  origin     TEXT NOT NULL,             -- seed_affinity | correction | auto
  created_at TEXT NOT NULL,
  UNIQUE(surface, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_alias_surface ON aliases(surface);

-- ── commitments ────────────────────────────────────────────────────────
-- kind distinguishes "I owe" from "I am owed". Both are first-class.
CREATE TABLE IF NOT EXISTS tasks (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,           -- todo | waiting_on
  title        TEXT NOT NULL,
  detail       TEXT,
  promiser     TEXT,                    -- who owes it
  promisee     TEXT,                    -- who is owed
  status       TEXT NOT NULL DEFAULT 'open',
                                        -- open | likely_done | done | dismissed | snoozed
  due          TEXT,
  snooze_until TEXT,
  confidence   REAL,                    -- extractor's own confidence 0-1
  skill        TEXT,                    -- matched skill, e.g. add-to-affinity
  first_seen   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  closed_at    TEXT,
  close_reason TEXT                     -- user_confirmed | inferred | dismissed
);
CREATE INDEX IF NOT EXISTS ix_task_status ON tasks(status);

-- many rows per task: the same commitment restated across call + email + chat
CREATE TABLE IF NOT EXISTS task_evidence (
  id          INTEGER PRIMARY KEY,
  task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  quote       TEXT,
  role        TEXT NOT NULL,            -- origin | restated | completion
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ev_task ON task_evidence(task_id);

CREATE TABLE IF NOT EXISTS task_entities (
  task_id   INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, entity_id)
);

-- ── corrections: fix this item, AND teach the rule ─────────────────────
CREATE TABLE IF NOT EXISTS corrections (
  id      INTEGER PRIMARY KEY,
  ts      TEXT NOT NULL,
  channel TEXT NOT NULL,                -- email_reply | cos_cli
  raw     TEXT NOT NULL,                -- exactly what you typed
  parsed  TEXT,                         -- JSON list of operations
  applied INTEGER NOT NULL DEFAULT 0
);

-- durable rules distilled from corrections; fed back into the extractor
CREATE TABLE IF NOT EXISTS rules (
  id                   INTEGER PRIMARY KEY,
  kind                 TEXT NOT NULL,   -- disambiguation | never_task | preference
  text                 TEXT NOT NULL,
  active               INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT NOT NULL,
  source_correction_id INTEGER REFERENCES corrections(id)
);

-- ── briefs: numbering must survive so "1 3 done" resolves correctly ────
CREATE TABLE IF NOT EXISTS briefs (
  id      INTEGER PRIMARY KEY,
  ts      TEXT NOT NULL,
  subject TEXT,
  html    TEXT,
  sent    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS brief_items (
  brief_id INTEGER NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
  num      INTEGER NOT NULL,
  task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  section  TEXT NOT NULL,
  PRIMARY KEY (brief_id, num)
);

-- one row per reply already applied, so re-ingesting Sent can't double-apply it
CREATE TABLE IF NOT EXISTS reply_log (
  document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  brief_id    INTEGER REFERENCES briefs(id) ON DELETE SET NULL,
  applied_at  TEXT NOT NULL
);

-- ── incremental ingest bookkeeping ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS watermarks (
  source     TEXT PRIMARY KEY,
  last_ts    TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id       INTEGER PRIMARY KEY,
  started  TEXT NOT NULL,
  finished TEXT,
  kind     TEXT,                        -- ingest | brief | reply
  stats    TEXT,
  error    TEXT
);

-- ── work journal ───────────────────────────────────────────────────────
-- Appended whenever anything ACTS on a task: the /cos skill picking it up,
-- a note you dictate, a close. Separate from `detail` (what the task IS) and
-- from `task_evidence` (what a SOURCE said). Without it, work done inside a
-- Claude session leaves no trace and tomorrow's brief re-asks a solved question.
CREATE TABLE IF NOT EXISTS task_log (
  id      INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  ts      TEXT NOT NULL,
  actor   TEXT NOT NULL,               -- cos_skill | user | pipeline
  action  TEXT NOT NULL,               -- worked | note | done | due | snooze | dismissed | created | reopened
  text    TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasklog_task ON task_log(task_id, id);

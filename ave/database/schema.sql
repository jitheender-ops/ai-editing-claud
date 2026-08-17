-- Seven tables. The spec listed fifteen, but `reference_analysis`, `edit_dna`,
-- `media_analysis` and `transcripts` are all the same thing reached by different
-- paths: one row of JSON keyed by (media, analyzer kind, analyzer version). And
-- `edit_style_versions` is a column, not a table.
--
-- Times are ISO-8601 text; SQLite has no date type and text sorts correctly.
-- `run_after` is the exception: epoch milliseconds, because the queue does
-- arithmetic on it.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS media (
  id            TEXT PRIMARY KEY,          -- med_...
  path          TEXT NOT NULL,
  content_hash  TEXT NOT NULL,             -- size + head/tail 4MB, not a full read
  kind          TEXT NOT NULL,             -- reference | source | asset
  probe         TEXT NOT NULL,             -- JSON: ffprobe output
  proxy_path    TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(content_hash, kind)
);
CREATE INDEX IF NOT EXISTS idx_media_hash ON media(content_hash);

-- THE cache. Never analyse unchanged media twice. Bump analyzer_version to
-- invalidate one analyzer's results without touching the others.
CREATE TABLE IF NOT EXISTS analysis (
  id                TEXT PRIMARY KEY,
  media_id          TEXT NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  kind              TEXT NOT NULL,         -- scenes|transcript|motion|color|audio|ocr|faces
  analyzer_version  TEXT NOT NULL,
  data              TEXT NOT NULL,         -- JSON
  duration_ms       INTEGER,
  created_at        TEXT NOT NULL,
  UNIQUE(media_id, kind, analyzer_version)
);

CREATE TABLE IF NOT EXISTS styles (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  version             INTEGER NOT NULL DEFAULT 1,
  parent_id           TEXT REFERENCES styles(id),   -- lineage for merged styles
  dna                 TEXT NOT NULL,                -- JSON: EditDNA
  dna_schema_version  TEXT NOT NULL,
  category            TEXT,                         -- free text, deliberately not an enum
  tags                TEXT NOT NULL DEFAULT '[]',
  source_media_ids    TEXT NOT NULL DEFAULT '[]',
  confidence          TEXT NOT NULL DEFAULT '{}',
  rating              INTEGER,
  created_at          TEXT NOT NULL,
  UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS projects (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  style_id     TEXT REFERENCES styles(id),
  footage_dir  TEXT,
  target       TEXT NOT NULL,                 -- JSON: platform, duration, aspect, fps
  autonomy     INTEGER NOT NULL DEFAULT 2,    -- 1 propose | 2 build+flag | 3 autonomous
  created_at   TEXT NOT NULL
);

-- Immutable and append-only. This IS AI_EDIT_v001/v002/v003; you can always revert
-- because nothing is ever overwritten.
CREATE TABLE IF NOT EXISTS plans (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version         INTEGER NOT NULL,
  parent_version  INTEGER,
  seed            INTEGER NOT NULL,           -- regeneration is byte-identical
  edl             TEXT NOT NULL,              -- JSON
  qc              TEXT,                       -- JSON: Edit Quality Report
  origin          TEXT NOT NULL,              -- generate | feedback | manual
  origin_detail   TEXT,                       -- the natural-language command, if any
  run_id          TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  UNIQUE(project_id, version)
);

-- Ops the validation gate returned REQUIRE_APPROVAL for. The point of this table
-- is that an uncertain op is neither silently applied nor silently dropped.
CREATE TABLE IF NOT EXISTS approvals (
  id          TEXT PRIMARY KEY,
  plan_id     TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  op_id       TEXT NOT NULL,
  reasons     TEXT NOT NULL,                     -- JSON: OpReason[]
  state       TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING | APPROVED | REJECTED
  decided_at  TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_queue (
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  payload         TEXT NOT NULL,                 -- JSON
  status          TEXT NOT NULL DEFAULT 'READY', -- READY | RUNNING | DONE | DEAD
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  run_after       INTEGER NOT NULL,              -- epoch ms; drives backoff
  correlation_id  TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON job_queue(status, run_after);

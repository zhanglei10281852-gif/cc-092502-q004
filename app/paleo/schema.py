"""植物遗存分析模块的数据库结构。

所有表通过 database.init_db 与基础结构一并创建，保持幂等。
"""
from __future__ import annotations

PALEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_samples (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 sample_code TEXT NOT NULL,
 context_type TEXT NOT NULL CHECK(context_type IN ('paleochannel','dwelling','ash_pit','other')),
 context_label TEXT NOT NULL DEFAULT '',
 collected_at TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id,sample_code)
);
CREATE TABLE IF NOT EXISTS bot_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_code TEXT NOT NULL,
 sample_id INTEGER NOT NULL REFERENCES bot_samples(id) ON DELETE CASCADE,
 volume_liters REAL NOT NULL CHECK(volume_liters >= 0),
 floated_at TEXT NOT NULL DEFAULT '',
 operator TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed')),
 confirmed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 confirmed_at TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id,batch_code)
);
CREATE TABLE IF NOT EXISTS bot_fractions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_id INTEGER NOT NULL REFERENCES bot_batches(id) ON DELETE CASCADE,
 fraction_type TEXT NOT NULL CHECK(fraction_type IN ('light','heavy')),
 mesh_size_mm REAL,
 mesh_key TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(batch_id,fraction_type,mesh_key)
);
CREATE TABLE IF NOT EXISTS bot_identifications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 fraction_id INTEGER NOT NULL REFERENCES bot_fractions(id) ON DELETE CASCADE,
 item_no INTEGER NOT NULL,
 taxon_raw TEXT NOT NULL,
 taxon_normalized TEXT NOT NULL,
 family TEXT NOT NULL DEFAULT '',
 genus TEXT NOT NULL DEFAULT '',
 species TEXT NOT NULL DEFAULT '',
 count_type TEXT NOT NULL CHECK(count_type IN ('exact','upper','present','absent')),
 count_value REAL,
 count_max REAL,
 is_unknown INTEGER NOT NULL DEFAULT 0,
 contamination TEXT NOT NULL DEFAULT '',
 confidence_raw TEXT NOT NULL DEFAULT '',
 confidence_level TEXT NOT NULL DEFAULT '' CHECK(confidence_level IN ('','high','medium','low')),
 analyst TEXT NOT NULL DEFAULT '',
 notes TEXT NOT NULL DEFAULT '',
 import_id INTEGER,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(fraction_id,item_no)
);
CREATE TABLE IF NOT EXISTS bot_imports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('samples','batches','fractions','identifications')),
 filename TEXT NOT NULL DEFAULT '',
 file_sha256 TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'done' CHECK(status IN ('done')),
 total_rows INTEGER NOT NULL DEFAULT 0,
 accepted_rows INTEGER NOT NULL DEFAULT 0,
 rejected_rows INTEGER NOT NULL DEFAULT 0,
 skipped_rows INTEGER NOT NULL DEFAULT 0,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 message TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(project_id,kind,file_sha256)
);
CREATE TABLE IF NOT EXISTS bot_import_rejects (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 import_id INTEGER NOT NULL REFERENCES bot_imports(id) ON DELETE CASCADE,
 row_number INTEGER NOT NULL,
 raw_json TEXT NOT NULL DEFAULT '{}',
 error_code TEXT NOT NULL,
 error_message TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_stats_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version_no INTEGER NOT NULL,
 algorithm_version TEXT NOT NULL,
 rank TEXT NOT NULL CHECK(rank IN ('family','genus','species')),
 seed INTEGER NOT NULL,
 iterations INTEGER NOT NULL,
 params_json TEXT NOT NULL DEFAULT '{}',
 groups_json TEXT NOT NULL DEFAULT '[]',
 changeset_id INTEGER,
 status TEXT NOT NULL DEFAULT 'done' CHECK(status IN ('done')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id,version_no)
);
CREATE TABLE IF NOT EXISTS bot_stats_results (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id INTEGER NOT NULL REFERENCES bot_stats_runs(id) ON DELETE CASCADE,
 group_key TEXT NOT NULL,
 taxon_key TEXT NOT NULL,
 n_samples INTEGER NOT NULL,
 n_present INTEGER NOT NULL,
 total_volume_liters REAL NOT NULL,
 count_min REAL NOT NULL,
 count_max REAL NOT NULL,
 density_min REAL,
 density_max REAL,
 density_point REAL,
 density_ci_low REAL,
 density_ci_high REAL,
 ubiquity REAL,
 ubiquity_ci_low REAL,
 ubiquity_ci_high REAL
);
CREATE TABLE IF NOT EXISTS bot_stats_comparisons (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id INTEGER NOT NULL REFERENCES bot_stats_runs(id) ON DELETE CASCADE,
 taxon_key TEXT NOT NULL,
 group_a TEXT NOT NULL,
 group_b TEXT NOT NULL,
 diff_point REAL,
 diff_ci_low REAL,
 diff_ci_high REAL
);
CREATE TABLE IF NOT EXISTS bot_changesets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 note TEXT NOT NULL DEFAULT '',
 stats_params_json TEXT NOT NULL DEFAULT '{}',
 proposed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 review_note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 reviewed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bot_changeset_ops (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 changeset_id INTEGER NOT NULL REFERENCES bot_changesets(id) ON DELETE CASCADE,
 op_type TEXT NOT NULL CHECK(op_type IN ('rename','merge')),
 from_taxa_json TEXT NOT NULL,
 to_taxon TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_batches_project ON bot_batches(project_id,sample_id);
CREATE INDEX IF NOT EXISTS idx_bot_ident_fraction ON bot_identifications(fraction_id);
CREATE INDEX IF NOT EXISTS idx_bot_ident_project ON bot_identifications(project_id);
CREATE INDEX IF NOT EXISTS idx_bot_rejects_import ON bot_import_rejects(import_id);
CREATE INDEX IF NOT EXISTS idx_bot_results_run ON bot_stats_results(run_id);
CREATE INDEX IF NOT EXISTS idx_bot_comparisons_run ON bot_stats_comparisons(run_id);
"""

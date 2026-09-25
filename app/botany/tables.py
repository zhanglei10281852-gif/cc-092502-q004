"""植物考古模块表结构。

设计要点：
- 鉴定（botany_identifications）一旦写入即不可变；专家更名/合并通过经批准的
  变更集（botany_taxonomy_changesets/changes）在统计时解析，绝不回写原始行。
- 每次统计生成不可变版本 botany_stat_versions，保存种子、迭代次数、算法版本与完整结果。
- 导入按文件内容哈希 + 批次编码双重去重；错误行进 botany_rejections，可导出 CSV。
"""
from __future__ import annotations

BOTANY_SCHEMA = """
CREATE TABLE IF NOT EXISTS botany_contexts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 context_code TEXT NOT NULL,
 context_type TEXT NOT NULL CHECK(context_type IN ('paleochannel','dwelling','pit','other')),
 context_name TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(project_id, context_code)
);
CREATE TABLE IF NOT EXISTS botany_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_code TEXT NOT NULL,
 source_file TEXT NOT NULL DEFAULT '',
 content_sha256 TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'imported' CHECK(status IN ('imported','locked')),
 samples_imported INTEGER NOT NULL DEFAULT 0,
 rows_rejected INTEGER NOT NULL DEFAULT 0,
 imported_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 imported_at TEXT NOT NULL,
 locked_at TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id, batch_code)
);
CREATE TABLE IF NOT EXISTS botany_samples (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_id INTEGER NOT NULL REFERENCES botany_batches(id) ON DELETE CASCADE,
 context_id INTEGER NOT NULL REFERENCES botany_contexts(id) ON DELETE CASCADE,
 sample_code TEXT NOT NULL,
 volume_liters REAL NOT NULL CHECK(volume_liters > 0),
 mesh_mm REAL,
 sieve_notes TEXT NOT NULL DEFAULT '',
 polluted INTEGER NOT NULL DEFAULT 0,
 pollution_note TEXT NOT NULL DEFAULT '',
 sample_fingerprint TEXT NOT NULL,
 row_number INTEGER,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, sample_code)
);
CREATE INDEX IF NOT EXISTS idx_botany_samples_batch ON botany_samples(batch_id);
CREATE INDEX IF NOT EXISTS idx_botany_samples_context ON botany_samples(context_id);
CREATE TABLE IF NOT EXISTS botany_components (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sample_id INTEGER NOT NULL REFERENCES botany_samples(id) ON DELETE CASCADE,
 fraction TEXT NOT NULL CHECK(fraction IN ('heavy','light')),
 notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(sample_id, fraction)
);
CREATE TABLE IF NOT EXISTS botany_taxa (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 taxon_code TEXT NOT NULL,
 scientific_name TEXT NOT NULL DEFAULT '',
 family TEXT NOT NULL DEFAULT '',
 is_unknown INTEGER NOT NULL DEFAULT 0,
 unknown_detail TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(project_id, taxon_code)
);
CREATE TABLE IF NOT EXISTS botany_identifications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 component_id INTEGER NOT NULL REFERENCES botany_components(id) ON DELETE CASCADE,
 taxon_id INTEGER NOT NULL REFERENCES botany_taxa(id),
 confidence TEXT NOT NULL CHECK(confidence IN ('high','medium','low','uncertain')),
 censor TEXT NOT NULL CHECK(censor IN ('exact','below','nd')),
 count_value REAL NOT NULL DEFAULT 0,
 detection_limit REAL,
 is_unknown INTEGER NOT NULL DEFAULT 0,
 notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
-- 表达式索引：NULL 检测限归一化为 -1，保证完全相同的重复鉴定行被数据库拒绝
CREATE UNIQUE INDEX IF NOT EXISTS uq_botid_dedup ON botany_identifications(
 component_id, taxon_id, censor, count_value, COALESCE(detection_limit,-1), confidence
);
CREATE INDEX IF NOT EXISTS idx_botid_taxon ON botany_identifications(taxon_id);
CREATE TABLE IF NOT EXISTS botany_imports (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 batch_id INTEGER REFERENCES botany_batches(id) ON DELETE SET NULL,
 filename TEXT NOT NULL,
 content_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('accepted','partial','rejected','empty','duplicate')),
 accepted_rows INTEGER NOT NULL DEFAULT 0,
 rejected_rows INTEGER NOT NULL DEFAULT 0,
 duplicate_lines INTEGER NOT NULL DEFAULT 0,
 summary_json TEXT NOT NULL DEFAULT '{}',
 imported_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_botany_imports_project ON botany_imports(project_id, id);
-- 同项目下相同文件内容只允许导入一次，数据库层面杜绝重传重复累加
CREATE UNIQUE INDEX IF NOT EXISTS uq_botany_imports_content ON botany_imports(project_id, content_sha256);
CREATE TABLE IF NOT EXISTS botany_rejections (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 import_id INTEGER NOT NULL REFERENCES botany_imports(id) ON DELETE CASCADE,
 row_number INTEGER NOT NULL,
 sheet TEXT NOT NULL DEFAULT '',
 raw_line TEXT NOT NULL DEFAULT '',
 error_code TEXT NOT NULL,
 error_message TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_botany_rej_import ON botany_rejections(import_id);
CREATE TABLE IF NOT EXISTS botany_taxonomy_views (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 view_code TEXT NOT NULL,
 label TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, view_code)
);
CREATE TABLE IF NOT EXISTS botany_view_mappings (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 view_id INTEGER NOT NULL REFERENCES botany_taxonomy_views(id) ON DELETE CASCADE,
 taxon_id INTEGER NOT NULL REFERENCES botany_taxa(id) ON DELETE CASCADE,
 mapped_code TEXT NOT NULL,
 mapped_label TEXT NOT NULL DEFAULT '',
 UNIQUE(view_id, taxon_id)
);
CREATE INDEX IF NOT EXISTS idx_botany_view_map ON botany_view_mappings(view_id);
CREATE TABLE IF NOT EXISTS botany_taxonomy_changesets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 title TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 rationale TEXT NOT NULL DEFAULT '',
 seed INTEGER NOT NULL,
 iterations INTEGER NOT NULL DEFAULT 2000,
 proposed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 reviewed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 review_note TEXT NOT NULL DEFAULT '',
 stat_version_id INTEGER REFERENCES botany_stat_versions(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 reviewed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_botany_changesets_project ON botany_taxonomy_changesets(project_id, id);
CREATE TABLE IF NOT EXISTS botany_taxonomy_changes (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 changeset_id INTEGER NOT NULL REFERENCES botany_taxonomy_changesets(id) ON DELETE CASCADE,
 taxon_id INTEGER NOT NULL REFERENCES botany_taxa(id),
 action TEXT NOT NULL CHECK(action IN ('rename','merge')),
 new_name TEXT NOT NULL DEFAULT '',
 new_code TEXT NOT NULL DEFAULT '',
 target_taxon_id INTEGER REFERENCES botany_taxa(id),
 note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_botany_changes_cs ON botany_taxonomy_changes(changeset_id);
CREATE TABLE IF NOT EXISTS botany_stat_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version_number INTEGER NOT NULL,
 label TEXT NOT NULL DEFAULT '',
 view_id INTEGER REFERENCES botany_taxonomy_views(id) ON DELETE SET NULL,
 changeset_id INTEGER REFERENCES botany_taxonomy_changesets(id) ON DELETE SET NULL,
 seed INTEGER NOT NULL,
 iterations INTEGER NOT NULL,
 confidence REAL NOT NULL,
 include_polluted INTEGER NOT NULL DEFAULT 0,
 algorithm_version TEXT NOT NULL,
 params_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(project_id, version_number)
);
"""


def create_schema(db) -> None:
    db.executescript(BOTANY_SCHEMA)

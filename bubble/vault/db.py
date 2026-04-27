"""Vault SQLite schema + connection helpers.

The schema upgrade from the pre-redesign version:
  packages PRIMARY KEY is (name, version, wheel_tag) — was (name, version)
  + wheel_tag, sha256, python_tag, abi_tag, platform_tag, last_used_at columns
  bubbles → split into 'bubbles' (ephemeral) and 'shells' (long-lived)

Migration: pre-redesign vault was empty (0 rows in any table). We drop and
recreate. If you've populated the old schema, dump first.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from .. import config


SCHEMA_VERSION = 3


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS packages (
    name        TEXT NOT NULL,
    version     TEXT NOT NULL,
    wheel_tag   TEXT NOT NULL,        -- e.g. cp313-cp313-manylinux2014_aarch64, or 'py3-none-any', or 'sdist'
    python_tag  TEXT,                 -- cp313, py3, etc
    abi_tag     TEXT,                 -- cp313, abi3, none
    platform_tag TEXT,                -- manylinux2014_aarch64, any
    sha256      TEXT,                 -- of the source artifact (wheel/sdist), nullable for venv-imported
    source      TEXT,                 -- 'pip', 'venv-import', 'pypi', 'manual', 'npm'
    cached_at   TEXT,
    last_used_at TEXT,
    vault_path  TEXT NOT NULL,
    has_native  INTEGER DEFAULT 0,
    metadata    TEXT,                 -- JSON
    PRIMARY KEY (name, version, wheel_tag)
);

CREATE INDEX IF NOT EXISTS idx_packages_name      ON packages(name);
CREATE INDEX IF NOT EXISTS idx_packages_last_used ON packages(last_used_at);

CREATE TABLE IF NOT EXISTS dependencies (
    package          TEXT NOT NULL,
    version          TEXT NOT NULL,
    wheel_tag        TEXT NOT NULL,
    dep_name         TEXT NOT NULL,
    dep_version_spec TEXT,
    optional         INTEGER DEFAULT 0,
    extra            TEXT,            -- e.g. 'dev', 'test'
    FOREIGN KEY (package, version, wheel_tag)
        REFERENCES packages(name, version, wheel_tag) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_deps ON dependencies(package, version, wheel_tag);

CREATE TABLE IF NOT EXISTS modules (
    package      TEXT NOT NULL,
    version      TEXT NOT NULL,
    wheel_tag    TEXT NOT NULL,
    module_name  TEXT NOT NULL,
    module_path  TEXT NOT NULL,
    is_native    INTEGER DEFAULT 0,
    size_bytes   INTEGER,
    FOREIGN KEY (package, version, wheel_tag)
        REFERENCES packages(name, version, wheel_tag) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_modules        ON modules(package, version, wheel_tag);
CREATE INDEX IF NOT EXISTS idx_modules_name   ON modules(module_name);

CREATE TABLE IF NOT EXISTS module_imports (
    package          TEXT NOT NULL,
    version          TEXT NOT NULL,
    wheel_tag        TEXT NOT NULL,
    module_name      TEXT NOT NULL,
    imports          TEXT,
    imports_external TEXT,
    FOREIGN KEY (package, version, wheel_tag)
        REFERENCES packages(name, version, wheel_tag) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_module_imports        ON module_imports(package, version, wheel_tag);
CREATE INDEX IF NOT EXISTS idx_module_imports_name   ON module_imports(module_name);

-- Each package's top-level importable names (from top_level.txt or directory scan)
-- This is the bridge from Python's import namespace to PyPI's distribution namespace.
-- import_sha256 binds the import name to the bytes the vault will serve under it:
-- computed once at vault-add time over the verified subtree, deterministic.
CREATE TABLE IF NOT EXISTS top_level (
    package        TEXT NOT NULL,
    version        TEXT NOT NULL,
    wheel_tag      TEXT NOT NULL,
    import_name    TEXT NOT NULL,   -- the importable top-level Python name
    import_sha256  TEXT,            -- content hash of the subtree this row claims
    FOREIGN KEY (package, version, wheel_tag)
        REFERENCES packages(name, version, wheel_tag) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_top_level_import ON top_level(import_name);
CREATE INDEX IF NOT EXISTS idx_top_level_pkg    ON top_level(package, version, wheel_tag);

CREATE TABLE IF NOT EXISTS bubbles (
    bubble_id   TEXT PRIMARY KEY,
    created_at  TEXT,
    script_path TEXT,
    status      TEXT DEFAULT 'active',
    bubble_path TEXT,
    packages    TEXT
);

CREATE TABLE IF NOT EXISTS shells (
    name         TEXT PRIMARY KEY,
    created_at   TEXT,
    last_used_at TEXT,
    shell_path   TEXT NOT NULL,
    python_tag   TEXT,
    lockfile     TEXT,                -- path to .bubble.lock if any
    metadata     TEXT
);

-- Per-file integrity facts. Computed at vault-add, consulted at
-- vault-read. `vault_files.sha256` is the integrity edge between the
-- bytes we stored and the bytes still on disk. `packages.sha256` is the
-- provenance edge between the bytes the index published and the bytes
-- we received — the two columns persist together but answer different
-- questions. Drift surfaces; it does not auto-recover.
CREATE TABLE IF NOT EXISTS vault_files (
    package    TEXT NOT NULL,
    version    TEXT NOT NULL,
    wheel_tag  TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    PRIMARY KEY (package, version, wheel_tag, rel_path),
    FOREIGN KEY (package, version, wheel_tag)
        REFERENCES packages(name, version, wheel_tag) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_vault_files_pkg     ON vault_files(package, version, wheel_tag);
CREATE INDEX IF NOT EXISTS idx_vault_files_sha256  ON vault_files(sha256);
"""


def _drop_old_schema(conn: sqlite3.Connection) -> None:
    for tbl in ("vault_files", "top_level", "module_imports", "modules",
                "dependencies", "bubbles", "shells", "packages", "schema_meta"):
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")


def init_db() -> None:
    """Initialize (or migrate) the vault DB.

    The old schema had PRIMARY KEY (name, version) on packages. The new key is
    (name, version, wheel_tag). When we see the old shape with zero rows, we
    drop and recreate. With rows present, we refuse — caller must export first.
    """
    config.ensure_dirs()
    conn = sqlite3.connect(str(config.VAULT_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='packages'"
    )
    has_packages = cur.fetchone() is not None

    needs_recreate = False
    if has_packages:
        # Check primary-key columns
        cols = [row[1] for row in conn.execute("PRAGMA table_info(packages)")]
        pk_cols = [row[1] for row in conn.execute("PRAGMA table_info(packages)") if row[5]]
        if "wheel_tag" not in cols or "wheel_tag" not in pk_cols:
            row_count = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
            if row_count == 0:
                needs_recreate = True
            else:
                conn.close()
                raise RuntimeError(
                    "Vault DB has the old schema with rows. Export first: "
                    "`bubble vault list > /tmp/old.txt && rm ~/.bubble/vault.db`"
                )

    if needs_recreate:
        _drop_old_schema(conn)

    conn.executescript(SCHEMA)

    # top_level.import_sha256 was added after the first vaults shipped; ALTER in
    # for older DBs. New DBs got the column from the SCHEMA above.
    tl_cols = {row[1] for row in conn.execute("PRAGMA table_info(top_level)")}
    if "import_sha256" not in tl_cols:
        conn.execute("ALTER TABLE top_level ADD COLUMN import_sha256 TEXT")

    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    conn.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.VAULT_DB))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

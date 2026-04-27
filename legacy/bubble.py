#!/usr/bin/env python3
"""
BUBBLE — Ephemeral Dependency Isolation for JANUS
==================================================

Three layers:
  1. VAULT   — Local offline cache of packages (stored unpacked at module level)
  2. SCANNER — Static import analysis + dependency graph resolution
  3. BUBBLE  — Ephemeral sandboxed environments assembled from Vault fragments

Usage:
  bubble vault add <package> [--version X.Y.Z]   # Download & cache a package
  bubble vault list                                # Show cached packages
  bubble vault index                               # Rebuild dependency index
  bubble scan <script.py>                          # Trace imports, show what's needed
  bubble up <script.py> [--keep]                   # Spin up bubble, run, dissolve
  bubble down [--all]                              # Clean up lingering bubbles
  bubble doctor                                    # Diagnose environment issues

No external dependencies. Runs on stdlib only.
"""

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

BUBBLE_HOME = Path(os.environ.get("BUBBLE_HOME", os.path.expanduser("~/.bubble")))
VAULT_DIR = BUBBLE_HOME / "vault"          # Unpacked module storage
VAULT_DB = BUBBLE_HOME / "vault.db"        # Dependency index
BUBBLES_DIR = BUBBLE_HOME / "bubbles"      # Active ephemeral environments
LOGS_DIR = BUBBLE_HOME / "logs"            # Diagnostic logs
WHEELS_DIR = BUBBLE_HOME / "wheels"        # Raw downloaded wheels/tarballs

# ─────────────────────────────────────────────
# Path Shims — compensate for proot/Termux
# filesystem layout differences
# ─────────────────────────────────────────────

# Map of paths that packages commonly expect → where to find them (or how to create them).
# Each entry: expected_path → { 'type': 'search'|'create'|'symlink', ... }
#
# 'search'  — look for the real file in a list of candidate locations, symlink it
# 'create'  — create the path as an empty dir (just needs to exist)
# 'symlink' — hardcoded redirect to a known alternative
#
# This table is the "connect the dots" — not exhaustive, just the paths that
# actually cause failures in practice. Extend as new ones are discovered.

PATH_SHIMS = {
    # SSL certificates — the #1 proot/Termux breakage
    '/etc/ssl/certs/ca-certificates.crt': {
        'type': 'search',
        'candidates': [
            # Termux
            '{PREFIX}/etc/tls/cert.pem',
            '{PREFIX}/etc/ssl/cert.pem',
            # Kali/Debian under proot
            '/etc/ssl/certs/ca-certificates.crt',
            # Alpine
            '/etc/ssl/cert.pem',
            # RHEL/Fedora
            '/etc/pki/tls/certs/ca-bundle.crt',
            # certifi package (Python)
            '{CERTIFI}',
        ]
    },
    '/etc/ssl/certs': {
        'type': 'search',
        'candidates': [
            '{PREFIX}/etc/tls',
            '{PREFIX}/etc/ssl/certs',
            '/etc/ssl/certs',
        ]
    },

    # Resolver — proot usually handles this but some tools check directly
    '/etc/resolv.conf': {
        'type': 'search',
        'candidates': [
            '/etc/resolv.conf',
            '{PREFIX}/etc/resolv.conf',
        ]
    },

    # Temp directories — some packages hardcode /tmp
    '/tmp': {
        'type': 'search',
        'candidates': [
            '/tmp',
            '{PREFIX}/tmp',
            '{TMPDIR}',
        ]
    },

    # Shared library paths that native packages look for
    # Multi-arch: aarch64-linux-gnu is the most common on ARM64 Linux
    '/usr/lib': {
        'type': 'search',
        'candidates': [
            '/usr/lib/aarch64-linux-gnu',
            '/usr/lib/x86_64-linux-gnu',
            '/usr/lib',
        ]
    },
    '/usr/local/lib': {
        'type': 'create',
    },
    '/usr/include': {
        'type': 'create',
    },

    # Node.js specific — gyp looks for these during native addon builds
    '/usr/bin/python': {
        'type': 'search',
        'candidates': [
            '/usr/bin/python3',
            '{PREFIX}/bin/python3',
            '{PREFIX}/bin/python',
        ]
    },
}


def _resolve_shim_var(path_template):
    """Resolve variables in shim path templates."""
    prefix = os.environ.get('PREFIX', '/usr')
    tmpdir = os.environ.get('TMPDIR', '/tmp')
    
    result = path_template.replace('{PREFIX}', prefix)
    result = result.replace('{TMPDIR}', tmpdir)
    
    # Special: {CERTIFI} resolves to certifi's CA bundle if available
    if '{CERTIFI}' in result:
        try:
            import certifi
            return certifi.where()
        except ImportError:
            return None
    
    return result


def setup_path_shims(bubble_dir, verbose=False):
    """
    Stage path shims into the bubble's filesystem overlay.
    Creates a 'sysroot' dir in the bubble that mirrors expected paths,
    with symlinks pointing to where things actually are on this system.
    
    Returns a dict of environment variable overrides.
    """
    sysroot = bubble_dir / "sysroot"
    env_overrides = {}
    shimmed = []
    
    for expected_path, shim_config in PATH_SHIMS.items():
        shim_type = shim_config['type']
        
        # Target location in the bubble sysroot
        target = sysroot / expected_path.lstrip('/')
        
        if shim_type == 'create':
            target.mkdir(parents=True, exist_ok=True)
            shimmed.append(expected_path)
            
        elif shim_type == 'search':
            # Try each candidate until we find one that exists
            found = None
            for candidate in shim_config['candidates']:
                resolved = _resolve_shim_var(candidate)
                if resolved and os.path.exists(resolved):
                    found = resolved
                    break
            
            if found:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    try:
                        os.symlink(found, target)
                        shimmed.append(f"{expected_path} → {found}")
                    except OSError:
                        # Can't symlink — copy if it's a file, mkdir if dir
                        if os.path.isfile(found):
                            shutil.copy2(found, target)
                            shimmed.append(f"{expected_path} → {found} (copied)")
                        elif os.path.isdir(found):
                            shutil.copytree(found, target, dirs_exist_ok=True)
                            shimmed.append(f"{expected_path} → {found} (copied)")
                            
        elif shim_type == 'symlink':
            dest = _resolve_shim_var(shim_config['dest'])
            if dest and os.path.exists(dest):
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    try:
                        os.symlink(dest, target)
                        shimmed.append(f"{expected_path} → {dest}")
                    except OSError:
                        pass
    
    # SSL environment overrides — many packages check these before filesystem
    ssl_cert = sysroot / "etc/ssl/certs/ca-certificates.crt"
    if ssl_cert.exists() or ssl_cert.is_symlink():
        env_overrides['SSL_CERT_FILE'] = str(ssl_cert)
        env_overrides['REQUESTS_CA_BUNDLE'] = str(ssl_cert)
        env_overrides['CURL_CA_BUNDLE'] = str(ssl_cert)
        env_overrides['NODE_EXTRA_CA_CERTS'] = str(ssl_cert)
    
    if verbose and shimmed:
        for s in shimmed:
            print(f"  │ shim: {s}")
    
    return env_overrides, shimmed

# Known stdlib modules (Python 3.10+) — these never need to be vaulted
STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(sys, 'stdlib_module_names') else {
    'abc', 'aifc', 'argparse', 'ast', 'asyncio', 'atexit', 'base64',
    'binascii', 'bisect', 'builtins', 'bz2', 'calendar', 'cgi', 'cgitb',
    'chunk', 'cmath', 'cmd', 'code', 'codecs', 'codeop', 'collections',
    'colorsys', 'compileall', 'concurrent', 'configparser', 'contextlib',
    'contextvars', 'copy', 'copyreg', 'cProfile', 'crypt', 'csv', 'ctypes',
    'curses', 'dataclasses', 'datetime', 'dbm', 'decimal', 'difflib', 'dis',
    'distutils', 'doctest', 'email', 'encodings', 'enum', 'errno',
    'faulthandler', 'fcntl', 'filecmp', 'fileinput', 'fnmatch', 'fractions',
    'ftplib', 'functools', 'gc', 'getopt', 'getpass', 'gettext', 'glob',
    'graphlib', 'grp', 'gzip', 'hashlib', 'heapq', 'hmac', 'html', 'http',
    'idlelib', 'imaplib', 'imghdr', 'importlib', 'inspect', 'io',
    'ipaddress', 'itertools', 'json', 'keyword', 'lib2to3', 'linecache',
    'locale', 'logging', 'lzma', 'mailbox', 'mailcap', 'marshal', 'math',
    'mimetypes', 'mmap', 'modulefinder', 'multiprocessing', 'netrc',
    'numbers', 'operator', 'optparse', 'os', 'pathlib', 'pdb', 'pickle',
    'pickletools', 'pipes', 'pkgutil', 'platform', 'plistlib', 'poplib',
    'posix', 'posixpath', 'pprint', 'profile', 'pstats', 'pty', 'pwd',
    'py_compile', 'pyclbr', 'pydoc', 'queue', 'quopri', 'random', 're',
    'readline', 'reprlib', 'resource', 'rlcompleter', 'runpy', 'sched',
    'secrets', 'select', 'selectors', 'shelve', 'shlex', 'shutil', 'signal',
    'site', 'smtpd', 'smtplib', 'sndhdr', 'socket', 'socketserver',
    'sqlite3', 'ssl', 'stat', 'statistics', 'string', 'stringprep',
    'struct', 'subprocess', 'sunau', 'symtable', 'sys', 'sysconfig',
    'syslog', 'tabnanny', 'tarfile', 'telnetlib', 'tempfile', 'termios',
    'test', 'textwrap', 'threading', 'time', 'timeit', 'tkinter', 'token',
    'tokenize', 'tomllib', 'trace', 'traceback', 'tracemalloc', 'tty',
    'turtle', 'turtledemo', 'types', 'typing', 'unicodedata', 'unittest',
    'urllib', 'uu', 'uuid', 'venv', 'warnings', 'wave', 'weakref',
    'webbrowser', 'winreg', 'winsound', 'wsgiref', 'xdrlib', 'xml',
    'xmlrpc', 'zipapp', 'zipfile', 'zipimport', 'zlib', '_thread',
}

# Common top-level import → PyPI package name mappings
IMPORT_TO_PACKAGE = {
    'cv2': 'opencv-python',
    'PIL': 'Pillow',
    'sklearn': 'scikit-learn',
    'skimage': 'scikit-image',
    'yaml': 'PyYAML',
    'bs4': 'beautifulsoup4',
    'gi': 'PyGObject',
    'attr': 'attrs',
    'serial': 'pyserial',
    'usb': 'pyusb',
    'wx': 'wxPython',
    'Crypto': 'pycryptodome',
    'lxml': 'lxml',
    'faiss': 'faiss-cpu',
    'dotenv': 'python-dotenv',
    'jose': 'python-jose',
    'magic': 'python-magic',
    'dateutil': 'python-dateutil',
    'psutil': 'psutil',
}

# Node.js built-in modules — these never need to be vaulted
NODE_BUILTINS = {
    'assert', 'buffer', 'child_process', 'cluster', 'console', 'constants',
    'crypto', 'dgram', 'dns', 'domain', 'events', 'fs', 'http', 'http2',
    'https', 'inspector', 'module', 'net', 'os', 'path', 'perf_hooks',
    'process', 'punycode', 'querystring', 'readline', 'repl', 'stream',
    'string_decoder', 'sys', 'timers', 'tls', 'trace_events', 'tty',
    'url', 'util', 'v8', 'vm', 'wasi', 'worker_threads', 'zlib',
    'node:assert', 'node:buffer', 'node:child_process', 'node:cluster',
    'node:console', 'node:crypto', 'node:dgram', 'node:dns', 'node:events',
    'node:fs', 'node:http', 'node:http2', 'node:https', 'node:inspector',
    'node:module', 'node:net', 'node:os', 'node:path', 'node:perf_hooks',
    'node:process', 'node:querystring', 'node:readline', 'node:repl',
    'node:stream', 'node:string_decoder', 'node:timers', 'node:tls',
    'node:tty', 'node:url', 'node:util', 'node:v8', 'node:vm',
    'node:worker_threads', 'node:zlib',
}


# ─────────────────────────────────────────────
# Database Layer
# ─────────────────────────────────────────────

def init_db():
    """Initialize the vault database."""
    BUBBLE_HOME.mkdir(parents=True, exist_ok=True)
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    BUBBLES_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    WHEELS_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(VAULT_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS packages (
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            source TEXT,          -- 'pip', 'manual', 'npm'
            cached_at TEXT,
            vault_path TEXT,      -- path to unpacked modules in vault
            has_native INTEGER DEFAULT 0,  -- 1 if contains .so/.dylib
            metadata TEXT,        -- JSON blob
            PRIMARY KEY (name, version)
        );

        CREATE TABLE IF NOT EXISTS dependencies (
            package TEXT NOT NULL,
            version TEXT NOT NULL,
            dep_name TEXT NOT NULL,
            dep_version_spec TEXT, -- e.g., '>=1.0,<2.0'
            optional INTEGER DEFAULT 0,
            FOREIGN KEY (package, version) REFERENCES packages(name, version)
        );

        CREATE TABLE IF NOT EXISTS modules (
            package TEXT NOT NULL,
            version TEXT NOT NULL,
            module_name TEXT NOT NULL,  -- e.g., 'numpy.linalg'
            module_path TEXT NOT NULL,  -- relative path in vault
            is_native INTEGER DEFAULT 0,
            size_bytes INTEGER,
            FOREIGN KEY (package, version) REFERENCES packages(name, version)
        );

        CREATE TABLE IF NOT EXISTS bubbles (
            bubble_id TEXT PRIMARY KEY,
            created_at TEXT,
            script_path TEXT,
            status TEXT DEFAULT 'active',  -- 'active', 'dissolved'
            bubble_path TEXT,
            packages TEXT  -- JSON list
        );

        CREATE INDEX IF NOT EXISTS idx_deps ON dependencies(package, version);
        CREATE INDEX IF NOT EXISTS idx_modules ON modules(package, version);
        CREATE INDEX IF NOT EXISTS idx_module_name ON modules(module_name);

        CREATE TABLE IF NOT EXISTS module_imports (
            package TEXT NOT NULL,
            version TEXT NOT NULL,
            module_name TEXT NOT NULL,
            imports TEXT,           -- JSON list of internal imports (same package)
            imports_external TEXT,  -- JSON list of external imports (other packages)
            FOREIGN KEY (package, version) REFERENCES packages(name, version)
        );

        CREATE INDEX IF NOT EXISTS idx_module_imports ON module_imports(package, version);
        CREATE INDEX IF NOT EXISTS idx_module_imports_name ON module_imports(module_name);
    """)
    conn.close()


def get_db():
    """Get a database connection."""
    return sqlite3.connect(str(VAULT_DB))


# ─────────────────────────────────────────────
# Layer 1: VAULT
# ─────────────────────────────────────────────

def vault_add(package_name, version=None, source='pip'):
    """Download a package and add it to the vault (unpacked at module level)."""
    init_db()

    # Download the wheel/sdist
    print(f"  ↓ Downloading {package_name}" + (f"=={version}" if version else ""))
    
    dl_dir = WHEELS_DIR / f"{package_name}_dl"
    dl_dir.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, '-m', 'pip', 'download',
           '--dest', str(dl_dir),
           '--no-deps',  # We manage deps ourselves
           '--prefer-binary']
    
    if version:
        cmd.append(f"{package_name}=={version}")
    else:
        cmd.append(package_name)
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ✗ Download failed: {result.stderr.strip()}")
        return False

    # Find what was downloaded
    downloaded = list(dl_dir.iterdir())
    if not downloaded:
        print("  ✗ No files downloaded")
        return False

    dl_file = downloaded[0]
    print(f"  ✓ Downloaded: {dl_file.name}")

    # Extract version from filename if not specified
    if not version:
        version = _extract_version_from_filename(dl_file.name, package_name)

    # Unpack into vault
    pkg_vault_dir = VAULT_DIR / f"{package_name}" / f"{version}"
    if pkg_vault_dir.exists():
        shutil.rmtree(pkg_vault_dir)
    pkg_vault_dir.mkdir(parents=True)

    has_native = False
    
    if dl_file.suffix == '.whl' or dl_file.name.endswith('.whl'):
        # Wheels are just zip files
        with zipfile.ZipFile(dl_file, 'r') as zf:
            zf.extractall(pkg_vault_dir, filter='data')
            has_native = any(n.endswith(('.so', '.dylib', '.pyd')) for n in zf.namelist())
    elif dl_file.suffix in ('.gz', '.tgz'):
        with tarfile.open(dl_file, 'r:gz') as tf:
            tf.extractall(pkg_vault_dir, filter='data')
            has_native = any(n.endswith(('.c', '.cpp', '.pyx')) for n in tf.getnames())
    elif dl_file.suffix == '.zip':
        with zipfile.ZipFile(dl_file, 'r') as zf:
            zf.extractall(pkg_vault_dir, filter='data')
    else:
        print(f"  ✗ Unknown format: {dl_file.suffix}")
        return False

    # Index the package
    _index_package(package_name, version, pkg_vault_dir, has_native, source)
    
    # Extract and index dependencies from metadata
    _index_dependencies(package_name, version, pkg_vault_dir)

    # Clean up download
    shutil.rmtree(dl_dir, ignore_errors=True)

    print(f"  ✓ Vaulted: {package_name}=={version} ({'native' if has_native else 'pure python'})")
    return True


def vault_add_recursive(package_name, version=None, depth=0, seen=None):
    """Download a package and all its dependencies into the vault."""
    if seen is None:
        seen = set()
    
    key = f"{package_name}=={version}" if version else package_name
    if key in seen:
        return True
    seen.add(key)
    
    indent = "  " * depth
    print(f"{indent}● {package_name}" + (f"=={version}" if version else ""))
    
    # Check if already vaulted
    conn = get_db()
    if version:
        row = conn.execute("SELECT 1 FROM packages WHERE name=? AND version=?",
                          (package_name, version)).fetchone()
    else:
        row = conn.execute("SELECT 1 FROM packages WHERE name=?",
                          (package_name,)).fetchone()
    conn.close()
    
    if row:
        print(f"{indent}  (already vaulted)")
    else:
        if not vault_add(package_name, version):
            return False
    
    # Now resolve dependencies
    conn = get_db()
    rows = conn.execute(
        "SELECT dep_name, dep_version_spec FROM dependencies WHERE package=? AND optional=0",
        (package_name,)).fetchall()
    conn.close()
    
    for dep_name, dep_spec in rows:
        # For now, don't pin to specific version — grab latest
        vault_add_recursive(dep_name, depth=depth+1, seen=seen)
    
    return True


def vault_list():
    """List all packages in the vault."""
    init_db()
    conn = get_db()
    rows = conn.execute(
        "SELECT name, version, has_native, cached_at FROM packages ORDER BY name"
    ).fetchall()
    conn.close()

    if not rows:
        print("  Vault is empty. Use 'bubble vault add <package>' to populate.")
        return

    print(f"  {'Package':<30} {'Version':<15} {'Type':<10} {'Cached'}")
    print(f"  {'─'*30} {'─'*15} {'─'*10} {'─'*20}")
    for name, ver, native, cached in rows:
        ptype = "native" if native else "pure"
        print(f"  {name:<30} {ver:<15} {ptype:<10} {cached}")
    
    total = len(rows)
    native_count = sum(1 for _, _, n, _ in rows if n)
    print(f"\n  Total: {total} packages ({native_count} native, {total - native_count} pure)")


def vault_index():
    """Rebuild the module-level index for all vaulted packages."""
    init_db()
    conn = get_db()
    conn.execute("DELETE FROM modules")
    conn.execute("DELETE FROM module_imports")

    rows = conn.execute("SELECT name, version, vault_path FROM packages").fetchall()

    # Build import-to-package mapping for categorizing imports
    import_to_pkg = {v: k for k, v in IMPORT_TO_PACKAGE.items()}

    count = 0
    import_count = 0
    for name, version, vault_path in rows:
        vpath = Path(vault_path)
        if not vpath.exists():
            continue

        # Package prefix for internal import detection
        pkg_normalized = name.replace('-', '_').replace('.', '_')
        pkg_import_name = import_to_pkg.get(name, pkg_normalized)

        for py_file in vpath.rglob("*.py"):
            rel = py_file.relative_to(vpath)
            # Convert path to module name
            parts = list(rel.parts)
            if any(p.endswith('.dist-info') for p in parts):
                continue
            if parts[-1] == '__init__.py':
                parts = parts[:-1]
            else:
                parts[-1] = parts[-1].replace('.py', '')

            module_name = '.'.join(parts) if parts else name
            is_native = 0
            size = py_file.stat().st_size

            conn.execute(
                "INSERT INTO modules (package, version, module_name, module_path, is_native, size_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, version, module_name, str(rel), is_native, size)
            )
            count += 1

            # Scan for imports
            try:
                with open(py_file, 'r', encoding='utf-8', errors='replace') as f:
                    source = f.read()
                tree = ast.parse(source)
                scanner = ImportScanner()
                scanner.visit(tree)

                internal_imports = []
                external_imports = []

                for imp in scanner.imports:
                    if imp in STDLIB_MODULES:
                        continue
                    top_level = imp.split('.')[0]
                    imp_pkg = IMPORT_TO_PACKAGE.get(top_level, top_level)
                    imp_normalized = imp_pkg.replace('-', '_').replace('.', '_')

                    if imp_normalized == pkg_import_name or imp.startswith(pkg_normalized + '.'):
                        internal_imports.append(imp)
                    else:
                        external_imports.append(imp)

                if internal_imports or external_imports:
                    conn.execute(
                        "INSERT INTO module_imports "
                        "(package, version, module_name, imports, imports_external) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (name, version, module_name,
                         json.dumps(internal_imports) if internal_imports else None,
                         json.dumps(external_imports) if external_imports else None)
                    )
                    import_count += 1
            except (SyntaxError, Exception):
                pass

    # Also index .so files
    for name, version, vault_path in rows:
        vpath = Path(vault_path)
        if not vpath.exists():
            continue
        for so_file in vpath.rglob("*.so"):
            rel = so_file.relative_to(vpath)
            parts = list(rel.parts)
            if any(p.endswith('.dist-info') for p in parts):
                continue
            parts[-1] = re.sub(r'\.cpython-\d+.*\.so$', '', parts[-1])
            module_name = '.'.join(parts)

            conn.execute(
                "INSERT INTO modules (package, version, module_name, module_path, is_native, size_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, version, module_name, str(rel), 1, so_file.stat().st_size)
            )
            count += 1

    conn.commit()
    conn.close()
    print(f"  ✓ Indexed {count} modules, {import_count} with imports across {len(rows)} packages")


# ─────────────────────────────────────────────
# Layer 2: SCANNER
# ─────────────────────────────────────────────

class ImportScanner(ast.NodeVisitor):
    """Static analysis: extract all imports from a Python source file."""
    
    def __init__(self):
        self.imports = set()        # Top-level module names
        self.from_imports = {}      # module -> set of names
        self.all_modules = set()    # Full dotted paths
    
    def visit_Import(self, node):
        for alias in node.names:
            top = alias.name.split('.')[0]
            self.imports.add(top)
            self.all_modules.add(alias.name)
        self.generic_visit(node)
    
    def visit_ImportFrom(self, node):
        if node.module:
            top = node.module.split('.')[0]
            self.imports.add(top)
            self.all_modules.add(node.module)
            if node.module not in self.from_imports:
                self.from_imports[node.module] = set()
            for alias in (node.names or []):
                self.from_imports[node.module].add(alias.name)
        self.generic_visit(node)


def scan_script(script_path):
    """Scan a Python script and return its dependency map."""
    script_path = Path(script_path)
    if not script_path.exists():
        print(f"  ✗ File not found: {script_path}")
        return None

    with open(script_path, 'r', encoding='utf-8', errors='replace') as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"  ✗ Syntax error in {script_path}: {e}")
        return None

    scanner = ImportScanner()
    scanner.visit(tree)
    
    # Categorize imports
    stdlib = set()
    external = set()
    local = set()
    
    script_dir = script_path.parent
    
    for mod in scanner.imports:
        if mod in STDLIB_MODULES:
            stdlib.add(mod)
        elif (script_dir / mod).exists() or (script_dir / f"{mod}.py").exists():
            local.add(mod)
        else:
            external.add(mod)
    
    # Map external imports to package names
    packages_needed = {}
    for mod in external:
        pkg_name = IMPORT_TO_PACKAGE.get(mod, mod)
        packages_needed[pkg_name] = {
            'import_name': mod,
            'submodules': [m for m in scanner.all_modules if m.split('.')[0] == mod],
            'from_imports': {k: list(v) for k, v in scanner.from_imports.items() 
                          if k.split('.')[0] == mod}
        }
    
    return {
        'script': str(script_path),
        'stdlib': sorted(stdlib),
        'external': packages_needed,
        'local': sorted(local),
        'all_imports': sorted(scanner.all_modules),
    }


def _resolve_transitive(initial_resolved):
    """Expand resolved packages by walking the dependency graph in the vault.

    For each package in initial_resolved, look up its declared dependencies
    in the dependencies table, check if those deps are in the vault, and
    add them to the resolved set. Repeat until no new packages are found.
    """
    conn = get_db()
    resolved = dict(initial_resolved)
    changed = True
    while changed:
        changed = False
        # Snapshot current keys so we don't iterate while modifying
        for pkg_name in list(resolved.keys()):
            info = resolved[pkg_name]
            rows = conn.execute(
                "SELECT dep_name, dep_version_spec FROM dependencies "
                "WHERE package=? AND version=? AND optional=0",
                (pkg_name, info['version'])
            ).fetchall()
            for dep_name, dep_spec in rows:
                if dep_name in resolved:
                    continue
                # Map module name to package name (e.g. PIL -> Pillow)
                vault_name = IMPORT_TO_PACKAGE.get(dep_name, dep_name)
                row = conn.execute(
                    "SELECT name, version, vault_path, has_native FROM packages "
                    "WHERE name=? ORDER BY cached_at DESC LIMIT 1",
                    (vault_name,)
                ).fetchone()
                if row:
                    name, version, vault_path, has_native = row
                    # Grab all modules for the dep (simpler than re-scanning imports)
                    mod_rows = conn.execute(
                        "SELECT module_name, module_path, size_bytes FROM modules "
                        "WHERE package=? AND version=?",
                        (name, version)
                    ).fetchall()
                    resolved[dep_name] = {
                        'version': version,
                        'vault_path': vault_path,
                        'has_native': bool(has_native),
                        'modules': [(m[0], m[1], m[2]) for m in mod_rows],
                        'total_size': sum(m[2] or 0 for m in mod_rows),
                    }
                    changed = True
    conn.close()
    return resolved


def _resolve_module_imports(resolved, conn=None):
    """Expand resolved modules by walking the module-level import graph.

    For each module in resolved, look up its imports (both internal and external)
    and ensure those modules are included. This enables module-level assembly:
    only pull the modules actually needed, not entire packages.
    """
    if conn is None:
        conn = get_db()

    # Track which (package, module_name) pairs we've already processed
    seen_modules = set()
    changed = True

    while changed:
        changed = False

        for pkg_name, info in list(resolved.items()):
            version = info['version']
            pkg_normalized = pkg_name.replace('-', '_').replace('.', '_')

            # Get all modules for this package that we've resolved so far
            for module_name, module_path, size_bytes in info.get('modules', []):
                key = (pkg_name, module_name)
                if key in seen_modules:
                    continue
                seen_modules.add(key)

                # Look up what this module imports
                row = conn.execute(
                    "SELECT imports, imports_external FROM module_imports "
                    "WHERE package=? AND version=? AND module_name=?",
                    (pkg_name, version, module_name)
                ).fetchone()

                if not row:
                    continue

                # Process internal imports (same package)
                if row[0]:  # imports column
                    internal_imports = json.loads(row[0])
                    for imp in internal_imports:
                        # These are internal to this package - add to modules if not already there
                        # Normalize the module name
                        if not imp.startswith(pkg_normalized + '.'):
                            # Relative import like "sessions" -> "requests.sessions"
                            imp_name = f"{pkg_normalized}.{imp}"
                        else:
                            imp_name = imp

                        # Check if already in modules list
                        if imp_name in [m[0] for m in info['modules']]:
                            continue

                        # Find this module
                        mod_row = conn.execute(
                            "SELECT module_name, module_path, size_bytes FROM modules "
                            "WHERE package=? AND version=? AND module_name=?",
                            (pkg_name, version, imp_name)
                        ).fetchone()

                        if mod_row:
                            info['modules'].append((mod_row[0], mod_row[1], mod_row[2]))
                            info['total_size'] += mod_row[2] or 0
                            changed = True

                # Process external imports (other packages)
                if row[1]:  # imports_external column
                    external_imports = json.loads(row[1])

                    for imp in external_imports:
                        # Map import to package
                        top_level = imp.split('.')[0]
                        imp_pkg = IMPORT_TO_PACKAGE.get(top_level, top_level)

                        # Check if this is actually an internal import (miscategorized)
                        # by checking if it's a module in the same package
                        if imp_pkg == pkg_name or imp_pkg == pkg_normalized:
                            # It's internal, add to current package's modules
                            imp_name = f"{pkg_normalized}.{top_level}" if '.' not in imp else imp
                            if imp_name not in [m[0] for m in info['modules']]:
                                mod_row = conn.execute(
                                    "SELECT module_name, module_path, size_bytes FROM modules "
                                    "WHERE package=? AND version=? AND module_name LIKE ?",
                                    (pkg_name, version, f"{top_level}%")
                                ).fetchone()
                                if mod_row:
                                    info['modules'].append((mod_row[0], mod_row[1], mod_row[2]))
                                    info['total_size'] += mod_row[2] or 0
                                    changed = True
                            continue

                        # Resolve the package if not already in resolved
                        if imp_pkg not in resolved:
                            pkg_row = conn.execute(
                                "SELECT name, version, vault_path, has_native FROM packages "
                                "WHERE name=? ORDER BY cached_at DESC LIMIT 1",
                                (imp_pkg,)
                            ).fetchone()

                            if pkg_row:
                                dep_name, dep_version, dep_path, dep_native = pkg_row

                                # Find the specific module(s) needed
                                mod_rows = conn.execute(
                                    "SELECT module_name, module_path, size_bytes FROM modules "
                                    "WHERE package=? AND version=? AND module_name LIKE ?",
                                    (dep_name, dep_version, f"{imp}%")
                                ).fetchall()

                                if not mod_rows:
                                    mod_rows = conn.execute(
                                        "SELECT module_name, module_path, size_bytes FROM modules "
                                        "WHERE package=? AND version=?",
                                        (dep_name, dep_version)
                                    ).fetchall()

                                resolved[imp_pkg] = {
                                    'version': dep_version,
                                    'vault_path': dep_path,
                                    'has_native': bool(dep_native),
                                    'modules': [(m[0], m[1], m[2]) for m in mod_rows],
                                    'total_size': sum(m[2] or 0 for m in mod_rows),
                                }
                                changed = True

    return resolved


def scan_and_resolve(script_path):
    """Scan a script and resolve against the vault."""
    scan = scan_script(script_path)
    if not scan:
        return None
    
    conn = get_db()
    
    resolved = {}
    missing = {}
    
    for pkg_name, info in scan['external'].items():
        # Check vault
        row = conn.execute(
            "SELECT name, version, vault_path, has_native FROM packages WHERE name=? "
            "ORDER BY cached_at DESC LIMIT 1",
            (pkg_name,)
        ).fetchone()
        
        if row:
            name, version, vault_path, has_native = row
            
            # Find specific modules needed
            needed_modules = []
            for submod in info['submodules']:
                mod_rows = conn.execute(
                    "SELECT module_name, module_path, size_bytes FROM modules "
                    "WHERE package=? AND version=? AND module_name LIKE ?",
                    (name, version, f"{submod}%")
                ).fetchall()
                needed_modules.extend(mod_rows)
            
            # If no specific submodules found, include all
            if not needed_modules:
                needed_modules = conn.execute(
                    "SELECT module_name, module_path, size_bytes FROM modules "
                    "WHERE package=? AND version=?",
                    (name, version)
                ).fetchall()
            
            resolved[pkg_name] = {
                'version': version,
                'vault_path': vault_path,
                'has_native': bool(has_native),
                'modules': [(m[0], m[1], m[2]) for m in needed_modules],
                'total_size': sum(m[2] or 0 for m in needed_modules),
            }
        else:
            missing[pkg_name] = info
    
    conn.close()

    # Expand with transitive dependencies (package-level)
    resolved = _resolve_transitive(resolved)

    # Expand with module-level imports (specific modules needed)
    resolved = _resolve_module_imports(resolved)

    return {
        'scan': scan,
        'resolved': resolved,
        'missing': missing,
    }


def print_scan_report(result):
    """Pretty-print a scan/resolve result."""
    scan = result['scan'] if 'scan' in result else result
    
    print(f"\n  ┌─ Scan: {scan['script']}")
    print(f"  │")
    
    if scan.get('stdlib'):
        print(f"  ├─ Stdlib ({len(scan['stdlib'])}): {', '.join(scan['stdlib'][:10])}"
              + ("..." if len(scan['stdlib']) > 10 else ""))
    
    if scan.get('local'):
        print(f"  ├─ Local ({len(scan['local'])}): {', '.join(scan['local'])}")
    
    if 'resolved' in result:
        if result['resolved']:
            # Split by purity at display time
            pure = {k: v for k, v in result['resolved'].items() if not v.get('has_native')}
            native = {k: v for k, v in result['resolved'].items() if v.get('has_native')}

            if pure:
                print(f"  ├─ Pure ({len(pure)}):")
                for pkg, info in sorted(pure.items()):
                    size_kb = info['total_size'] / 1024
                    mod_count = len(info['modules'])
                    print(f"  │   ✓ {pkg}=={info['version']} "
                          f"({mod_count} modules, {size_kb:.1f}KB)")

            if native:
                print(f"  ├─ Native ({len(native)}) [arch-bound]:")
                for pkg, info in sorted(native.items()):
                    size_kb = info['total_size'] / 1024
                    mod_count = len(info['modules'])
                    print(f"  │   ⚠ {pkg}=={info['version']} "
                          f"({mod_count} modules, {size_kb:.1f}KB)")

        if result['missing']:
            print(f"  ├─ Missing from Vault:")
            for pkg, info in result['missing'].items():
                print(f"  │   ✗ {pkg} (import: {info['import_name']})")
    else:
        if scan.get('external'):
            print(f"  ├─ External ({len(scan['external'])}):")
            for pkg, info in scan['external'].items():
                submods = ', '.join(info['submodules'][:5])
                print(f"  │   • {pkg}: {submods}")
    
    print(f"  └─")


# ─────────────────────────────────────────────
# PACKAGE SCANNER — recursive directory scan
# ─────────────────────────────────────────────

def scan_package(pkg_path):
    """
    Recursively scan an entire package/directory for dependencies.
    Walks the tree, scans every source file, unions all external deps,
    and also checks for manifest files (requirements.txt, package.json).
    """
    pkg_path = Path(pkg_path).resolve()
    
    if not pkg_path.exists():
        print(f"  ✗ Path not found: {pkg_path}")
        return None
    
    # If it's a single file, just scan it directly
    if pkg_path.is_file():
        eco = _detect_ecosystem(str(pkg_path))
        if eco == 'npm':
            return scan_js_script(str(pkg_path))
        return scan_script(str(pkg_path))
    
    # It's a directory — walk it
    eco = _detect_package_ecosystem(pkg_path)
    
    all_external = {}       # pkg_name → merged info
    all_stdlib = set()
    all_local = set()
    all_imports = set()
    files_scanned = 0
    scan_errors = []
    
    # Determine which extensions to scan
    if eco == 'npm':
        extensions = {'.js', '.mjs', '.cjs', '.ts', '.tsx', '.mts'}
    else:
        extensions = {'.py'}
    
    # Walk the tree
    for root, dirs, files in os.walk(pkg_path):
        root_path = Path(root)
        
        # Skip common noise directories
        skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv',
                     'env', '.env', '.tox', '.mypy_cache', '.pytest_cache',
                     'dist', 'build', 'egg-info', '.egg-info', '.bubble'}
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.endswith('.egg-info')]
        
        for fname in files:
            fpath = root_path / fname
            if fpath.suffix.lower() not in extensions:
                continue
            
            files_scanned += 1
            
            try:
                if eco == 'npm':
                    result = scan_js_script(str(fpath))
                else:
                    result = scan_script(str(fpath))
                
                if result is None:
                    scan_errors.append(str(fpath))
                    continue
                
                # Merge stdlib
                all_stdlib.update(result.get('stdlib', []))
                
                # Merge local (but filter — what's local within the package is internal)
                for loc in result.get('local', []):
                    # For Python, check if it's a sibling in this package
                    if eco == 'pip':
                        # If the import resolves to a file within pkg_path, it's internal
                        candidate = root_path / loc
                        candidate_py = root_path / f"{loc}.py"
                        if not (candidate.exists() or candidate_py.exists()):
                            all_local.add(loc)
                    else:
                        # For JS, relative imports are always internal
                        pass
                
                # Merge external deps
                for pkg_name, info in result.get('external', {}).items():
                    if pkg_name in all_external:
                        # Merge submodules
                        existing = all_external[pkg_name]
                        existing_subs = set(existing.get('submodules', []))
                        existing_subs.update(info.get('submodules', []))
                        existing['submodules'] = sorted(existing_subs)
                        
                        # Merge from_imports
                        for mod, names in info.get('from_imports', {}).items():
                            if mod not in existing.get('from_imports', {}):
                                existing.setdefault('from_imports', {})[mod] = names
                            else:
                                merged = set(existing['from_imports'][mod])
                                merged.update(names if isinstance(names, list) else list(names))
                                existing['from_imports'][mod] = sorted(merged)
                    else:
                        all_external[pkg_name] = {
                            'import_name': info.get('import_name', pkg_name),
                            'submodules': info.get('submodules', []),
                            'from_imports': info.get('from_imports', {}),
                        }
                
                all_imports.update(result.get('all_imports', []))
                
            except Exception as e:
                scan_errors.append(f"{fpath}: {e}")
    
    # Check for manifest files and merge their declarations
    manifest_deps = _scan_manifests(pkg_path, eco)
    for pkg_name in manifest_deps:
        if pkg_name not in all_external:
            all_external[pkg_name] = {
                'import_name': pkg_name,
                'submodules': [],
                'from_imports': {},
                'source': 'manifest',
            }
    
    # Detect entry point
    entry_point = _find_entry_point(pkg_path, eco)
    
    result = {
        'script': str(pkg_path),
        'is_package': True,
        'ecosystem': eco,
        'entry_point': entry_point,
        'files_scanned': files_scanned,
        'scan_errors': scan_errors,
        'stdlib': sorted(all_stdlib),
        'external': all_external,
        'local': sorted(all_local),
        'all_imports': sorted(all_imports),
        'manifest_deps': sorted(manifest_deps),
    }
    
    return result


def scan_and_resolve_package(pkg_path):
    """Scan an entire package and resolve against the vault."""
    scan = scan_package(pkg_path)
    if not scan:
        return None
    
    eco = scan.get('ecosystem', 'pip')
    conn = get_db()
    resolved = {}
    missing = {}
    
    source_filter = "AND source='npm'" if eco == 'npm' else ""
    
    for pkg_name, info in scan['external'].items():
        row = conn.execute(
            f"SELECT name, version, vault_path, has_native FROM packages WHERE name=? "
            f"{source_filter} ORDER BY cached_at DESC LIMIT 1",
            (pkg_name,)
        ).fetchone()
        
        if row:
            name, version, vault_path, has_native = row
            
            needed_modules = []
            for submod in info.get('submodules', []):
                mod_rows = conn.execute(
                    "SELECT module_name, module_path, size_bytes FROM modules "
                    "WHERE package=? AND version=? AND module_name LIKE ?",
                    (name, version, f"{submod}%")
                ).fetchall()
                needed_modules.extend(mod_rows)
            
            if not needed_modules:
                needed_modules = conn.execute(
                    "SELECT module_name, module_path, size_bytes FROM modules "
                    "WHERE package=? AND version=?",
                    (name, version)
                ).fetchall()
            
            resolved[pkg_name] = {
                'version': version,
                'vault_path': vault_path,
                'has_native': bool(has_native),
                'modules': [(m[0], m[1], m[2]) for m in needed_modules],
                'total_size': sum(m[2] or 0 for m in needed_modules),
            }
        else:
            missing[pkg_name] = info
    
    conn.close()
    return {'scan': scan, 'resolved': resolved, 'missing': missing}


def print_package_report(result):
    """Pretty-print a package scan report."""
    scan = result['scan'] if 'scan' in result else result
    
    print(f"\n  ┌─ Package Scan: {scan['script']}")
    print(f"  │  [{scan.get('ecosystem', 'pip')}] "
          f"{scan.get('files_scanned', 0)} files scanned")
    
    if scan.get('entry_point'):
        print(f"  │  Entry: {scan['entry_point']}")
    
    if scan.get('scan_errors'):
        print(f"  │  ⚠ {len(scan['scan_errors'])} scan errors")
    
    print(f"  │")
    
    if scan.get('stdlib'):
        std_list = ', '.join(scan['stdlib'][:15])
        more = f"... +{len(scan['stdlib'])-15}" if len(scan['stdlib']) > 15 else ""
        print(f"  ├─ Stdlib ({len(scan['stdlib'])}): {std_list}{more}")
    
    if scan.get('manifest_deps'):
        print(f"  ├─ From manifests: {', '.join(scan['manifest_deps'][:10])}")
    
    if 'resolved' in result:
        if result['resolved']:
            # Split by purity at display time
            pure = {k: v for k, v in result['resolved'].items() if not v.get('has_native')}
            native = {k: v for k, v in result['resolved'].items() if v.get('has_native')}

            if pure:
                total_pure = sum(info['total_size'] for info in pure.values()) / 1024
                print(f"  ├─ Pure ({len(pure)}, {total_pure:.0f}KB):")
                for pkg, info in sorted(pure.items()):
                    print(f"  │   ✓ {pkg}=={info['version']}")

            if native:
                total_native = sum(info['total_size'] for info in native.values()) / 1024
                print(f"  ├─ Native ({len(native)}, {total_native:.0f}KB) [arch-bound]:")
                for pkg, info in sorted(native.items()):
                    print(f"  │   ⚠ {pkg}=={info['version']}")

        if result['missing']:
            print(f"  ├─ Missing ({len(result['missing'])}):")
            prefix = 'npm:' if scan.get('ecosystem') == 'npm' else ''
            for pkg in sorted(result['missing']):
                print(f"  │   ✗ {prefix}{pkg}")
    else:
        if scan.get('external'):
            print(f"  ├─ External ({len(scan['external'])}):")
            for pkg in sorted(scan['external']):
                print(f"  │   • {pkg}")
    
    print(f"  └─")


def _scan_manifests(pkg_path, ecosystem):
    """Check for manifest files and extract declared dependencies."""
    deps = set()
    
    if ecosystem == 'pip':
        # requirements.txt
        for req_file in ['requirements.txt', 'requirements-dev.txt', 
                         'requirements_dev.txt', 'reqs.txt']:
            req_path = pkg_path / req_file
            if req_path.exists():
                with open(req_path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#') or line.startswith('-'):
                            continue
                        # Extract package name (before any version specifier)
                        m = re.match(r'([A-Za-z0-9_.-]+)', line)
                        if m:
                            deps.add(m.group(1))
        
        # setup.py — look for install_requires (rough regex)
        setup_py = pkg_path / 'setup.py'
        if setup_py.exists():
            with open(setup_py, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            for m in re.finditer(r"['\"]([A-Za-z0-9_.-]+)(?:[><=!]|$)", content):
                candidate = m.group(1)
                if candidate not in STDLIB_MODULES and len(candidate) > 1:
                    deps.add(candidate)
        
        # pyproject.toml — look for dependencies
        pyproject = pkg_path / 'pyproject.toml'
        if pyproject.exists():
            with open(pyproject, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            # Simple extraction — not a full TOML parser
            in_deps = False
            for line in content.split('\n'):
                if 'dependencies' in line and '=' in line:
                    in_deps = True
                    continue
                if in_deps:
                    if line.strip().startswith(']'):
                        in_deps = False
                        continue
                    m = re.match(r'\s*["\']([A-Za-z0-9_.-]+)', line)
                    if m:
                        deps.add(m.group(1))
    
    elif ecosystem == 'npm':
        # package.json
        pkg_json = pkg_path / 'package.json'
        if pkg_json.exists():
            try:
                with open(pkg_json) as f:
                    data = json.load(f)
                deps.update(data.get('dependencies', {}).keys())
                deps.update(data.get('devDependencies', {}).keys())
            except (json.JSONDecodeError, KeyError):
                pass
    
    return deps


def _find_entry_point(pkg_path, ecosystem):
    """Try to find the main entry point of a package."""
    if ecosystem == 'npm':
        pkg_json = pkg_path / 'package.json'
        if pkg_json.exists():
            try:
                with open(pkg_json) as f:
                    data = json.load(f)
                main = data.get('main') or data.get('bin')
                if isinstance(main, str):
                    return main
                if isinstance(main, dict):
                    return list(main.values())[0] if main else None
            except (json.JSONDecodeError, KeyError):
                pass
        # Fallback
        for candidate in ['index.js', 'index.mjs', 'src/index.js', 'src/index.ts',
                          'app.js', 'server.js', 'main.js']:
            if (pkg_path / candidate).exists():
                return candidate
    else:
        # Python
        for candidate in ['__main__.py', 'main.py', 'app.py', 'cli.py',
                          'src/__main__.py', 'src/main.py']:
            if (pkg_path / candidate).exists():
                return candidate
        # Check setup.py/pyproject.toml for console_scripts
        setup_py = pkg_path / 'setup.py'
        if setup_py.exists():
            with open(setup_py, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            m = re.search(r'console_scripts.*?["\'](\w+)\s*=\s*([^"\']+)', content, re.DOTALL)
            if m:
                return m.group(2).strip()
    
    return None


# ─────────────────────────────────────────────
# Layer 3: BUBBLE
# ─────────────────────────────────────────────

def _detect_ecosystem(script_path):
    """Detect whether a script is Python or JS/TS."""
    p = Path(script_path)
    if p.is_dir():
        return _detect_package_ecosystem(p)
    ext = p.suffix.lower()
    if ext in ('.js', '.mjs', '.cjs', '.ts', '.tsx', '.mts'):
        return 'npm'
    return 'pip'


def _detect_package_ecosystem(pkg_path):
    """Detect ecosystem for a directory/package."""
    pkg_path = Path(pkg_path)
    # Check for strong signals
    if (pkg_path / 'package.json').exists():
        return 'npm'
    if (pkg_path / 'setup.py').exists() or (pkg_path / 'pyproject.toml').exists():
        return 'pip'
    if (pkg_path / 'requirements.txt').exists():
        return 'pip'
    
    # Count files
    py_count = len(list(pkg_path.rglob('*.py')))
    js_count = len(list(pkg_path.rglob('*.js'))) + len(list(pkg_path.rglob('*.ts')))
    
    return 'npm' if js_count > py_count else 'pip'


def bubble_up(script_path, keep=False, extra_args=None):
    """Create an ephemeral bubble, run the script or package, dissolve. Auto-detects ecosystem."""
    p = Path(script_path)
    eco = _detect_ecosystem(script_path)
    
    # If it's a directory, use the package scanner and find the entry point
    if p.is_dir():
        return bubble_up_package(p, keep=keep, extra_args=extra_args)
    
    if eco == 'npm':
        return bubble_up_js(script_path, keep=keep, extra_args=extra_args)
    return bubble_up_py(script_path, keep=keep, extra_args=extra_args)


def bubble_up_package(pkg_path, keep=False, extra_args=None):
    """Bubble up an entire package directory."""
    init_db()
    pkg_path = Path(pkg_path).resolve()
    
    print(f"\n  ◉ Bubble Up (Package): {pkg_path.name}")
    print(f"  │")
    
    # Scan the whole package
    result = scan_and_resolve_package(pkg_path)
    if not result:
        print(f"  ✗ Package scan failed")
        return 1
    
    scan = result['scan']
    eco = scan.get('ecosystem', 'pip')
    print(f"  │ Ecosystem: {eco} | {scan.get('files_scanned', 0)} files scanned")
    
    # Auto-vault missing packages
    if result['missing']:
        print(f"  │ Missing packages — attempting auto-vault...")
        for pkg_name in result['missing']:
            # Skip Node built-ins (node:* prefix)
            if pkg_name.startswith('node:'):
                continue
            if eco == 'npm':
                npm_vault_add(pkg_name)
            else:
                vault_add(pkg_name)
        result = scan_and_resolve_package(pkg_path)
        scan = result['scan']
    
    if result['missing']:
        print(f"  │ ⚠ Still missing:")
        for pkg in result['missing']:
            print(f"  │   • {pkg}")
    
    # Find entry point
    entry = scan.get('entry_point')
    if not entry:
        # Try to guess
        print(f"  │ ⚠ No entry point found")
        if extra_args:
            entry = extra_args[0]
            extra_args = extra_args[1:]
            print(f"  │   Using argument: {entry}")
        else:
            print(f"  ✗ Cannot determine entry point. Pass it as an argument:")
            print(f"  ✗   bubble up {pkg_path} main.py")
            return 1
    
    entry_path = pkg_path / entry
    if not entry_path.exists():
        print(f"  ✗ Entry point not found: {entry_path}")
        return 1
    
    print(f"  │ Entry: {entry}")
    
    # Create bubble
    bubble_id = hashlib.md5(
        f"{pkg_path}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:12]
    
    bubble_dir = BUBBLES_DIR / bubble_id
    
    if eco == 'npm':
        bubble_lib = bubble_dir / "node_modules"
    else:
        bubble_lib = bubble_dir / "lib"
    bubble_lib.mkdir(parents=True)
    
    print(f"  │ Bubble: {bubble_id}")
    
    # Path shims
    shim_overrides, shimmed = setup_path_shims(bubble_dir, verbose=True)
    if shimmed:
        print(f"  │ Shimmed {len(shimmed)} paths")
    
    # Assemble dependencies
    assembled = []
    for pkg_name, info in result.get('resolved', {}).items():
        vault_path = Path(info['vault_path'])
        if not vault_path.exists():
            continue
        
        if eco == 'npm':
            pkg_subdir = vault_path / 'package'
            if not pkg_subdir.exists():
                pkg_subdir = vault_path
            if pkg_name.startswith('@'):
                scope, name = pkg_name.split('/', 1)
                dest_dir = bubble_lib / scope
                dest_dir.mkdir(exist_ok=True)
                dest = dest_dir / name
            else:
                dest = bubble_lib / pkg_name
            if not dest.exists():
                try:
                    os.symlink(pkg_subdir, dest)
                except OSError:
                    shutil.copytree(pkg_subdir, dest)
                assembled.append(pkg_name)
        else:
            for item in vault_path.iterdir():
                if item.name.endswith('.dist-info') or item.name.endswith('.data'):
                    continue
                dest = bubble_lib / item.name
                if not dest.exists():
                    try:
                        os.symlink(item, dest)
                    except OSError:
                        if item.is_dir():
                            shutil.copytree(item, dest)
                        else:
                            shutil.copy2(item, dest)
                    assembled.append(pkg_name)
    
    if assembled:
        print(f"  │ Assembled: {', '.join(sorted(set(assembled)))}")
    
    # Build environment
    env = os.environ.copy()
    env.update(shim_overrides)
    env['BUBBLE_ID'] = bubble_id
    env['BUBBLE_DIR'] = str(bubble_dir)
    
    if eco == 'npm':
        env['NODE_PATH'] = str(bubble_lib)
        runner = 'node'
        if entry_path.suffix in ('.ts', '.tsx'):
            runner = 'tsx'
        cmd = [runner, str(entry_path)]
    else:
        existing_pypath = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = str(bubble_lib) + (f":{existing_pypath}" if existing_pypath else "")
        cmd = [sys.executable, str(entry_path)]
    
    if extra_args:
        cmd.extend(extra_args)
    
    # Record
    conn = get_db()
    conn.execute(
        "INSERT INTO bubbles (bubble_id, created_at, script_path, status, bubble_path, packages) "
        "VALUES (?, ?, ?, 'active', ?, ?)",
        (bubble_id, datetime.now().isoformat(), str(pkg_path), str(bubble_dir),
         json.dumps(list(result.get('resolved', {}).keys())))
    )
    conn.commit()
    conn.close()
    
    # Run
    print(f"  │")
    print(f"  ├─── Run ───────────────────")
    print(f"  │")
    
    start = datetime.now()
    proc = subprocess.run(cmd, env=env, cwd=str(pkg_path))
    elapsed = (datetime.now() - start).total_seconds()
    
    print(f"  │")
    print(f"  ├─── End ({elapsed:.2f}s) ──────────")
    
    if keep:
        print(f"  │ Kept: {bubble_dir}")
    else:
        shutil.rmtree(bubble_dir, ignore_errors=True)
        conn = get_db()
        conn.execute("UPDATE bubbles SET status='dissolved' WHERE bubble_id=?", (bubble_id,))
        conn.commit()
        conn.close()
        print(f"  │ Dissolved")
    
    returncode = proc.returncode
    status = "✓" if returncode == 0 else f"✗ (exit {returncode})"
    print(f"  └─ {status}")
    return returncode


def bubble_up_py(script_path, keep=False, extra_args=None):
    """Create an ephemeral bubble for a Python script, run, dissolve."""
    init_db()
    script_path = Path(script_path).resolve()
    
    print(f"\n  ◉ Bubble Up: {script_path.name}")
    print(f"  │")
    
    # Step 1: Scan and resolve
    result = scan_and_resolve(script_path)
    if not result:
        print(f"  ✗ Scan failed")
        return 1
    
    if result['missing']:
        print(f"  │ Missing packages — attempting auto-vault...")
        for pkg_name in result['missing']:
            vault_add(pkg_name)
        # Re-resolve
        result = scan_and_resolve(script_path)
    
    if result['missing']:
        print(f"  │")
        print(f"  │ ⚠ Still missing (will try system fallback):")
        for pkg in result['missing']:
            print(f"  │   • {pkg}")
    
    # Step 2: Create bubble
    bubble_id = hashlib.md5(
        f"{script_path}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:12]
    
    bubble_dir = BUBBLES_DIR / bubble_id
    bubble_lib = bubble_dir / "lib"
    bubble_lib.mkdir(parents=True)
    
    print(f"  │ Bubble: {bubble_id}")
    
    # Step 3a: Set up path shims (proot/Termux compensation)
    shim_overrides, shimmed = setup_path_shims(bubble_dir, verbose=True)
    if shimmed:
        print(f"  │ Shimmed {len(shimmed)} paths")
    
    # Step 3b: Assemble — symlink/copy resolved modules into bubble
    # Use the module index to mirror exact paths, not whole packages.
    # info['modules'] is list of (module_name, module_path, size_bytes)
    assembled = []
    for pkg_name, info in result.get('resolved', {}).items():
        vault_path = Path(info['vault_path'])
        if not vault_path.exists():
            continue

        for module_name, module_path, _ in info.get('modules', []):
            src = vault_path / module_path
            if not src.exists():
                # Fallback: whole-package symlink if module not found individually
                # (can happen for non-Python files like .so bundled at package root)
                for item in vault_path.iterdir():
                    if item.name == module_path or item.stem == module_name.split('.')[-1]:
                        src = item
                        break
                if not src.exists():
                    continue

            # Preserve directory structure in bubble_lib
            dest = bubble_lib / module_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                try:
                    os.symlink(src, dest)
                except OSError:
                    if src.is_dir():
                        shutil.copytree(src, dest)
                    else:
                        shutil.copy2(src, dest)
                assembled.append(pkg_name)
    
    if assembled:
        print(f"  │ Assembled: {', '.join(set(assembled))}")
    
    # Step 4: Build environment
    env = os.environ.copy()
    
    # Apply path shim overrides (SSL certs, etc.)
    env.update(shim_overrides)
    
    # Prepend bubble lib to Python path
    existing_pypath = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = str(bubble_lib) + (f":{existing_pypath}" if existing_pypath else "")
    env['BUBBLE_ID'] = bubble_id
    env['BUBBLE_DIR'] = str(bubble_dir)
    
    # Record bubble
    conn = get_db()
    conn.execute(
        "INSERT INTO bubbles (bubble_id, created_at, script_path, status, bubble_path, packages) "
        "VALUES (?, ?, ?, 'active', ?, ?)",
        (bubble_id, datetime.now().isoformat(), str(script_path), str(bubble_dir),
         json.dumps(list(result.get('resolved', {}).keys())))
    )
    conn.commit()
    conn.close()
    
    # Step 5: Run with error-driven retry loop
    print(f"  │")
    print(f"  ├─── Run ───────────────────")
    print(f"  │")

    cmd = [sys.executable, str(script_path)]
    if extra_args:
        cmd.extend(extra_args)

    max_retries = 20  # Safety limit
    retry_count = 0
    start = datetime.now()

    while retry_count < max_retries:
        proc = subprocess.run(cmd, env=env, cwd=str(script_path.parent), capture_output=True, text=True)

        if proc.returncode == 0:
            # Success - print any output and exit
            if proc.stdout:
                print(proc.stdout.rstrip())
            break

        # Check for ModuleNotFoundError
        stderr = proc.stderr
        if 'ModuleNotFoundError' in stderr or 'ImportError' in stderr:
            # Extract module name from error
            import re
            match = re.search(r"No module named '([^']+)'", stderr)
            if not match:
                match = re.search(r"cannot import name '([^']+)'", stderr)
            if match:
                missing_module = match.group(1)
                print(f"  │ ⚠ Missing module: {missing_module} — resolving...")

                # Find which package provides this module
                top_level = missing_module.split('.')[0]
                pkg_name = IMPORT_TO_PACKAGE.get(top_level, top_level)

                # Check if it's in the vault
                conn = get_db()
                row = conn.execute(
                    "SELECT name, version, vault_path FROM packages WHERE name=? "
                    "ORDER BY cached_at DESC LIMIT 1",
                    (pkg_name,)
                ).fetchone()
                conn.close()

                if row:
                    pkg_name, version, vault_path = row
                    print(f"  │   Found in vault: {pkg_name}=={version}")

                    # Add missing module to bubble
                    added = False
                    for module_path in Path(vault_path).rglob("*.py"):
                        rel = module_path.relative_to(vault_path)
                        parts = list(rel.parts)
                        if any(p.endswith('.dist-info') for p in parts):
                            continue
                        if parts[-1] == '__init__.py':
                            parts = parts[:-1]
                        else:
                            parts[-1] = parts[-1].replace('.py', '')
                        mod_name = '.'.join(parts)

                        # Check if this module matches what we need
                        if mod_name == missing_module or mod_name.startswith(missing_module + '.'):
                            dest = bubble_lib / str(rel)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if not dest.exists():
                                try:
                                    os.symlink(module_path, dest)
                                except OSError:
                                    shutil.copy2(module_path, dest)
                                added = True

                    if added:
                        print(f"  │   Added to bubble, retrying...")
                        retry_count += 1
                        continue
                    else:
                        print(f"  │   Module not found in package, trying system fallback...")
                else:
                    # Not in vault — vault it and add directly to bubble
                    print(f"  │   Not in vault, downloading {pkg_name}...")
                    vault_add(pkg_name)

                    # Get the newly vaulted package and add to bubble
                    conn = get_db()
                    row = conn.execute(
                        "SELECT name, version, vault_path FROM packages WHERE name=? "
                        "ORDER BY cached_at DESC LIMIT 1",
                        (pkg_name,)
                    ).fetchone()
                    conn.close()

                    if row:
                        pkg_name_resolved, version, vault_path = row
                        vault_path = Path(vault_path)

                        # Add all modules from this package to the bubble
                        for item in vault_path.iterdir():
                            if item.name.endswith('.dist-info') or item.name.endswith('.data'):
                                continue
                            dest = bubble_lib / item.name
                            if not dest.exists():
                                try:
                                    os.symlink(item, dest)
                                except OSError:
                                    if item.is_dir():
                                        shutil.copytree(item, dest)
                                    else:
                                        shutil.copy2(item, dest)

                        print(f"  │   Added {pkg_name}, retrying...")
                        retry_count += 1
                        continue
                    else:
                        print(f"  │   Could not vault {pkg_name}")
                        break
            else:
                # Could not parse error
                print(proc.stderr.rstrip())
                break
        else:
            # Non-import error - print and exit
            if proc.stdout:
                print(proc.stdout.rstrip())
            if proc.stderr:
                print(proc.stderr.rstrip(), file=sys.stderr)
            break

    elapsed = (datetime.now() - start).total_seconds()

    print(f"  │")
    if retry_count > 0:
        print(f"  │ Retries: {retry_count}")
    print(f"  ├─── End ({elapsed:.2f}s) ──────────")

    # Step 6: Dissolve (unless --keep)
    if keep:
        print(f"  │ Kept: {bubble_dir}")
    else:
        shutil.rmtree(bubble_dir, ignore_errors=True)
        conn = get_db()
        conn.execute("UPDATE bubbles SET status='dissolved' WHERE bubble_id=?", (bubble_id,))
        conn.commit()
        conn.close()
        print(f"  │ Dissolved")
    
    returncode = proc.returncode
    status = "✓" if returncode == 0 else f"✗ (exit {returncode})"
    print(f"  └─ {status}")
    
    return returncode


def bubble_down(all_bubbles=False):
    """Clean up bubbles."""
    init_db()
    conn = get_db()
    
    if all_bubbles:
        rows = conn.execute("SELECT bubble_id, bubble_path FROM bubbles WHERE status='active'").fetchall()
    else:
        rows = conn.execute(
            "SELECT bubble_id, bubble_path FROM bubbles WHERE status='active' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchall()
    
    if not rows:
        print("  No active bubbles")
        return
    
    for bid, bpath in rows:
        bpath = Path(bpath)
        if bpath.exists():
            shutil.rmtree(bpath, ignore_errors=True)
        conn.execute("UPDATE bubbles SET status='dissolved' WHERE bubble_id=?", (bid,))
        print(f"  ✓ Dissolved: {bid}")
    
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# DOCTOR — Environment Diagnostics
# ─────────────────────────────────────────────

def doctor():
    """Diagnose the bubble environment."""
    init_db()
    
    print("\n  ┌─ Bubble Doctor")
    print(f"  │")
    
    # Python
    print(f"  ├─ Python: {sys.version.split()[0]} at {sys.executable}")
    
    # Platform
    import platform
    print(f"  ├─ Platform: {platform.machine()} / {platform.system()}")
    
    # Detect environment
    in_termux = 'com.termux' in os.environ.get('PREFIX', '')
    in_proot = os.path.exists('/proc/self/status') and 'proot' in open('/proc/self/status').read().lower() if os.path.exists('/proc/self/status') else False
    
    if in_termux:
        print(f"  ├─ Environment: Termux detected")
    if in_proot:
        print(f"  ├─ Environment: proot detected")
    
    # Vault status
    conn = get_db()
    pkg_count = conn.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
    mod_count = conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
    native_count = conn.execute("SELECT COUNT(*) FROM packages WHERE has_native=1").fetchone()[0]
    pure_count = pkg_count - native_count
    bubble_count = conn.execute("SELECT COUNT(*) FROM bubbles WHERE status='active'").fetchone()[0]
    conn.close()

    print(f"  ├─ Vault: {pkg_count} packages ({pure_count} pure, {native_count} native)")
    print(f"  │        {mod_count} modules indexed")
    print(f"  ├─ Active bubbles: {bubble_count}")

    if native_count > 0:
        print(f"  │")
        print(f"  ├─ ⚠ {native_count} native packages are arch-bound")
        print(f"  │  They were compiled for {platform.machine()}.")
        print(f"  │  If running on different hardware, they may need")
        print(f"  │  re-resolution from source.")
    
    # Disk usage
    vault_size = sum(f.stat().st_size for f in VAULT_DIR.rglob('*') if f.is_file()) if VAULT_DIR.exists() else 0
    print(f"  ├─ Vault size: {vault_size / (1024*1024):.1f} MB")
    
    # Symlink support
    try:
        test_link = BUBBLE_HOME / '.symlink_test'
        test_target = BUBBLE_HOME / '.symlink_target'
        test_target.touch()
        os.symlink(test_target, test_link)
        test_link.unlink()
        test_target.unlink()
        print(f"  ├─ Symlinks: ✓ supported")
    except OSError:
        print(f"  ├─ Symlinks: ✗ not supported (will use copies)")
    
    # pip availability
    pip_result = subprocess.run([sys.executable, '-m', 'pip', '--version'], 
                                capture_output=True, text=True)
    if pip_result.returncode == 0:
        pip_ver = pip_result.stdout.strip().split('\n')[0]
        print(f"  ├─ pip: {pip_ver}")
    else:
        print(f"  ├─ pip: ✗ not available")
    
    # npm/node availability
    node_result = subprocess.run(['node', '--version'], capture_output=True, text=True)
    if node_result.returncode == 0:
        print(f"  ├─ node: {node_result.stdout.strip()}")
    else:
        print(f"  ├─ node: ✗ not available")
    
    npm_result = subprocess.run(['npm', '--version'], capture_output=True, text=True)
    if npm_result.returncode == 0:
        print(f"  ├─ npm: {npm_result.stdout.strip()}")
    else:
        print(f"  ├─ npm: ✗ not available")
    
    # Path shim status
    shim_count = 0
    for expected_path, shim_config in PATH_SHIMS.items():
        if shim_config['type'] == 'search':
            for candidate in shim_config['candidates']:
                resolved = _resolve_shim_var(candidate)
                if resolved and os.path.exists(resolved):
                    shim_count += 1
                    break
        elif shim_config['type'] == 'create':
            shim_count += 1
    print(f"  ├─ Path shims: {shim_count}/{len(PATH_SHIMS)} resolvable")
    
    print(f"  │")
    print(f"  └─ Home: {BUBBLE_HOME}")


def preflight(script_path):
    """
    Dry-run: scan a script and report everything needed to run it offline.
    Produces a shopping list for JANUS or the operator.
    """
    init_db()
    script_path = Path(script_path)
    eco = _detect_ecosystem(script_path)
    
    print(f"\n  ┌─ Preflight: {script_path.name} [{eco}]")
    print(f"  │")
    
    if eco == 'npm':
        result = scan_and_resolve_js(script_path)
    else:
        result = scan_and_resolve(script_path)
    
    if not result:
        print(f"  ✗ Scan failed")
        return
    
    scan = result['scan']
    resolved = result.get('resolved', {})
    missing = result.get('missing', {})
    
    # What's already covered
    if resolved:
        print(f"  ├─ ✓ Ready ({len(resolved)}):")
        for pkg, info in resolved.items():
            size_kb = info['total_size'] / 1024
            native = " [native]" if info['has_native'] else ""
            print(f"  │   {pkg}=={info['version']} ({size_kb:.1f}KB){native}")
    
    # What's missing
    if missing:
        print(f"  │")
        print(f"  ├─ ✗ Need to vault ({len(missing)}):")
        prefix = 'npm:' if eco == 'npm' else ''
        for pkg in missing:
            print(f"  │   bubble vault add {prefix}{pkg} --recursive")
    
    # Check transitive deps of resolved packages
    conn = get_db()
    transitive_missing = []
    for pkg_name in resolved:
        deps = conn.execute(
            "SELECT dep_name FROM dependencies WHERE package=? AND optional=0",
            (pkg_name,)
        ).fetchall()
        for (dep_name,) in deps:
            source_filter = "AND source='npm'" if eco == 'npm' else "AND source!='npm'"
            in_vault = conn.execute(
                f"SELECT 1 FROM packages WHERE name=? {source_filter}", (dep_name,)
            ).fetchone()
            if not in_vault and dep_name not in resolved and dep_name not in missing:
                transitive_missing.append((pkg_name, dep_name))
    conn.close()
    
    if transitive_missing:
        print(f"  │")
        print(f"  ├─ ⚠ Transitive deps missing:")
        for parent, dep in transitive_missing:
            print(f"  │   {dep} (needed by {parent})")
    
    # Path shim readiness
    shim_gaps = []
    for expected_path, shim_config in PATH_SHIMS.items():
        if shim_config['type'] == 'search':
            found = False
            for candidate in shim_config['candidates']:
                resolved_path = _resolve_shim_var(candidate)
                if resolved_path and os.path.exists(resolved_path):
                    found = True
                    break
            if not found:
                shim_gaps.append(expected_path)
    
    if shim_gaps:
        print(f"  │")
        print(f"  ├─ ⚠ Path shims unresolvable:")
        for gap in shim_gaps:
            print(f"  │   {gap}")
    
    # Summary
    total_needed = len(missing) + len(transitive_missing)
    if total_needed == 0 and not shim_gaps:
        print(f"  │")
        print(f"  └─ ✓ Ready for offline operation")
    else:
        print(f"  │")
        print(f"  └─ {total_needed} packages + {len(shim_gaps)} paths to resolve before going offline")


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _extract_version_from_filename(filename, package_name):
    """Extract version from a wheel/tarball filename."""
    # Wheel: package-1.2.3-py3-none-any.whl
    # Sdist: package-1.2.3.tar.gz
    name_normalized = re.sub(r'[-_.]+', '[-_.]+', re.escape(package_name))
    m = re.match(rf'{name_normalized}-([^-]+)', filename, re.IGNORECASE)
    if m:
        ver = m.group(1)
        # Strip .tar, .zip etc
        ver = re.sub(r'\.(tar|zip)$', '', ver)
        return ver
    return 'unknown'


def _index_package(name, version, vault_path, has_native, source):
    """Add a package to the database, indexing both Python and native modules."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO packages (name, version, source, cached_at, vault_path, has_native) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, version, source, datetime.now().isoformat(), str(vault_path), int(has_native))
    )

    is_native_pkg = 1 if has_native else 0

    # Index Python modules and their imports
    for py_file in vault_path.rglob("*.py"):
        rel = py_file.relative_to(vault_path)
        parts = list(rel.parts)

        # Skip .dist-info
        if any(p.endswith('.dist-info') for p in parts):
            continue

        if parts[-1] == '__init__.py':
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1].replace('.py', '')

        if not parts:
            continue

        module_name = '.'.join(parts)

        conn.execute(
            "INSERT OR REPLACE INTO modules (package, version, module_name, module_path, is_native, size_bytes) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (name, version, module_name, str(rel), py_file.stat().st_size)
        )

        # Scan for imports and categorize as internal/external
        try:
            with open(py_file, 'r', encoding='utf-8', errors='replace') as f:
                source = f.read()
            tree = ast.parse(source)
            scanner = ImportScanner()
            scanner.visit(tree)

            internal_imports = []
            external_imports = []

            # Determine the package prefix for this module
            pkg_prefix = name.replace('-', '_').replace('.', '_')
            # Also try common import-name mappings
            import_to_pkg = {v: k for k, v in IMPORT_TO_PACKAGE.items()}
            pkg_import_name = import_to_pkg.get(name, name.replace('-', '_').replace('.', '_'))

            for imp in scanner.imports:
                # Skip stdlib
                if imp in STDLIB_MODULES:
                    continue

                # Check if this is internal (same package) or external
                top_level = imp.split('.')[0]
                # Map import name to package name
                imp_pkg = IMPORT_TO_PACKAGE.get(top_level, top_level)

                # Normalize for comparison
                imp_normalized = imp_pkg.replace('-', '_').replace('.', '_')
                pkg_normalized = pkg_import_name.replace('-', '_').replace('.', '_')

                if imp_normalized == pkg_normalized or imp.startswith(pkg_prefix + '.'):
                    # Internal import - same package
                    internal_imports.append(imp)
                else:
                    external_imports.append(imp)

            if internal_imports or external_imports:
                conn.execute(
                    "INSERT OR REPLACE INTO module_imports "
                    "(package, version, module_name, imports, imports_external) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, version, module_name,
                     json.dumps(internal_imports) if internal_imports else None,
                     json.dumps(external_imports) if external_imports else None)
                )
        except (SyntaxError, Exception):
            # Skip files that can't be parsed
            pass

    # Index native extensions (.so, .pyd)
    # Filename format: module_name.extension+platform.tag.so
    # e.g. _multiarray_umath.cpython-313-aarch64-linux-gnu.so
    #       → module path: numpy/_core/_multiarray_umath.so
    for native_file in vault_path.rglob("*.so"):
        rel = native_file.relative_to(vault_path)
        parts = list(rel.parts)

        # Skip .dist-info
        if any(p.endswith('.dist-info') for p in parts):
            continue

        # Strip platform tag from filename: foo.cpython-313-aarch64-linux-gnu.so → foo.so
        stem = parts[-1]
        for ext in ('.pyd', '.so'):
            if stem.endswith(ext):
                base = stem[:-len(ext)]
                # Remove platform tag suffix (cpython version + arch + OS)
                # e.g. cpython-313-aarch64-linux-gnu
                idx = base.rfind('.cpython-')
                if idx >= 0:
                    base = base[:idx]
                parts[-1] = base + ext
                break

        if not parts or parts[-1] in ('.so', '.pyd'):
            continue

        # Derive module name: numpy/_core/_multiarray_umath.so → numpy._core._multiarray_umath
        module_parts = list(parts)
        for ext in ('.so', '.pyd'):
            if module_parts[-1].endswith(ext):
                module_parts[-1] = module_parts[-1][:-len(ext)]
                break
        if not module_parts[-1]:
            continue
        module_name = '.'.join(module_parts)

        conn.execute(
            "INSERT OR REPLACE INTO modules (package, version, module_name, module_path, is_native, size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, version, module_name, str(rel), is_native_pkg, native_file.stat().st_size)
        )

    conn.commit()
    conn.close()


def _index_dependencies(name, version, vault_path):
    """Extract dependencies from package metadata."""
    conn = get_db()
    
    # Look for METADATA or PKG-INFO in .dist-info
    for dist_info in vault_path.rglob("METADATA"):
        with open(dist_info, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('Requires-Dist:'):
                    dep_str = line[len('Requires-Dist:'):].strip()
                    # Parse: "package (>=1.0)" or "package (>=1.0) ; extra == 'dev'"
                    optional = '; extra' in dep_str or '; python_version' in dep_str
                    
                    # Extract name and version spec
                    m = re.match(r'([A-Za-z0-9_.-]+)\s*(?:\(([^)]+)\))?', dep_str)
                    if m:
                        dep_name = m.group(1).strip()
                        dep_spec = m.group(2) or ''
                        
                        conn.execute(
                            "INSERT OR IGNORE INTO dependencies "
                            "(package, version, dep_name, dep_version_spec, optional) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (name, version, dep_name, dep_spec, int(optional))
                        )
    
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# NPM VAULT — Layer 1 for Node.js
# ─────────────────────────────────────────────

# Allowlist of npm registry hosts we'll fetch tarballs from. Refuses MITM
# / poisoned-mirror responses that try to redirect tarball_url to file:// or
# an attacker-controlled host.
_NPM_ALLOWED_TARBALL_HOSTS = frozenset({
    "registry.npmjs.org",
    "registry.npmjs.com",
})


def _npm_tarball_url_ok(url):
    """Validate that an npm registry-supplied tarball URL is https and on a known host."""
    import urllib.parse
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in _NPM_ALLOWED_TARBALL_HOSTS


def npm_vault_add(package_name, version=None):
    """Download an npm package tarball directly from the registry — no npm binary, no lockfile."""
    import urllib.request
    import urllib.error
    init_db()

    print(f"  ↓ Fetching npm:{package_name}" + (f"@{version}" if version else ""))

    # ── Step 1: Resolve version via registry JSON API ────────────────────────
    # Scoped packages: @scope/name → registry path is %40scope%2Fname
    encoded_name = package_name.replace('/', '%2F').replace('@', '%40') if package_name.startswith('@') else package_name
    registry_url = f"https://registry.npmjs.org/{encoded_name}"

    try:
        with urllib.request.urlopen(registry_url, timeout=30) as resp:
            meta = json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as e:
        print(f"  ✗ Registry fetch failed: {e}")
        return False
    except json.JSONDecodeError as e:
        print(f"  ✗ Registry response malformed: {e}")
        return False

    # Resolve to concrete version
    if not version:
        version = meta.get('dist-tags', {}).get('latest')
        if not version:
            print(f"  ✗ Could not resolve latest version for {package_name}")
            return False

    version_meta = meta.get('versions', {}).get(version)
    if not version_meta:
        print(f"  ✗ Version {version} not found in registry for {package_name}")
        return False

    tarball_url = version_meta.get('dist', {}).get('tarball')
    if not tarball_url:
        print(f"  ✗ No tarball URL in registry metadata")
        return False
    if not _npm_tarball_url_ok(tarball_url):
        print(f"  ✗ Refusing tarball URL from registry: {tarball_url}")
        return False

    # ── Step 2: Download tarball directly ───────────────────────────────────
    dl_dir = WHEELS_DIR / f"npm_{package_name.replace('/', '_').replace('@', '')}_dl"
    dl_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = dl_dir / f"{package_name.replace('/', '-').replace('@', '')}-{version}.tgz"

    try:
        urllib.request.urlretrieve(tarball_url, tarball_path)
    except urllib.error.URLError as e:
        print(f"  ✗ Tarball download failed: {e}")
        shutil.rmtree(dl_dir, ignore_errors=True)
        return False

    print(f"  ✓ Downloaded: {tarball_path.name}")

    # ── Step 3: Unpack into vault ────────────────────────────────────────────
    pkg_vault_dir = VAULT_DIR / f"npm_{package_name.replace('/', '_').replace('@', '')}" / f"{version}"
    if pkg_vault_dir.exists():
        shutil.rmtree(pkg_vault_dir)
    pkg_vault_dir.mkdir(parents=True)

    has_native = False
    try:
        with tarfile.open(tarball_path, 'r:gz') as tf:
            # filter='data' rejects unsafe members on 3.12+; we also pre-scan
            # so the failure mode is explicit on older runtimes. Walk the
            # member list once and reuse it for both validation and has_native.
            members = tf.getmembers()
            for member in members:
                n = member.name
                if n.startswith('/') or '..' in Path(n).parts or '\x00' in n:
                    raise ValueError(f"unsafe tar member: {n!r}")
                if member.issym() or member.islnk():
                    raise ValueError(f"link member not permitted: {n!r}")
            has_native = any(m.name.endswith(('.node', '.gyp')) for m in members)
            tf.extractall(pkg_vault_dir, filter='data')
    except (tarfile.TarError, ValueError) as e:
        print(f"  ✗ Extraction failed: {e}")
        shutil.rmtree(dl_dir, ignore_errors=True)
        return False

    # npm tarballs always extract into a 'package/' subdir
    pkg_subdir = pkg_vault_dir / 'package'
    if not pkg_subdir.exists():
        pkg_subdir = pkg_vault_dir  # fallback if structure differs

    # ── Step 4: Read deps from registry meta (more reliable than package.json) ──
    deps = version_meta.get('dependencies', {})
    peer_deps = version_meta.get('peerDependencies', {})
    # We vault hard deps only; peer deps are advisory
    confirmed_version = version_meta.get('version', version)

    # ── Step 5: Index in database ────────────────────────────────────────────
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO packages (name, version, source, cached_at, vault_path, has_native, metadata) "
        "VALUES (?, ?, 'npm', ?, ?, ?, ?)",
        (package_name, confirmed_version, datetime.now().isoformat(), str(pkg_vault_dir),
         int(has_native), json.dumps({'ecosystem': 'npm', 'peer_deps': list(peer_deps.keys())}))
    )

    for dep_name, dep_spec in deps.items():
        conn.execute(
            "INSERT OR IGNORE INTO dependencies (package, version, dep_name, dep_version_spec, optional) "
            "VALUES (?, ?, ?, ?, 0)",
            (package_name, confirmed_version, dep_name, dep_spec)
        )

    # Index JS modules
    scan_root = pkg_subdir if pkg_subdir.exists() else pkg_vault_dir
    for js_file in scan_root.rglob("*.js"):
        rel = js_file.relative_to(pkg_vault_dir)
        module_name = str(rel).replace('/', '.').replace('.js', '')
        conn.execute(
            "INSERT OR REPLACE INTO modules (package, version, module_name, module_path, is_native, size_bytes) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (package_name, confirmed_version, module_name, str(rel), js_file.stat().st_size)
        )

    conn.commit()
    conn.close()

    # Cleanup download dir
    shutil.rmtree(dl_dir, ignore_errors=True)

    print(f"  ✓ Vaulted: npm:{package_name}@{confirmed_version} ({'native' if has_native else 'pure js'})")
    return True


def npm_vault_add_recursive(package_name, version=None, depth=0, seen=None):
    """Download an npm package and all its dependencies."""
    if seen is None:
        seen = set()
    
    key = f"{package_name}@{version}" if version else package_name
    if key in seen:
        return True
    seen.add(key)
    
    indent = "  " * depth
    print(f"{indent}● npm:{package_name}" + (f"@{version}" if version else ""))
    
    # Check if already vaulted
    conn = get_db()
    row = conn.execute("SELECT 1 FROM packages WHERE name=? AND source='npm'",
                       (package_name,)).fetchone()
    conn.close()
    
    if row:
        print(f"{indent}  (already vaulted)")
    else:
        if not npm_vault_add(package_name, version):
            return False
    
    # Recurse into dependencies
    conn = get_db()
    rows = conn.execute(
        "SELECT dep_name, dep_version_spec FROM dependencies WHERE package=? AND optional=0",
        (package_name,)).fetchall()
    conn.close()
    
    for dep_name, dep_spec in rows:
        npm_vault_add_recursive(dep_name, depth=depth+1, seen=seen)
    
    return True


# ─────────────────────────────────────────────
# JS/TS SCANNER — Layer 2 for Node.js
# ─────────────────────────────────────────────

def scan_js_imports(source):
    """
    Regex-based import scanner for JS/TS.
    Catches:
      - require('package')
      - require("package")
      - import ... from 'package'
      - import ... from "package"
      - import 'package'
      - dynamic import('package')
    """
    imports = set()
    
    # require('...')  and  require("...")
    for m in re.finditer(r'''require\s*\(\s*['"]([^'"]+)['"]\s*\)''', source):
        imports.add(m.group(1))
    
    # import ... from '...'  and  import ... from "..."
    for m in re.finditer(r'''import\s+.*?\s+from\s+['"]([^'"]+)['"]''', source, re.DOTALL):
        imports.add(m.group(1))
    
    # import '...'  (side-effect import)
    for m in re.finditer(r'''import\s+['"]([^'"]+)['"]''', source):
        imports.add(m.group(1))
    
    # dynamic import('...')
    for m in re.finditer(r'''import\s*\(\s*['"]([^'"]+)['"]\s*\)''', source):
        imports.add(m.group(1))
    
    return imports


def scan_js_script(script_path):
    """Scan a JS/TS file and return its dependency map."""
    script_path = Path(script_path)
    if not script_path.exists():
        print(f"  ✗ File not found: {script_path}")
        return None
    
    with open(script_path, 'r', encoding='utf-8', errors='replace') as f:
        source = f.read()
    
    raw_imports = scan_js_imports(source)
    
    # Categorize
    builtin = set()
    external = set()
    local = set()

    for imp in raw_imports:
        # Check for node:* builtins (e.g., node:fs, node:fs/promises)
        if imp.startswith('node:'):
            # Extract base module: node:fs/promises -> fs
            base = imp[5:].split('/')[0]
            if base in NODE_BUILTINS or f'node:{base}' in NODE_BUILTINS:
                builtin.add(imp)
                continue
        elif imp in NODE_BUILTINS:
            # Non-prefixed builtin (e.g., 'fs', 'path')
            builtin.add(imp)
            continue

        if imp.startswith('.') or imp.startswith('/'):
            local.add(imp)
        else:
            # Extract package name (handle scoped packages like @scope/pkg)
            if imp.startswith('@'):
                parts = imp.split('/')
                pkg_name = '/'.join(parts[:2]) if len(parts) >= 2 else imp
            else:
                pkg_name = imp.split('/')[0]

            # Skip if the extracted package name is a builtin
            if pkg_name in NODE_BUILTINS or pkg_name.lstrip('node:') in NODE_BUILTINS:
                builtin.add(imp)
            else:
                external.add(pkg_name)
    
    packages_needed = {}
    for pkg in external:
        packages_needed[pkg] = {
            'import_name': pkg,
            'submodules': [i for i in raw_imports if i.startswith(pkg)],
            'from_imports': {}
        }
    
    return {
        'script': str(script_path),
        'stdlib': sorted(builtin),
        'external': packages_needed,
        'local': sorted(local),
        'all_imports': sorted(raw_imports),
        'ecosystem': 'npm',
    }


def scan_and_resolve_js(script_path):
    """Scan a JS file and resolve against the vault."""
    scan = scan_js_script(script_path)
    if not scan:
        return None
    
    conn = get_db()
    resolved = {}
    missing = {}
    
    for pkg_name, info in scan['external'].items():
        row = conn.execute(
            "SELECT name, version, vault_path, has_native FROM packages "
            "WHERE name=? AND source='npm' ORDER BY cached_at DESC LIMIT 1",
            (pkg_name,)
        ).fetchone()
        
        if row:
            name, version, vault_path, has_native = row
            resolved[pkg_name] = {
                'version': version,
                'vault_path': vault_path,
                'has_native': bool(has_native),
                'modules': [],
                'total_size': 0,
            }
            # Calculate size
            vpath = Path(vault_path)
            if vpath.exists():
                total = sum(f.stat().st_size for f in vpath.rglob('*') if f.is_file())
                resolved[pkg_name]['total_size'] = total
        else:
            missing[pkg_name] = info
    
    conn.close()
    return {'scan': scan, 'resolved': resolved, 'missing': missing}


# ─────────────────────────────────────────────
# JS BUBBLE — Layer 3 for Node.js
# ─────────────────────────────────────────────

def bubble_up_js(script_path, keep=False, extra_args=None):
    """Create an ephemeral bubble for a JS/TS script with error-driven retry loop."""
    init_db()
    script_path = Path(script_path).resolve()

    print(f"\n  ◉ Bubble Up (Node): {script_path.name}")
    print(f"  │")

    # Scan and resolve
    result = scan_and_resolve_js(script_path)
    if not result:
        print(f"  ✗ Scan failed")
        return 1

    if result['missing']:
        print(f"  │ Missing packages — attempting auto-vault...")
        for pkg_name in result['missing']:
            npm_vault_add(pkg_name)
        result = scan_and_resolve_js(script_path)

    if result['missing']:
        print(f"  │ ⚠ Still missing (will try runtime resolution):")
        for pkg in result['missing']:
            print(f"  │   • {pkg}")

    # Create bubble
    bubble_id = hashlib.md5(
        f"{script_path}{datetime.now().isoformat()}".encode()
    ).hexdigest()[:12]

    bubble_dir = BUBBLES_DIR / bubble_id
    bubble_nm = bubble_dir / "node_modules"
    bubble_nm.mkdir(parents=True)

    print(f"  │ Bubble: {bubble_id}")

    # Path shims (proot/Termux compensation)
    shim_overrides, shimmed = setup_path_shims(bubble_dir, verbose=True)
    if shimmed:
        print(f"  │ Shimmed {len(shimmed)} paths")

    # Helper to add a package to the bubble's node_modules
    binaries_added = []

    def add_package_to_bubble(pkg_name, vault_path):
        """Add a vaulted package to the bubble's node_modules."""
        nonlocal binaries_added
        pkg_subdir = Path(vault_path) / 'package'
        if not pkg_subdir.exists():
            pkg_subdir = Path(vault_path)

        if not pkg_subdir.exists():
            return False

        # Handle scoped packages (@scope/name)
        if pkg_name.startswith('@'):
            parts = pkg_name.split('/', 1)
            scope = parts[0]
            name = parts[1] if len(parts) > 1 else ''
            dest_dir = bubble_nm / scope
            dest_dir.mkdir(exist_ok=True)
            dest = dest_dir / name if name else dest_dir
        else:
            dest = bubble_nm / pkg_name

        if dest.exists():
            return True

        try:
            os.symlink(pkg_subdir, dest)
        except OSError:
            shutil.copytree(pkg_subdir, dest)

        # Set up binaries from package.json
        pkg_json_path = pkg_subdir / 'package.json'
        if pkg_json_path.exists():
            try:
                with open(pkg_json_path, 'r') as f:
                    pkg_json = json.load(f)
                bin_field = pkg_json.get('bin', {})
                if isinstance(bin_field, str):
                    # Single binary: "bin": "./cli.js"
                    bin_field = {pkg_name: bin_field}
                for bin_name, bin_path in bin_field.items():
                    bin_src = pkg_subdir / bin_path
                    if bin_src.exists():
                        bin_dest = bubble_nm / '.bin' / bin_name
                        bin_dest.parent.mkdir(parents=True, exist_ok=True)
                        if not bin_dest.exists():
                            try:
                                os.symlink(bin_src, bin_dest)
                                binaries_added.append(bin_name)
                            except OSError:
                                shutil.copy2(bin_src, bin_dest)
                                binaries_added.append(bin_name)
            except (json.JSONDecodeError, IOError):
                pass

        return True

    # Assemble — symlink each resolved package into node_modules
    assembled = []
    for pkg_name, info in result.get('resolved', {}).items():
        vault_path = Path(info['vault_path'])
        if add_package_to_bubble(pkg_name, vault_path):
            assembled.append(pkg_name)

    if assembled:
        print(f"  │ Assembled: {', '.join(assembled)}")

    # Build environment
    env = os.environ.copy()
    env.update(shim_overrides)
    env['NODE_PATH'] = str(bubble_nm)
    env['BUBBLE_ID'] = bubble_id
    env['BUBBLE_DIR'] = str(bubble_dir)

    # Add .bin to PATH for npm package binaries
    bin_dir = bubble_nm / '.bin'
    if bin_dir.exists():
        existing_path = env.get('PATH', '')
        env['PATH'] = str(bin_dir) + (f':{existing_path}' if existing_path else '')

    # Record bubble
    conn = get_db()
    conn.execute(
        "INSERT INTO bubbles (bubble_id, created_at, script_path, status, bubble_path, packages) "
        "VALUES (?, ?, ?, 'active', ?, ?)",
        (bubble_id, datetime.now().isoformat(), str(script_path), str(bubble_dir),
         json.dumps(list(result.get('resolved', {}).keys())))
    )
    conn.commit()
    conn.close()

    # Run with error-driven retry loop
    print(f"  │")
    print(f"  ├─── Run ───────────────────")
    print(f"  │")

    # Reopen DB connection for the retry loop
    conn = get_db()

    # Detect runner: node for .js/.mjs, tsx for .ts
    if script_path.suffix in ('.ts', '.tsx'):
        runner = 'tsx'
        # Ensure tsx is available for TypeScript files
        runner_available = False
        for path_dir in env.get('PATH', '').split(':'):
            if path_dir and Path(path_dir).joinpath('tsx').exists():
                runner_available = True
                break
        if not runner_available:
            # Check if tsx is vaulted
            tsx_row = conn.execute(
                "SELECT name, version, vault_path FROM packages WHERE name='tsx' AND source='npm' "
                "ORDER BY cached_at DESC LIMIT 1"
            ).fetchone()
            if tsx_row:
                _, version, vault_path = tsx_row
                if add_package_to_bubble('tsx', vault_path):
                    bin_dir = bubble_nm / '.bin'
                    existing_path = env.get('PATH', '')
                    env['PATH'] = str(bin_dir) + (f':{existing_path}' if existing_path else '')
                    print(f"  │ Added tsx@{version} for TypeScript support")
            else:
                # Download tsx
                print(f"  │ Installing tsx for TypeScript...")
                if npm_vault_add('tsx'):
                    tsx_row = conn.execute(
                        "SELECT name, version, vault_path FROM packages WHERE name='tsx' AND source='npm' "
                        "ORDER BY cached_at DESC LIMIT 1"
                    ).fetchone()
                    if tsx_row:
                        _, version, vault_path = tsx_row
                        if add_package_to_bubble('tsx', vault_path):
                            bin_dir = bubble_nm / '.bin'
                            existing_path = env.get('PATH', '')
                            env['PATH'] = str(bin_dir) + (f':{existing_path}' if existing_path else '')
                            print(f"  │ Added tsx@{version} for TypeScript support")
    else:
        runner = 'node'

    cmd = [runner, str(script_path)]
    if extra_args:
        cmd.extend(extra_args)

    max_retries = 20
    retry_count = 0
    start = datetime.now()

    while retry_count < max_retries:
        proc = subprocess.run(cmd, env=env, cwd=str(script_path.parent), capture_output=True, text=True)

        if proc.returncode == 0:
            if proc.stdout:
                print(proc.stdout.rstrip())
            break

        # Check for missing module errors
        stderr = proc.stderr

        # Node.js error patterns:
        # - Error: Cannot find module 'xyz'
        # - Error: Cannot find package 'xyz'
        # - Error [ERR_MODULE_NOT_FOUND]: Cannot find module 'xyz'
        missing_pkg = None

        # Try different error patterns
        patterns = [
            r"Cannot find module ['\"]([^'\"]+)['\"]",
            r"Cannot find package ['\"]([^'\"]+)['\"]",
            r"Error\[ERR_MODULE_NOT_FOUND\].*?['\"]([^'\"]+)['\"]",
        ]

        for pattern in patterns:
            match = re.search(pattern, stderr)
            if match:
                missing_pkg = match.group(1)
                break

        if missing_pkg:
            # Extract package name from import path
            # e.g., 'lodash/fp' -> 'lodash', '@scope/pkg/sub' -> '@scope/pkg'
            if missing_pkg.startswith('@'):
                parts = missing_pkg.split('/')
                pkg_name = '/'.join(parts[:2]) if len(parts) >= 2 else missing_pkg
            else:
                pkg_name = missing_pkg.split('/')[0]

            # Skip Node.js builtins
            if pkg_name in NODE_BUILTINS or pkg_name.lstrip('node:') in NODE_BUILTINS:
                print(f"  ✗ Cannot resolve builtin module: {missing_pkg}")
                print(proc.stderr.rstrip())
                break

            print(f"  │ ⚠ Missing module: {missing_pkg} — resolving...")

            # Check if it's in the vault
            row = conn.execute(
                "SELECT name, version, vault_path FROM packages WHERE name=? AND source='npm' "
                "ORDER BY cached_at DESC LIMIT 1",
                (pkg_name,)
            ).fetchone()

            if row:
                pkg_name_resolved, version, vault_path = row
                print(f"  │   Found in vault: {pkg_name_resolved}@{version}")
                if add_package_to_bubble(pkg_name_resolved, vault_path):
                    # Update PATH if binaries were added
                    if binaries_added:
                        bin_dir = bubble_nm / '.bin'
                        existing_path = env.get('PATH', '')
                        env['PATH'] = str(bin_dir) + (f':{existing_path}' if existing_path else '')
                        binaries_added.clear()
                    print(f"  │   Added to bubble, retrying...")
                    retry_count += 1
                    continue
            else:
                # Not in vault — download it
                print(f"  │   Not in vault, downloading {pkg_name}...")
                if npm_vault_add(pkg_name):
                    # Get newly vaulted package
                    row = conn.execute(
                        "SELECT name, version, vault_path FROM packages WHERE name=? AND source='npm' "
                        "ORDER BY cached_at DESC LIMIT 1",
                        (pkg_name,)
                    ).fetchone()
                    if row:
                        pkg_name_resolved, version, vault_path = row
                        if add_package_to_bubble(pkg_name_resolved, vault_path):
                            # Update PATH if binaries were added
                            if binaries_added:
                                bin_dir = bubble_nm / '.bin'
                                existing_path = env.get('PATH', '')
                                env['PATH'] = str(bin_dir) + (f':{existing_path}' if existing_path else '')
                                binaries_added.clear()
                            print(f"  │   Added {pkg_name_resolved}@{version}, retrying...")
                            retry_count += 1
                            continue
                else:
                    print(f"  │   Failed to download {pkg_name}")
                    print(proc.stderr.rstrip())
                    break

        # Not a module-not-found error — print stderr and exit
        if proc.stderr:
            print(proc.stderr.rstrip())
        break

    if retry_count > 0:
        print(f"  │ Retries: {retry_count}")

    elapsed = (datetime.now() - start).total_seconds()
    print(f"  │")
    print(f"  ├─── End ({elapsed:.2f}s) ──────────")

    # Dissolve
    if keep:
        print(f"  │ Kept: {bubble_dir}")
    else:
        shutil.rmtree(bubble_dir, ignore_errors=True)
        conn = get_db()
        conn.execute("UPDATE bubbles SET status='dissolved' WHERE bubble_id=?", (bubble_id,))
        conn.commit()
        conn.close()
        print(f"  │ Dissolved")

    returncode = proc.returncode
    status = "✓" if returncode == 0 else f"✗ (exit {returncode})"
    print(f"  └─ {status}")
    return returncode



def main():
    parser = argparse.ArgumentParser(
        prog='bubble',
        description='Ephemeral dependency isolation for JANUS',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          bubble vault add numpy
          bubble vault add requests --version 2.31.0
          bubble vault add numpy --recursive
          bubble vault list
          bubble scan my_script.py
          bubble up my_script.py
          bubble up my_script.py --keep
          bubble down --all
          bubble doctor
        """)
    )
    
    sub = parser.add_subparsers(dest='command')
    
    # vault
    vault_p = sub.add_parser('vault', help='Manage the package vault')
    vault_sub = vault_p.add_subparsers(dest='vault_command')
    
    vault_add_p = vault_sub.add_parser('add', help='Add package to vault')
    vault_add_p.add_argument('package', help='Package name')
    vault_add_p.add_argument('--version', '-v', help='Specific version')
    vault_add_p.add_argument('--recursive', '-r', action='store_true',
                             help='Also vault all dependencies')
    vault_add_p.add_argument('--npm', action='store_true',
                             help='Treat as npm package (auto-detected if name starts with npm:)')
    
    vault_sub.add_parser('list', help='List vaulted packages')
    vault_sub.add_parser('index', help='Rebuild module index')
    
    # scan
    scan_p = sub.add_parser('scan', help='Scan a script for dependencies')
    scan_p.add_argument('script', help='Path to script (.py, .js, .ts)')
    scan_p.add_argument('--resolve', '-r', action='store_true',
                        help='Also resolve against vault')
    
    # up
    up_p = sub.add_parser('up', help='Spin up a bubble and run')
    up_p.add_argument('script', help='Path to Python script')
    up_p.add_argument('--keep', action='store_true', help='Keep bubble after run')
    up_p.add_argument('args', nargs='*', help='Arguments to pass to script')
    
    # down
    down_p = sub.add_parser('down', help='Dissolve bubbles')
    down_p.add_argument('--all', action='store_true', help='Dissolve all active bubbles')
    
    # doctor
    sub.add_parser('doctor', help='Diagnose environment')
    
    # preflight
    pre_p = sub.add_parser('preflight', help='Dry-run: check what a script needs before going offline')
    pre_p.add_argument('script', help='Path to script (.py, .js, .ts)')
    
    args = parser.parse_args()
    
    if args.command == 'vault':
        if args.vault_command == 'add':
            # Detect ecosystem: --npm flag, or 'npm:' prefix on package name
            pkg = args.package
            is_npm = args.npm or pkg.startswith('npm:')
            if pkg.startswith('npm:'):
                pkg = pkg[4:]
            
            if is_npm:
                if args.recursive:
                    npm_vault_add_recursive(pkg, args.version)
                else:
                    npm_vault_add(pkg, args.version)
            else:
                if args.recursive:
                    vault_add_recursive(pkg, args.version)
                else:
                    vault_add(pkg, args.version)
        elif args.vault_command == 'list':
            vault_list()
        elif args.vault_command == 'index':
            vault_index()
        else:
            vault_p.print_help()
    
    elif args.command == 'scan':
        target = Path(args.script)
        if target.is_dir():
            if args.resolve:
                result = scan_and_resolve_package(args.script)
                if result:
                    print_package_report(result)
            else:
                result = scan_package(args.script)
                if result:
                    print_package_report(result)
        else:
            eco = _detect_ecosystem(args.script)
            if eco == 'npm':
                if args.resolve:
                    result = scan_and_resolve_js(args.script)
                else:
                    result = scan_js_script(args.script)
            else:
                if args.resolve:
                    result = scan_and_resolve(args.script)
                else:
                    result = scan_script(args.script)
            if result:
                print_scan_report(result)
    
    elif args.command == 'up':
        sys.exit(bubble_up(args.script, keep=args.keep, extra_args=args.args))
    
    elif args.command == 'down':
        bubble_down(all_bubbles=args.all)
    
    elif args.command == 'doctor':
        doctor()
    
    elif args.command == 'preflight':
        preflight(args.script)
    
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

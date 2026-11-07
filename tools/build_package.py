# -*- coding: utf-8 -*-
"""Build music_synth.neko-plugin (N.E.K.O official plugin package format).

Usage:  python tools/build_package.py
Output: dist/music_synth.neko-plugin

Format:
  manifest.toml, metadata.toml at archive root;
  payload/dependencies.toml, payload/profiles/default.toml,
  payload/plugins/<id>/...

Payload hash:
  for each payload entry sorted by NFC(full name):
    sha256.update(NFC(relpath).encode('utf-8')); update(b'\\0')
    update(raw_bytes); update(b'\\0')
"""
import hashlib
import json
import os
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (build host may be older than the plugin runtime)
    import tomli as tomllib
import unicodedata
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO
OUT = REPO / 'dist' / 'music_synth.neko-plugin'
PID = 'music_synth'

EXCLUDE_DIR_NAMES = {
    '__pycache__', '.pytest_cache', '.venv', 'node_modules', '.git',
    '_trash', 'claude-backups', 'NEKO_PLUGIN_BACKUPS', 'dist', 'tools',
}


def norm(rel):
    return str(rel).replace('\\', '/')


def skip_dir(rel_dir):
    rel_dir = norm(rel_dir)
    parts = [p for p in rel_dir.split('/') if p and p != '.']
    if not parts:
        return False
    if rel_dir == 'static/_preview':
        return True
    return any(p in EXCLUDE_DIR_NAMES or p.startswith('_backup') or p.startswith('_bak') for p in parts)


def keep(rel):
    r = norm(rel)
    if skip_dir('/'.join(r.split('/')[:-1])):
        return False
    base = r.split('/')[-1]
    if base in {'plugin.meta.json', '.DS_Store', 'Thumbs.db'} or base.endswith('.lock'):
        return False
    if base.endswith(('.pyc', '.pyo', '~')):
        return False
    # 市场拒绝包内二进制文件；原生组件由安装流程 pip 装进合成环境。
    if base.endswith(('.pyd', '.exe', '.dll', '.so', '.dylib', '.gz')):
        return False
    if '.bak' in base or '_bak' in base or base.startswith('_dbg'):
        return False
    return True


def toml_val(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return '[' + ', '.join(toml_val(x) for x in v) + ']'
    raise SystemExit('unsupported config value type: %r' % (v,))


def payload_hash(entries):
    h = hashlib.sha256()
    for k in sorted(entries, key=lambda s: unicodedata.normalize('NFC', s)):
        h.update(unicodedata.normalize('NFC', k).encode('utf-8'))
        h.update(b'\0')
        h.update(entries[k])
        h.update(b'\0')
    return h.hexdigest()


def main():
    files = {}
    for root, dirs, names in os.walk(SRC):
        rel_root = norm(os.path.relpath(root, SRC))
        dirs[:] = [d for d in dirs if not skip_dir(os.path.join(rel_root, d))]
        for nm in names:
            p = os.path.join(root, nm)
            rel = norm(os.path.relpath(p, SRC))
            if keep(rel):
                with open(p, 'rb') as f:
                    files[rel] = f.read()

    with open(SRC / 'plugin.toml', 'rb') as fh:
        doc = tomllib.load(fh)
    pinfo = doc['plugin']
    assert pinfo['id'] == PID, pinfo['id']
    cfg = doc.get(PID, {})

    manifest = (
        'schema_version = "1.0"\n'
        'package_type = "plugin"\n\n'
        'id = "%s"\n'
        'package_name = %s\n'
        'version = %s\n'
        'package_description = %s\n'
    ) % (PID, json.dumps(pinfo['name'], ensure_ascii=False),
         json.dumps(pinfo['version'], ensure_ascii=False),
         json.dumps(pinfo.get('description') or '', ensure_ascii=False))

    deps = (
        'schema_version = "1.0"\n\n'
        '[plugins.%s]\n'
        'python_requirements = []\n'
        'host_python_requirements = []\n'
        'plugin_dependencies = []\n'
        'advanced_plugin_dependencies = []\n'
        'vendor_path = "plugins/%s/vendor"\n'
        'vendor_present = false\n'
    ) % (PID, PID)

    plines = ['name = "default"', 'enabled_plugins = ["%s"]' % PID, '',
              '[plugin.%s]' % PID, 'enabled = true', 'auto_start = true']
    for k, v in cfg.items():
        if k not in ('enabled', 'auto_start'):
            plines.append('%s = %s' % (k, toml_val(v)))
    profiles = '\n'.join(plines) + '\n'

    entries = {
        'dependencies.toml': deps.encode('utf-8'),
        'profiles/default.toml': profiles.encode('utf-8'),
    }
    for rel, data in files.items():
        entries['plugins/%s/%s' % (PID, rel)] = data

    ph = payload_hash(entries)
    metadata = (
        '[payload]\nhash_algorithm = "sha256"\nhash = "%s"\n\n'
        '[source]\nkind = "local"\npaths = ["%s"]\n'
    ) % (ph, PID)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(OUT) + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr('manifest.toml', manifest)
        z.writestr('metadata.toml', metadata)
        for k in sorted(entries, key=lambda s: unicodedata.normalize('NFC', s)):
            z.writestr('payload/' + k, entries[k])
    os.replace(tmp, OUT)

    # verify
    z = zipfile.ZipFile(OUT)
    assert z.testzip() is None
    got = payload_hash({n[len('payload/'):]: z.read(n) for n in z.namelist()
                        if n.startswith('payload/') and not n.endswith('/')})
    assert got == ph
    print('written:', OUT)
    print('files:', len(entries), 'size:', os.path.getsize(OUT), 'bytes')
    print('payload hash:', ph)
    return 0


if __name__ == '__main__':
    sys.exit(main())

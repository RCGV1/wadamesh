#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the canonical WADAMESH app with its pinned native Meshtastic firmware."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.meshtastic'
DEPENDENCY = ROOT / 'meshtastic/firmware'
LOCK = json.loads((ROOT / 'meshtastic/lock.json').read_text())
REVISION = LOCK['firmware']['revision']
WORKSPACE = CACHE / ('firmware-' + REVISION[:12])


def run(args, cwd=ROOT, env=None):
    subprocess.run([str(x) for x in args], cwd=cwd, env=env, check=True)


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


@contextlib.contextmanager
def locked():
    if CACHE.is_symlink():
        raise RuntimeError('The generated .meshtastic directory must not be a symlink')
    CACHE.mkdir(exist_ok=True)
    with (CACHE / 'build.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def ensure_dependency():
    if not (DEPENDENCY / '.git').exists():
        run(['git', 'submodule', 'update', '--init', '--', 'meshtastic/firmware'])
    if git(DEPENDENCY, 'rev-parse', 'HEAD') != REVISION:
        raise RuntimeError('Firmware differs from meshtastic/lock.json; run git submodule update --init')
    if git(DEPENDENCY, 'status', '--porcelain', '--untracked-files=all'):
        raise RuntimeError('The pinned firmware dependency has local changes; preserve them before building')
    schema = DEPENDENCY / 'protobufs'
    if not (schema / '.git').exists():
        run(['git', '-C', DEPENDENCY, 'submodule', 'update', '--init', '--depth', '1', '--', 'protobufs'])
    if git(schema, 'rev-parse', 'HEAD') != LOCK['firmware']['protobuf_revision']:
        raise RuntimeError('Firmware protobuf checkout does not match the lock')
    if git(schema, 'status', '--porcelain', '--untracked-files=all'):
        raise RuntimeError('The protobuf dependency has local changes')


def source_digest():
    digest = hashlib.sha256()
    for base in ['src', 'include', 'lib', 'meshtastic/app', 'meshtastic/tests']:
        for path in sorted((ROOT / base).rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(b'\0')
            digest.update(path.read_bytes())
    digest.update((ROOT / 'meshtastic/lock.json').read_bytes())
    return digest.hexdigest()


def replace_with_link(path, target):
    if not path.parent.resolve().is_relative_to(WORKSPACE.resolve()):
        raise RuntimeError('Refusing a link outside the generated workspace')
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    path.symlink_to(os.path.relpath(target, path.parent), target_is_directory=True)


def ensure_schema_workspace():
    path = WORKSPACE / 'protobufs'
    if path.is_symlink():
        path.unlink()
    if path.exists() and not any(path.iterdir()):
        path.rmdir()
    if not path.exists():
        run(['git', '-C', DEPENDENCY / 'protobufs', 'worktree', 'add', '--detach', path, LOCK['firmware']['protobuf_revision']])
    if git(path, 'rev-parse', 'HEAD') != LOCK['firmware']['protobuf_revision'] or git(path, 'status', '--porcelain', '--untracked-files=all'):
        raise RuntimeError('Generated protobuf workspace is not the clean pinned revision')


def check_workspace():
    allowed = ('lib/WadaApp', 'test/wadamesh_host')
    entries = subprocess.check_output(['git', '-C', str(WORKSPACE), 'status', '--porcelain', '-z', '--ignore-submodules=all', '--untracked-files=all'], text=True).split('\0')
    for entry in entries:
        if not entry:
            continue
        name = entry[3:] if len(entry) >= 3 and entry[2] == ' ' else entry
        if name == '.wada-workspace.json' or any(name == prefix or name.startswith(prefix + '/') for prefix in allowed):
            continue
        raise RuntimeError('Generated firmware was modified outside the app overlay: ' + name)
    tracked = set(git(WORKSPACE, 'ls-tree', '-r', '--name-only', REVISION).splitlines())
    for base in ['src', 'lib', 'variants', 'bin', 'extra_scripts']:
        for path in (WORKSPACE / base).rglob('*'):
            if not path.is_file() or path.suffix not in ['.c', '.cpp', '.h', '.hpp', '.py', '.ini', '.csv']:
                continue
            name = path.relative_to(WORKSPACE).as_posix()
            if any(name == prefix or name.startswith(prefix + '/') for prefix in allowed):
                continue
            if name not in tracked:
                raise RuntimeError('Untracked generated firmware input: ' + name)


def prepare():
    ensure_dependency()
    # A missing canonical input must never fall back to an old vendored copy.
    for path in (DEPENDENCY / 'lib/WadaApp/vendor').rglob('*'):
        if path.is_file() and not (ROOT / 'src' / path.relative_to(DEPENDENCY / 'lib/WadaApp/vendor')).is_file():
            raise RuntimeError('Missing canonical UI input: ' + str(path.relative_to(DEPENDENCY / 'lib/WadaApp/vendor')))
    if not WORKSPACE.exists():
        run(['git', '-C', DEPENDENCY, 'worktree', 'add', '--detach', WORKSPACE, REVISION])
    if WORKSPACE.is_symlink() or Path(git(WORKSPACE, 'rev-parse', '--show-toplevel')).resolve() != WORKSPACE.resolve():
        raise RuntimeError('Generated workspace has an unexpected Git root')
    if git(WORKSPACE, 'rev-parse', 'HEAD') != REVISION:
        raise RuntimeError('Generated workspace has an unexpected revision')
    marker = WORKSPACE / '.wada-workspace.json'
    if marker.exists():
        if json.loads(marker.read_text()).get('firmware_revision') != REVISION:
            raise RuntimeError('Generated workspace ownership marker is invalid')
    elif git(WORKSPACE, 'status', '--porcelain', '--ignore-submodules=all'):
        raise RuntimeError('Refusing to replace files in an unmarked, modified workspace')
    marker.write_text(json.dumps({'format': 1, 'firmware_revision': REVISION}, indent=2) + '\n')
    ensure_schema_workspace()
    check_workspace()
    replace_with_link(WORKSPACE / 'lib/WadaApp', ROOT / 'meshtastic/app')
    replace_with_link(WORKSPACE / 'test/wadamesh_host', ROOT / 'meshtastic/tests')
    (WORKSPACE / '.pio').mkdir(exist_ok=True)
    (CACHE / 'libdeps').mkdir(exist_ok=True)
    replace_with_link(WORKSPACE / '.pio/libdeps', CACHE / 'libdeps')
    for path in [WORKSPACE / 'lib/WadaApp/vendor', WORKSPACE / 'lib/WadaApp/port']:
        expected = ROOT / ('src' if path.name == 'vendor' else 'meshtastic/app/port')
        if path.resolve() != expected.resolve():
            raise RuntimeError('Canonical application link did not resolve correctly')
    record = {'firmware_revision': REVISION, 'protobuf_revision': LOCK['firmware']['protobuf_revision'],
              'wadamesh_revision': git(ROOT, 'rev-parse', 'HEAD'), 'source_sha256': source_digest()}
    (CACHE / 'sources.json').write_text(json.dumps(record, indent=2) + '\n')
    print('Prepared canonical WADAMESH app with firmware ' + REVISION[:12], flush=True)


def pio_python(explicit=None):
    choices = [Path(explicit)] if explicit else [Path.home() / '.platformio/penv/bin/python', Path(sys.executable)]
    for path in choices:
        if not path.is_file():
            continue
        result = subprocess.run([str(path), '-c', 'import platformio; print(platformio.__version__)'], capture_output=True, text=True)
        match = re.match(r'(\d+)\.(\d+)', result.stdout.strip())
        if result.returncode == 0 and match and tuple(map(int, match.groups())) >= (6, 2):
            return path
    raise RuntimeError('Install PlatformIO Core >=6.2 or supply --pio-python /path/to/python')


def native_build(options):
    python = pio_python(options.pio_python)
    env = dict(os.environ)
    env['PYTHONPATH'] = str(WORKSPACE / 'bin/pio_compat') + os.pathsep + env.get('PYTHONPATH', '')
    run([python, '-m', 'platformio', 'run', '-e', 't-deck-tft', '-j', str(options.jobs)], cwd=WORKSPACE, env=env)
    record = json.loads((CACHE / 'sources.json').read_text())
    record['artifacts'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted((WORKSPACE / '.pio/build/t-deck-tft').glob('*.bin'))}
    if not record['artifacts']:
        raise RuntimeError('PlatformIO produced no firmware images')
    record['platformio_version'] = subprocess.check_output([str(python), '-m', 'platformio', '--version'], text=True, env=env).strip()
    (CACHE / 'build-info.json').write_text(json.dumps(record, indent=2) + '\n')
    print('Firmware artifacts: ' + str(WORKSPACE / '.pio/build/t-deck-tft'))


def sim_build(options):
    if not (ROOT / 'web/third_party/lvgl/lvgl.h').is_file():
        run(['bash', ROOT / 'web/fetch-deps.sh'])
    if not (WORKSPACE / '.pio/libdeps/t-deck-tft/Nanopb/pb.h').is_file():
        raise RuntimeError('Build native firmware once to install its pinned Nanopb dependency')
    run(['cmake', '-S', ROOT / 'desktop', '-B', ROOT / 'desktop/build'])
    run(['cmake', '--build', ROOT / 'desktop/build', '-j', str(options.jobs)])


def test_host():
    env = dict(os.environ)
    env['UBSAN_OPTIONS'] = 'halt_on_error=1'
    for script in ['test-wadamesh-host.py', 'test-wadamesh-ui.py']:
        run([sys.executable, WORKSPACE / 'bin' / script], cwd=WORKSPACE, env=env)


def test_sim(options):
    sim_build(options)
    for name in ['run_fixture_protocol.py', 'run_keyboard_input.py']:
        run([sys.executable, ROOT / 'desktop/tests' / name])
    folder = CACHE / ('sim-check-' + str(time.time_ns()))
    folder.mkdir()
    script = folder / 'check.wada'
    script.write_text('wait 3600\nexpect-state "node_count" "4"\nclick-text "Settings"\nwait 300\nclick-text "LoRa"\nwait 300\nexpect-value "Region" "US"\nstate "state.json"\nquit\n')
    env = dict(os.environ, WADA_NET='0', WADA_DATA_DIR=str(folder / 'data'))
    run([ROOT / 'desktop/build/wadamesh-meshtastic-sim', '--headless', '--script', script, '--shots', folder], env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'build', 'sim-build', 'test', 'sim-test'])
    parser.add_argument('--pio-python')
    parser.add_argument('-j', '--jobs', type=int, default=2)
    options = parser.parse_args()
    if options.jobs < 1:
        parser.error('jobs must be positive')
    with locked():
        prepare()
        if options.command == 'build': native_build(options)
        elif options.command == 'sim-build': sim_build(options)
        elif options.command == 'test': test_host()
        elif options.command == 'sim-test': test_sim(options)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print('Meshtastic build: ' + str(error), file=sys.stderr)
        sys.exit(1)

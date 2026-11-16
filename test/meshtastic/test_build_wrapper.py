#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise build provenance using disposable Git repositories, without downloads.

Run with: python3 -B -m unittest discover -s test/meshtastic -v
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]


def load_wrapper():
    spec = importlib.util.spec_from_file_location('meshtastic_build_wrapper', REPOSITORY / 'scripts/meshtastic.py')
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, 'dont_write_bytecode', True):
        spec.loader.exec_module(module)
    return module


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.PIPE).strip()


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def initialize(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, 'init', '-q')
    git(path, 'config', 'user.name', 'Build Wrapper Test')
    git(path, 'config', 'user.email', 'wrapper-test@example.invalid')
    git(path, 'config', 'commit.gpgsign', 'false')


def commit(path):
    git(path, 'add', '--all')
    git(path, 'commit', '-qm', 'Test fixture')
    return git(path, 'rev-parse', 'HEAD')


class BuildWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wada wrapper tests ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'canonical app with spaces'
        self.dependency = self.root / 'meshtastic/firmware'
        schema = Path(self.temporary.name) / 'schema origin'
        initialize(schema)
        write(schema / 'meshtastic/test.proto', 'syntax = "proto3";\n')
        protobuf_revision = commit(schema)

        initialize(self.dependency)
        write(self.dependency / '.gitignore', '.pio/\n')
        write(self.dependency / 'src/main.cpp', 'void setup() {}\n')
        write(self.dependency / 'lib/WadaApp/vendor/ui-touch/UI.cpp', 'old vendored UI\n')
        write(self.dependency / 'test/wadamesh_host/old.cpp', 'old vendored test\n')
        write(self.dependency / 'bin/pio_compat/platformio.py', '''import json
from pathlib import Path
import subprocess
import sys
if sys.argv[1:] == ['--version']:
    print('PlatformIO Core, version 6.2.0-test')
    sys.exit(0)
def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()
Path('native-context.json').write_text(json.dumps({
    'root': git('rev-parse', '--show-toplevel'),
    'revision': git('rev-parse', 'HEAD'),
    'schema_root': git('-C', 'protobufs', 'rev-parse', '--show-toplevel'),
    'schema_revision': git('-C', 'protobufs', 'rev-parse', 'HEAD'),
    'schema_status': git('-C', 'protobufs', 'status', '--porcelain'),
    'args': sys.argv[1:]}))
output = Path('.pio/build/t-deck-tft/firmware.bin')
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b'fixture firmware')
''')
        git(self.dependency, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(schema), 'protobufs')
        revision = commit(self.dependency)

        initialize(self.root)
        write(self.root / '.gitignore', '.meshtastic/\nmeshtastic/firmware/\n')
        write(self.root / 'src/ui-touch/UI.cpp', 'canonical UI\n')
        write(self.root / 'include/WadaApp.h', '// canonical header\n')
        write(self.root / 'lib/helper.cpp', '// canonical helper\n')
        write(self.root / 'meshtastic/app/port/Port.cpp', '// canonical port\n')
        (self.root / 'meshtastic/app/vendor').symlink_to('../../src', target_is_directory=True)
        write(self.root / 'meshtastic/tests/test_app.cpp', '// canonical test\n')
        lock = {'firmware': {'revision': revision, 'protobuf_revision': protobuf_revision}}
        write(self.root / 'meshtastic/lock.json', json.dumps(lock))
        commit(self.root)

        self.wrapper = load_wrapper()
        self.wrapper.ROOT = self.root
        self.wrapper.CACHE = self.root / '.meshtastic'
        self.wrapper.DEPENDENCY = self.dependency
        self.wrapper.LOCK = lock
        self.wrapper.REVISION = revision
        self.wrapper.WORKSPACE = self.wrapper.CACHE / ('firmware-' + revision[:12])

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()), self.wrapper.locked():
            self.wrapper.prepare()

    def test_clean_pinned_dependencies_are_accepted(self):
        self.wrapper.ensure_dependency()

    def test_wrong_firmware_revision_is_rejected(self):
        self.wrapper.REVISION = '0' * 40
        with self.assertRaisesRegex(RuntimeError, 'differs from meshtastic/lock.json'):
            self.wrapper.ensure_dependency()

    def test_untracked_dependency_compiled_source_is_rejected(self):
        write(self.dependency / 'src/untracked.cpp', 'unexpected compiled source\n')
        with self.assertRaisesRegex(RuntimeError, 'dependency has local changes'):
            self.wrapper.ensure_dependency()

    def test_wrong_protobuf_revision_is_rejected(self):
        self.wrapper.LOCK['firmware']['protobuf_revision'] = '0' * 40
        with self.assertRaisesRegex(RuntimeError, 'protobuf checkout does not match'):
            self.wrapper.ensure_dependency()

    def test_missing_canonical_ui_never_uses_vendored_fallback(self):
        (self.root / 'src/ui-touch/UI.cpp').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Missing canonical UI input: ui-touch/UI.cpp'):
            self.prepare()
        self.assertFalse(self.wrapper.WORKSPACE.exists())

    def test_marked_workspace_still_rejects_modified_firmware(self):
        self.prepare()
        write(self.wrapper.WORKSPACE / 'src/main.cpp', 'unexpected firmware edit\n')
        with self.assertRaisesRegex(RuntimeError, 'modified outside the app overlay: src/main.cpp'):
            self.prepare()
        self.assertEqual((self.wrapper.WORKSPACE / 'src/main.cpp').read_text(), 'unexpected firmware edit\n')

    def test_untracked_generated_compiled_source_is_rejected(self):
        self.prepare()
        write(self.wrapper.WORKSPACE / 'src/extra.cpp', 'unexpected compiled source\n')
        with self.assertRaisesRegex(RuntimeError, '(modified outside the app overlay|Untracked generated firmware input)'):
            self.prepare()

    def test_app_and_tests_link_to_canonical_sources_and_prepare_is_repeatable(self):
        self.prepare()
        workspace = self.wrapper.WORKSPACE
        self.assertEqual((workspace / 'lib/WadaApp').resolve(), self.root / 'meshtastic/app')
        self.assertEqual((workspace / 'lib/WadaApp/vendor').resolve(), self.root / 'src')
        self.assertEqual((workspace / 'test/wadamesh_host').resolve(), self.root / 'meshtastic/tests')
        before = json.loads((self.wrapper.CACHE / 'sources.json').read_text())
        write(self.root / 'src/ui-touch/UI.cpp', 'updated canonical UI\n')
        write(self.root / 'meshtastic/tests/test_app.cpp', 'updated canonical test\n')
        self.prepare()
        self.assertEqual((workspace / 'lib/WadaApp/vendor/ui-touch/UI.cpp').read_text(), 'updated canonical UI\n')
        self.assertEqual((workspace / 'test/wadamesh_host/test_app.cpp').read_text(), 'updated canonical test\n')
        after = json.loads((self.wrapper.CACHE / 'sources.json').read_text())
        self.assertNotEqual(before['source_sha256'], after['source_sha256'])
        self.assertEqual(after['wadamesh_revision'], git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual(after['firmware_revision'], self.wrapper.REVISION)

    def test_native_dispatch_has_real_clean_git_worktrees_with_spaces(self):
        self.prepare()
        workspace = self.wrapper.WORKSPACE
        self.assertIn(' ', str(workspace))
        self.assertFalse((workspace / 'protobufs').is_symlink())
        self.assertTrue((workspace / 'protobufs/.git').is_file())
        options = types.SimpleNamespace(pio_python=None, jobs=3)
        with mock.patch.object(self.wrapper, 'pio_python', return_value=Path(sys.executable)), contextlib.redirect_stdout(io.StringIO()):
            self.wrapper.native_build(options)
        context = json.loads((workspace / 'native-context.json').read_text())
        self.assertEqual(Path(context['root']).resolve(), workspace.resolve())
        self.assertEqual(context['revision'], self.wrapper.REVISION)
        self.assertEqual(Path(context['schema_root']).resolve(), (workspace / 'protobufs').resolve())
        self.assertEqual(context['schema_revision'], self.wrapper.LOCK['firmware']['protobuf_revision'])
        self.assertEqual(context['schema_status'], '')
        self.assertEqual(context['args'], ['run', '-e', 't-deck-tft', '-j', '3'])
        record = json.loads((self.wrapper.CACHE / 'build-info.json').read_text())
        self.assertEqual(set(record['artifacts']), {'firmware.bin'})
        self.assertEqual(record['source_sha256'], self.wrapper.source_digest())


if __name__ == '__main__':
    unittest.main()

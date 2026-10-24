"""Native PTY authority stays bound to the kernel-issued PTY extension."""
import stat
import sys
import types
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
import support as sp
from test_apple_verification import Fixture
apple=sp.lw.apple

class PtyRules(unittest.TestCase):
    def test_master_is_one_literal_and_slave_needs_anchored_shape_and_extension(self):
        rules=apple.pty_rules()
        self.assertIn('(allow pseudo-tty)',rules)
        self.assertIn('(allow file-write-data (literal "/dev/ptmx"))',rules)
        self.assertIn('(require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty"))',rules)
        self.assertNotIn('file-write*',rules)
        self.assertNotIn('subpath',rules)
        self.assertNotIn('network',rules)
        self.assertNotIn('/dev/tty"',rules)
    def test_missing_noncharacter_or_symlinked_master_fails_closed(self):
        for info in (types.SimpleNamespace(st_mode=stat.S_IFDIR),types.SimpleNamespace(st_mode=stat.S_IFREG),types.SimpleNamespace(st_mode=stat.S_IFLNK)):
            with self.subTest(mode=info.st_mode),mock.patch.object(apple.os,'lstat',return_value=info):
                with self.assertRaises(apple.ApplePolicyError):apple.pty_rules()
        with mock.patch.object(apple.os,'lstat',side_effect=OSError('missing')):
            with self.assertRaises(apple.ApplePolicyError):apple.pty_rules()

class PtyProfile(Fixture):
    def test_full_profile_contains_extension_scoped_rule_after_denies(self):
        with mock.patch.object(apple,'testmanagerd_socket',return_value=None):profile=self.profile()
        self.assertIn(apple.pty_rules(),profile)
        self.assertIn('(deny network* network-bind network-inbound network-outbound)',profile)
        self.assertNotIn('(allow network',profile)
        self.assertNotIn('(subpath "/dev")',profile)
        self.assertNotIn('(allow file-write* (regex',profile)
        self.assertLess(profile.index('(deny file-write*'),profile.index('(allow pseudo-tty)'))

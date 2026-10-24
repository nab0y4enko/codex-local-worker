import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
import support as sp
apple=sp.lw.apple

class TestmanagerdSocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir="/private/tmp");self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.parent=self.root/'com.apple.launchd.TEST123';self.parent.mkdir(mode=0o700)
        self.path=self.parent/'com.apple.testmanagerd.unix-domain.socket'
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.bind(str(self.path));self.addCleanup(self.sock.close)
    def resolve(self, candidates=None):
        return apple.testmanagerd_socket(candidates=candidates if candidates is not None else [self.path],root=self.root)
    def test_only_private_current_user_real_socket_is_accepted(self):
        self.assertEqual(self.resolve(),self.path)
    def test_no_socket_grants_no_exception(self):
        self.assertIsNone(self.resolve([]))
    def test_ambiguous_socket_fails_closed(self):
        with self.assertRaises(apple.ApplePolicyError):self.resolve([self.path,self.path])
    def test_regular_file_symlink_and_wrong_leaf_are_refused(self):
        other=self.parent/'other';other.write_text('not a socket')
        link=self.root/'link';link.symlink_to(self.path)
        for p in (other,link):
            with self.subTest(path=p),self.assertRaises(apple.ApplePolicyError):self.resolve([p])
        self.sock.close();self.path.unlink();self.path.write_text('not socket')
        with self.assertRaises(apple.ApplePolicyError):self.resolve()
    def test_broad_or_foreign_directory_is_refused(self):
        self.parent.chmod(0o755)
        with self.assertRaises(apple.ApplePolicyError):self.resolve()
        self.parent.chmod(0o700)
        with mock.patch.object(apple.os,'getuid',return_value=os.getuid()+1):
            with self.assertRaises(apple.ApplePolicyError):self.resolve()
    def test_socket_under_unexpected_directory_is_refused(self):
        wrong=self.root/'other';wrong.mkdir(mode=0o700)
        target=wrong/self.path.name;s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.addCleanup(s.close);s.bind(str(target))
        with self.assertRaises(apple.ApplePolicyError):self.resolve([target])
    def test_symlinked_parent_is_refused(self):
        link=self.root/'com.apple.launchd.LINK';link.symlink_to(self.parent,target_is_directory=True)
        with self.assertRaises(apple.ApplePolicyError):self.resolve([link/self.path.name])
    def test_rule_is_outbound_exact_literal_after_network_deny(self):
        rule=apple.testmanagerd_grant(self.path)
        self.assertEqual(rule,'(allow network-outbound (literal '+apple._scheme_string(str(self.path))+'))')
        self.assertNotIn('subpath',rule);self.assertNotIn('regex',rule);self.assertNotIn('file-write',rule)

    def test_discovery_queries_only_the_selected_device_and_fixed_env_key(self):
        result=mock.Mock(returncode=0,stdout=str(self.path)+'\n')
        with mock.patch.object(apple.subprocess,'run',return_value=result) as run:
            self.assertEqual(apple.testmanagerd_socket(sp.APPLE_UDID,root=self.root),self.path)
        self.assertEqual(run.call_args.args[0],[apple.XCRUN,'simctl','getenv',sp.APPLE_UDID,'TESTMANAGERD_SIM_SOCK'])
        self.assertEqual(run.call_args.kwargs['timeout'],10)
        self.assertNotIn('TESTMANAGERD_SIM_SOCK',run.call_args.kwargs['env'])
    def test_failed_empty_or_multiple_endpoint_discovery_never_broadens(self):
        for rc,out in [(1,str(self.path)),(0,'')]:
            with mock.patch.object(apple.subprocess,'run',return_value=mock.Mock(returncode=rc,stdout=out)):
                self.assertIsNone(apple.testmanagerd_socket(sp.APPLE_UDID,root=self.root))
        with mock.patch.object(apple.subprocess,'run',return_value=mock.Mock(returncode=0,stdout=str(self.path)+'\n'+str(self.path))):
            with self.assertRaises(apple.ApplePolicyError):apple.testmanagerd_socket(sp.APPLE_UDID,root=self.root)

from test_apple_verification import Fixture

class TestmanagerdProfileTests(Fixture):
    def test_full_profile_preserves_denies_and_adds_only_exact_socket_outbound(self):
        endpoint=Path('/private/var/tmp/com.apple.launchd.TEST123/com.apple.testmanagerd.unix-domain.socket')
        with mock.patch.object(apple,'testmanagerd_socket',return_value=endpoint):
            profile=self.profile()
        denies='(deny network* network-bind network-inbound network-outbound)'
        grant=apple.testmanagerd_grant(endpoint)
        self.assertLess(profile.index(denies),profile.index(grant))
        self.assertEqual([line for line in profile.splitlines() if line.startswith('(allow network')],[grant])
        self.assertFalse(any(str(endpoint) in line for line in profile.splitlines() if line.startswith('(allow file-write')))
    def test_missing_endpoint_adds_no_network_allow_rule(self):
        with mock.patch.object(apple,'testmanagerd_socket',return_value=None):profile=self.profile()
        self.assertNotIn('(allow network',profile)

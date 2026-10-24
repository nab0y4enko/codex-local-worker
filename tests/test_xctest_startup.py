"""XCTest developer-service permissions and build helper confinement."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import support as sp
from test_apple_verification import Fixture
apple=sp.lw.apple

class XCTestStartupPolicy(Fixture):
    def test_only_observed_runner_services_are_added_as_exact_names(self):
        profile=self.profile()
        for name in ('com.apple.testmanagerd.control','com.apple.dt.instruments.dtarbiter.xpc','com.apple.dt.instruments.dtsecurity.xpc'):
            self.assertIn(name,apple.MACH_SERVICES)
            self.assertIn('(global-name "'+name+'")',profile)
        self.assertEqual(apple.MACH_NAME_PATTERNS,(r'^com\.apple\.CoreSimulator\.[A-Za-z0-9]+$',r'^com\.apple\.dt\.[A-Za-z0-9]+$'))
        self.assertNotIn('(allow job-creation',profile)
        self.assertNotIn('(allow mach-register',profile)
        self.assertNotIn('(allow network-bind',profile)
        self.assertNotIn('^com\\.apple\\.instruments',profile)
        self.assertNotIn('(allow user-preference-write',profile)
    def test_asset_compiler_cannot_reuse_a_daemon_with_a_previous_profile(self):
        original={'HOME':'/test-home','CODEX_HOME':'/test-codex','IBToolNeverDeque':'0'}
        bounded=apple.verification_env(original,self.scratch)
        self.assertEqual(bounded['IBToolNeverDeque'],'1')
        self.assertEqual(bounded['HOME'],original['HOME'])
        self.assertEqual(bounded['CODEX_HOME'],original['CODEX_HOME'])
        self.assertEqual(original['IBToolNeverDeque'],'0')

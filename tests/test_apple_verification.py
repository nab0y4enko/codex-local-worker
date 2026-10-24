"""Opt-in Apple Seatbelt backend: profile bounds, argv wrapping and the readiness probe.

Nothing here nests a real Seatbelt apply: the probe is driven with the suite's mocked
``run_bounded``, so every assertion is about what the backend hands the launcher - the
profile text, the wrapped argv, the bounded roots and honest failure details. A test that
could only pass by *trusting* the backend is deliberately absent.
"""
import json
import os
import re
import subprocess
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
apple = lw.apple

CHECK_NAMES = ["xcodebuild-version", "simctl-available-devices", "allow-bounded-scratch-write",
               "deny-write-outside-worktree", "deny-loopback-tcp"]
REQUIRED_IPC = ("com.apple.CoreSimulator.CoreSimulatorService",
                "com.apple.CoreSimulator.simdiskimaged", "com.apple.dt.XCBuildService",
                "com.apple.cfprefsd", "com.apple.notifyd.register", "com.apple.logd",
                "com.apple.system.opendirectoryd.libinfo", "com.apple.SecurityServer",
                "com.apple.trustd", "com.apple.coreservices.launchservicesd",
                "/dev/dtracehelper", "/dev/null")
PLATFORM_HELPERS = ("com.apple.bsd.dirhelper", "com.apple.CoreServices.coreservicesd",
                    "com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent")
LOADER_SERVICES = ("com.apple.CoreSimulator.SimLaunchHost-arm64",
                   "com.apple.CoreSimulator.SimLaunchHost-x86")
POWER_ASSERTION_SERVICE = "com.apple.PowerManagement.control"


def covers(root, child):
    """Seatbelt ``subpath`` semantics: does ``child`` resolve inside ``root``?"""
    try:
        Path(os.path.realpath(str(child))).relative_to(os.path.realpath(str(root)))
        return True
    except ValueError:
        return False


def set_env(name, value):
    """Restore one environment variable exactly as it was, including absence."""
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


class FakeSubprocess:
    """``subprocess`` stand-in that records argv, for the fixed ``getconf`` fallback call."""

    SubprocessError = subprocess.SubprocessError

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(([str(item) for item in argv], dict(kwargs)))
        return types.SimpleNamespace(returncode=self.returncode, stdout=self.stdout,
                                     stderr=self.stderr)


class UuidValidation(unittest.TestCase):
    def test_bare_uuid_is_accepted_and_trimmed(self):
        self.assertEqual(apple.validate_simulator_udid(" " + sp.APPLE_UDID + " "), sp.APPLE_UDID)
        self.assertEqual(apple.validate_simulator_udid(sp.OTHER_UDID.lower()),
                         sp.OTHER_UDID.lower())

    def test_anything_that_is_not_a_bare_uuid_is_refused(self):
        for value in ("", "   ", "not-a-uuid", "../etc", "etc/passwd", sp.APPLE_UDID + "/",
                      sp.APPLE_UDID.replace("-", ""), sp.APPLE_UDID + "/..", "x" * 36,
                      "--udid", None, 7, [sp.APPLE_UDID], True):
            with self.subTest(value=value):
                with self.assertRaises(apple.ApplePolicyError):
                    apple.validate_simulator_udid(value)


class Fixture(unittest.TestCase):
    """Shared canonical layout: primary repo, worktree outside it, report-owned scratch."""

    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)
        self.home = sp.apple_home(self.root)
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.scratch = self.root / "report" / "apple-scratch"
        self.scratch.mkdir(parents=True)
        self.profile_path = self.root / "report" / apple.PROFILE_NAME
        # the one root the backend asks the OS about, so a fixture can assert against it
        try:
            self.darwin_temp = apple.darwin_temp_root()
        except apple.ApplePolicyError as error:
            self.skipTest(f"this host reports no usable Darwin temporary root: {error}")

    def profile(self, worktree=None, primary=None, job_temp=None, udid=None, home=None):
        return apple.build_profile(worktree or self.worktree, primary or self.repo,
                                   job_temp or self.scratch, udid or sp.APPLE_UDID,
                                   home or self.home)

    def roots(self, **overrides):
        passed = {"worktree": self.worktree, "primary": self.repo, "job_temp": self.scratch,
                  "simulator_udid": sp.APPLE_UDID, "home": self.home}
        passed.update(overrides)
        return apple.writable_roots(passed["worktree"], passed["primary"], passed["job_temp"],
                                    passed["simulator_udid"], passed["home"])

    def expected_roots(self, worktree=None, scratch=None, udid=None, home=None):
        home = Path(str(home or self.home))
        entries = [worktree or self.worktree, scratch or self.scratch,
                   home / apple.DEVICES_RELATIVE / (udid or sp.APPLE_UDID)]
        entries += [home / relative for relative in apple.APPLE_WRITE_RELATIVES]
        # the plan appends the OS-reported per-user temporary root last
        entries.append(self.darwin_temp)
        return [os.path.realpath(str(entry)) for entry in entries]

    def sandbox_exec_verdict(self, text):
        """Real Seatbelt syntax validation of one profile against a fixed sandboxed /usr/bin/true.

        0: the suite itself runs outside an outer sandbox and the profile applied cleanly.
        71 with sandbox_apply refused: the nested profile parsed and was accepted, and the
        apply failed only because this process already runs inside a Seatbelt sandbox.
        Anything else is a real profile problem - 65 in particular is what an invalid
        reporting modifier produces ("report modifier does not apply to deny action").
        Never a toolchain or simulator run, never an unsandboxed fallback.
        """
        result = subprocess.run([apple.SANDBOX_EXEC, "-p", text, "/usr/bin/true"],
                                capture_output=True, text=True, timeout=60)
        self.assertNotIn("report modifier", result.stderr)
        self.assertEqual(result.stdout, "")
        if result.returncode == 0:
            return result.returncode
        self.assertEqual(result.returncode, 71, result.stderr[-300:])
        self.assertIn("sandbox_apply", result.stderr)
        self.assertIn("Operation not permitted", result.stderr)
        return result.returncode


class ProfileBounds(Fixture):
    def test_deny_default_with_bounded_writes_and_no_network(self):
        text = self.profile()
        self.assertIn("(version 1)", text)
        self.assertIn("(deny default)", text)
        # exactly one default rule and it is the plain deny: sandbox-exec refuses
        # "(with report)" on a deny action ("report modifier does not apply to deny action",
        # exit 65), so the profile must never grow a reporting modifier again
        self.assertEqual(re.findall(r"^\(deny default.*\)$", text, re.M), ["(deny default)"])
        self.assertNotIn("with report", text)
        self.assertIn("(deny network* network-bind network-inbound network-outbound)", text)
        self.assertIn("(allow process-fork process-exec process-info* signal)", text)
        self.assertIn("(allow file-map-executable)", text)
        self.assertIn("(allow sysctl-read)", text)
        self.assertIn("(allow file-read* file-test-existence file-search)", text)
        # the bounded allow, the explicit deny and the one device-node exception: the shape is
        # pinned, so a new write statement is a deliberate change rather than an accident
        self.assertEqual([(action, sorted({kind for kind, _ in entries}))
                          for action, entries in sp.write_statements(text)],
                         [("allow", ["subpath"]), ("deny", ["subpath"]), ("allow", ["literal"])])

    def test_device_write_exception_is_one_literal_node_not_a_root(self):
        text = self.profile()
        self.assertEqual(sp.literal_paths(text), list(apple.DEVICE_WRITE_PATHS))
        self.assertEqual(sp.literal_paths(text, "deny"), [])
        write_lines = [line for line in text.splitlines() if "file-write*" in line]
        self.assertEqual(len(write_lines), 3)
        self.assertEqual(write_lines[-1], '(allow file-write* (literal "/dev/null"))')
        for forbidden in ('(subpath "/dev"', '(subpath "/"', "(allow file-write*)"):
            with self.subTest(fragment=forbidden):
                self.assertNotIn(forbidden, text)
        # Other /dev nodes never join the broad file-write* statement. The separate
        # PTY data-write/runtime-extension rules are checked in test_pty_permissions.
        for node in ("/dev/zero", "/dev/random", "/dev/urandom", "/dev/tty", "/dev/ptmx",
                     "/dev/dtracehelper"):
            with self.subTest(node=node):
                self.assertFalse(any(node in line for line in write_lines), node)
        for line in write_lines[:-1]:
            self.assertNotIn("/dev", line)
        self.assertIn("(deny default)", text)
        self.assertIn("(deny network* network-bind network-inbound network-outbound)", text)

    def test_profile_decisions_are_bounded_roots_plus_the_device_node(self):
        text = self.profile()
        self.assertTrue(sp.seatbelt_write_allowed(text, "/dev/null"))
        for target in ("/dev/zero", "/dev/random", "/dev", "/dev/console", "/etc/hosts",
                       "/tmp/anywhere", self.repo / "add.py",
                       self.worktree / ".git" / "HEAD", self.worktree / ".codex" / "config"):
            with self.subTest(denied=str(target)):
                self.assertFalse(sp.seatbelt_write_allowed(text, target))
        for target in (self.worktree / "build" / "out.o", self.scratch / "marker",
                       self.home / apple.DEVICES_RELATIVE / sp.APPLE_UDID / "device.log"):
            with self.subTest(allowed=str(target)):
                self.assertTrue(sp.seatbelt_write_allowed(text, target))

    def test_widening_the_device_exception_is_refused(self):
        original = apple.DEVICE_WRITE_PATHS
        self.addCleanup(setattr, apple, "DEVICE_WRITE_PATHS", original)
        for hostile in ("/", "/dev", "/Users", "/dev/sub", "/dev/null/", "dev/null", "", None,
                        "/private/tmp"):
            with self.subTest(path=hostile):
                apple.DEVICE_WRITE_PATHS = (hostile,)
                with self.assertRaises(apple.ApplePolicyError) as ctx:
                    self.profile()
                self.assertIn("device write exception", str(ctx.exception))
        apple.DEVICE_WRITE_PATHS = original
        self.assertIn('(allow file-write* (literal "/dev/null"))', self.profile())

    def test_writable_writes_are_exactly_the_bounded_roots(self):
        allowed, denied = sp.FakeRun._seatbelt_roots(self.profile())
        self.assertEqual(sorted(allowed), sorted(self.expected_roots()))
        self.assertEqual(len(allowed), 7)
        primary = os.path.realpath(str(self.repo))
        for required in [primary] + [os.path.realpath(str(self.worktree / name))
                                     for name in apple.PROTECTED_NAMES]:
            self.assertIn(required, denied)

    def test_primary_and_metadata_never_appear_as_writable(self):
        allowed, _ = sp.FakeRun._seatbelt_roots(self.profile())
        self.assertNotIn(os.path.realpath(str(self.repo)), allowed)
        for name in apple.PROTECTED_NAMES:
            self.assertNotIn(os.path.realpath(str(self.worktree / name)), allowed)
            self.assertNotIn(os.path.realpath(str(self.repo / name)), allowed)

    def test_a_primary_nested_in_the_temporary_root_stays_denied(self):
        # the per-user temporary root is a granted write bound, so this is the real nested
        # case: a primary checkout and the worktree metadata names placed *inside* a granted
        # root. Seatbelt keeps the last match, so the deny rules still win.
        nested = sp.tmpdir_under(self.darwin_temp)
        self.addCleanup(sp.cleanup, nested)
        nested_repo = sp.make_repo(nested)
        nested_worktree = nested / "wt"
        nested_worktree.mkdir()
        text = self.profile(worktree=nested_worktree, primary=nested_repo)
        allowed, _ = sp.FakeRun._seatbelt_roots(text)
        self.assertIn(str(self.darwin_temp), allowed)
        for target in (nested_repo / "add.py", nested_repo / "notes.md",
                       nested_worktree / ".git" / "HEAD",
                       nested_worktree / ".codex" / "config",
                       nested_worktree / ".agents" / "settings.json"):
            with self.subTest(denied=str(target)):
                self.assertFalse(sp.seatbelt_write_allowed(text, target), str(target))
        for target in (nested_worktree / "build" / "out.o", self.scratch / "marker",
                       self.darwin_temp / "shared" / "x.o"):
            with self.subTest(allowed=str(target)):
                self.assertTrue(sp.seatbelt_write_allowed(text, target), str(target))
        # the shared boundary is per-user, not per-job: anything under the OS root stays
        # writable - including another fixture's home inside it - which is exactly what
        # trust_limits() refuses to claim is job-confined
        shared = self.home / "Library" / "sneak.txt"
        self.assertTrue(covers(self.darwin_temp, shared), str(shared))
        self.assertTrue(sp.seatbelt_write_allowed(text, shared), str(shared))

    def test_the_temporary_grant_is_that_one_root_and_never_widens(self):
        text = self.profile()
        temp = str(self.darwin_temp)
        allowed, _ = sp.FakeRun._seatbelt_roots(text)
        self.assertEqual([entry for entry in allowed if entry == temp], [temp])
        # no ancestor of the OS root may be granted: /tmp, /private/tmp, a bare bucket, a bare
        # user directory or "/" would all swallow the deny rules above
        self.assertEqual([entry for entry in allowed
                          if entry != temp and temp.startswith(entry.rstrip("/") + "/")], [])
        for fragment in ('(subpath "/"', '(subpath "/private"', '(subpath "/private/tmp"',
                         '(subpath "/tmp"', '(subpath "/var/folders"',
                         '(subpath "/private/var/folders"',
                         '(subpath "%s"' % str(self.darwin_temp.parent.parent),
                         '(subpath "%s"' % str(self.darwin_temp.parent)):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, text)
        for target in ("/tmp/anywhere", "/private/tmp/anywhere", "/etc/hosts",
                       self.darwin_temp.parent / "other-user" / "x.o"):
            with self.subTest(denied=str(target)):
                self.assertFalse(sp.seatbelt_write_allowed(text, target), str(target))
        self.assertTrue(sp.seatbelt_write_allowed(text, self.darwin_temp / "job" / "x.o"))
        self.assertIn("(deny network* network-bind network-inbound network-outbound)", text)

    def test_mach_lookup_is_named_never_blanket(self):
        text = self.profile()
        lines = [line for line in text.splitlines() if line.startswith("(allow mach-lookup")]
        self.assertTrue(lines)
        for line in lines:
            self.assertTrue("global-name" in line or "local-name" in line, line)
        self.assertNotIn("(allow mach-lookup)", text)
        for name in REQUIRED_IPC:
            self.assertIn(name, text)
        for pattern in apple.MACH_NAME_PATTERNS:
            self.assertIn('#"' + pattern + '"', text)

    def test_observed_platform_helpers_and_read_only_preferences_are_named(self):
        text = self.profile()
        for name in PLATFORM_HELPERS:
            with self.subTest(service=name):
                self.assertIn('(global-name "%s")' % name, text)
        # the cfprefsd agent is a per-session service: it must also be bound by local-name
        self.assertIn('(allow mach-lookup (local-name "com.apple.cfprefsd.agent"))', text)
        self.assertEqual(list(apple.MACH_LOCAL_NAMES), ["com.apple.cfprefsd.agent"])
        # preferences are read, never written
        self.assertIn("(allow user-preference-read)", text)
        rules = [line for line in text.splitlines() if line.startswith("(")]
        self.assertEqual([line for line in rules if "preference" in line],
                         ["(allow user-preference-read)"])
        self.assertEqual(list(apple.PREFERENCE_READ_OPERATIONS), ["user-preference-read"])

    def test_simlaunchhost_runtime_loaders_are_named_exactly(self):
        text = self.profile()
        for name in LOADER_SERVICES:
            with self.subTest(service=name):
                self.assertIn('(global-name "%s")' % name, text)
                # the anchored patterns stop at [A-Za-z0-9]+, so these hyphenated names are
                # reachable only as exact grants - never by widening a pattern
                self.assertFalse([pattern for pattern in apple.MACH_NAME_PATTERNS
                                  if re.compile(pattern).match(name)], name)
        self.assertEqual(sorted(apple.MACH_NAME_PATTERNS),
                         [r"^com\.apple\.CoreSimulator\.[A-Za-z0-9]+$",
                          r"^com\.apple\.dt\.[A-Za-z0-9]+$"])
        # nothing else in the profile is hyphenated-by-wildcard or a prefix grant
        named = re.findall(r'\(global-name "((?:[^"\\]|\\.)*)"\)', text)
        self.assertEqual(sorted(named), sorted(apple.MACH_SERVICES))
        self.assertEqual(sorted(n for n in named if "-" in n), sorted(LOADER_SERVICES))
        for name in named:
            with self.subTest(service=name):
                for fragment in ("*", "[", "?", " "):
                    self.assertNotIn(fragment, name, name)
        self.assertEqual(re.findall(r'\(global-name #"[^"]*"\)', text),
                         ['(global-name #"^com\\.apple\\.CoreSimulator\\.[A-Za-z0-9]+$")',
                          '(global-name #"^com\\.apple\\.dt\\.[A-Za-z0-9]+$")'])
        self.assertNotIn("(allow mach-lookup)", text)

    def test_power_assertion_service_is_exact_and_widens_nothing(self):
        # observed failure, before any test case ran: "Failed to prevent system sleep during UI
        # testing. Error code: -536870199" (IOPMAssertionCreateWithName), while the live kernel
        # stream showed exactly one denial at that instant - "deny(1) mach-lookup
        # com.apple.PowerManagement.control". XCTest housekeeping asks powerd, outside the
        # profile, for the user-idle sleep assertion, so that one name is granted exactly.
        text = self.profile()
        self.assertEqual([name for name in apple.MACH_SERVICES if "PowerManagement" in name],
                         [POWER_ASSERTION_SERVICE])
        self.assertEqual([name for name in apple.MACH_LOCAL_NAMES
                          if "PowerManagement" in name], [])
        self.assertIn('(global-name "com.apple.PowerManagement.control")', text)
        # exact-name only: the anchored patterns are untouched and cannot reach it, so no
        # prefix, wildcard or new pattern was introduced to make it resolve
        self.assertFalse([pattern for pattern in apple.MACH_NAME_PATTERNS
                          if re.compile(pattern).match(POWER_ASSERTION_SERVICE)])
        named = re.findall(r'\(global-name "((?:[^"\\]|\\.)*)"\)', text)
        self.assertEqual(sorted(named), sorted(apple.MACH_SERVICES))
        self.assertEqual(len(re.findall("PowerManagement", text)), 1)
        for forbidden in ('(global-name "com.apple.PowerManagement")', '(global-name "*")',
                          '(global-name #"^com\\.apple\\.PowerManagement', "(allow mach-lookup)",
                          "(allow iokit", "iokit-user-client-class", "job-creation",
                          "system-socket", "user-preference-write", "power-preference"):
            with self.subTest(fragment=forbidden):
                self.assertNotIn(forbidden, text)
        # every other bound stays as it was: same write roots, same denies, still no network
        allowed, denied = sp.FakeRun._seatbelt_roots(text)
        self.assertEqual(sorted(allowed), sorted(self.expected_roots()))
        write_lines = [line for line in text.splitlines() if "file-write*" in line]
        self.assertEqual(len(write_lines), 3)
        self.assertEqual(write_lines[-1], '(allow file-write* (literal "/dev/null"))')
        self.assertIn(os.path.realpath(str(self.repo)), denied)
        self.assertIn("(deny network* network-bind network-inbound network-outbound)", text)

    def test_helper_grants_never_widen_to_mach_preferences_or_writable_roots(self):
        text = self.profile()
        for forbidden in ("user-preference-write", "preference-write", "(allow preference",
                          "(allow mach-lookup)", "(local-name #", "(global-name \"*\")",
                          "(global-name #\"^com\\.apple\\.\")"):
            with self.subTest(fragment=forbidden):
                self.assertNotIn(forbidden, text)
        for line in text.splitlines():
            if line.startswith("(allow mach-lookup"):
                with self.subTest(line=line):
                    self.assertTrue("global-name \"" in line or "global-name #\"" in line
                                    or 'local-name "' in line, line)
        self.assertEqual(sorted(re.findall(r'\(local-name "((?:[^"\\]|\\.)*)"\)', text)),
                         sorted(apple.MACH_LOCAL_NAMES))
        # every pre-existing bound is exactly as it was: same roots, same /dev/null node,
        # same network denial, nothing under the home directory or the filesystem root
        allowed, denied = sp.FakeRun._seatbelt_roots(text)
        self.assertEqual(sorted(allowed), sorted(self.expected_roots()))
        write_lines = [line for line in text.splitlines() if "file-write*" in line]
        self.assertEqual(len(write_lines), 3)
        self.assertEqual(write_lines[-1], '(allow file-write* (literal "/dev/null"))')
        for forbidden in ('(subpath "/"', '(subpath "/tmp"', '(subpath "/Users"'):
            with self.subTest(fragment=forbidden):
                self.assertNotIn(forbidden, text)
        self.assertNotIn('(subpath "%s"' % os.path.realpath(str(self.home)), text)
        self.assertIn("(deny default)", text)
        self.assertIn("(deny network* network-bind network-inbound network-outbound)", text)
        self.assertTrue(any(str(self.repo) in entry or os.path.realpath(str(self.repo)) in entry
                            for entry in denied), str(denied))

    def test_profile_holds_no_shell_or_caller_supplied_text(self):
        text = self.profile()
        for forbidden in ("/bin/sh", " -c ", "(import", "system(", "call-with"):
            self.assertNotIn(forbidden, text)
        self.assertTrue(all(line.startswith((";", "(")) or not line.strip()
                            for line in text.splitlines()))

    def test_profile_is_deterministic_and_changes_with_the_inputs(self):
        text = self.profile()
        self.assertEqual(apple.profile_sha256(text), apple.profile_sha256(self.profile()))
        other_home = sp.apple_home(self.root, udid=sp.OTHER_UDID, name="home")
        other = self.profile(udid=sp.OTHER_UDID, home=other_home)
        self.assertNotEqual(apple.profile_sha256(text), apple.profile_sha256(other))
        elsewhere = self.root / "other-scratch"
        elsewhere.mkdir()
        self.assertNotEqual(apple.profile_sha256(text),
                            apple.profile_sha256(self.profile(job_temp=elsewhere)))

    def test_quotes_and_backslashes_in_paths_are_escaped_not_injected(self):
        weird = self.root / 'wei"rd\\dir'
        weird.mkdir()
        text = self.profile(worktree=weird)
        self.assertIn('wei\\"rd\\\\dir', text)
        self.assertNotIn('wei"rd\\dir"', text)
        allowed, _ = sp.FakeRun._seatbelt_roots(text)
        self.assertEqual(sorted(allowed),
                         sorted(self.expected_roots(worktree=weird)))
        self.assertEqual([action for action, _ in sp.write_statements(text)],
                         ["allow", "deny", "allow"])

    def test_control_characters_in_paths_are_refused(self):
        hostile = self.root / "con\ntrol"
        hostile.mkdir()
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            self.profile(job_temp=hostile)
        self.assertIn("control character", str(ctx.exception))

    def test_worktree_gitdir_file_is_resolved_and_denied(self):
        gitdir = self.repo / ".git" / "worktrees" / "wt-1"
        gitdir.mkdir(parents=True)
        (self.worktree / ".git").write_text("gitdir: " + str(gitdir) + "\n", encoding="utf-8")
        self.assertEqual(apple.resolved_gitdir(self.worktree),
                         Path(os.path.realpath(str(gitdir))))
        _, denied = sp.FakeRun._seatbelt_roots(self.profile())
        self.assertIn(os.path.realpath(str(gitdir)), denied)
        (self.worktree / ".git").unlink()
        self.assertIsNone(apple.resolved_gitdir(self.worktree))

    @unittest.skipUnless(sys.platform == "darwin", "needs the real macOS Seatbelt parser")
    def test_generated_profile_is_valid_to_sandbox_exec(self):
        # one fixed sandboxed /usr/bin/true: real syntax validation of the current template
        self.sandbox_exec_verdict(self.profile())

    def test_debug_deny_is_deny_diagnostics_and_not_a_grant(self):
        # the installed /System/Library/Sandbox/Profiles log refusals with exactly this
        # modifier, so a pathless permissionDenied turns into a denials line with its path
        text = self.profile()
        self.assertEqual(re.findall(r"^\(debug .*\)$", text, re.M), ["(debug deny)"])
        # the default rule stays the single plain deny: the modifier decorates it, never
        # replaces it, and no allow-form debug or reporting modifier ever shows up
        self.assertEqual(re.findall(r"^\((?:allow|deny) default.*\)$", text, re.M),
                         ["(deny default)"])
        for forbidden in ("(debug allow", "(debug all", "(with report", "(debug deny)\n(allow"):
            with self.subTest(fragment=forbidden):
                self.assertNotIn(forbidden, text)
        # the modifier sits with the default deny, ahead of every grant, so it decorates the
        # deny rather than any allow. Diagnostics only: the effective bounds are unchanged -
        # same bounded allow, same explicit deny, same one device-node exception
        lines = text.splitlines()
        self.assertLess(lines.index("(debug deny)"),
                        min(index for index, line in enumerate(lines)
                            if line.startswith("(allow ")))
        self.assertEqual([(action, sorted({kind for kind, _ in entries}))
                          for action, entries in sp.write_statements(text)],
                         [("allow", ["subpath"]), ("deny", ["subpath"]), ("allow", ["literal"])])
        self.assertEqual(sp.literal_paths(text), list(apple.DEVICE_WRITE_PATHS))
        allowed, denied = sp.FakeRun._seatbelt_roots(text)
        self.assertEqual(sorted(allowed), sorted(self.expected_roots()))
        self.assertTrue(any(os.path.realpath(str(self.repo)) in entry for entry in denied),
                        str(denied))
        self.assertIn("(deny network* network-bind network-inbound network-outbound)", text)
        for target in (self.repo / "add.py", self.worktree / ".git" / "HEAD", "/tmp/anywhere",
                       "/etc/hosts"):
            with self.subTest(denied=str(target)):
                self.assertFalse(sp.seatbelt_write_allowed(text, target), str(target))
        self.assertTrue(sp.seatbelt_write_allowed(text, self.worktree / "build" / "out.o"))

    @unittest.skipUnless(sys.platform == "darwin", "needs the real macOS Seatbelt parser")
    def test_debug_deny_compiles_in_the_real_parser(self):
        # the diagnostics rule has to be accepted by this host's parser, and it must not change
        # what the parser makes of the profile: same verdict with and without the modifier
        text = self.profile()
        stripped = "\n".join(line for line in text.splitlines()
                             if not line.startswith("(debug "))
        self.assertIn("(debug deny)", text)
        self.assertNotIn("(debug ", stripped)
        self.assertEqual(self.sandbox_exec_verdict(stripped), self.sandbox_exec_verdict(text))

class DarwinTempRoot(unittest.TestCase):
    """The one OS-derived grant: what a resolver may report, and everything that is refused.

    The resolver is patched only where a fixture needs a portable answer; the host's own
    reported root and this host's ``/usr/bin/getconf`` argv are always checked for real.
    """

    FOREIGN = "/private/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T"
    HAS_GETCONF = sys.platform == "darwin" and os.path.isfile(apple.GETCONF)

    def setUp(self):
        try:
            self.real = apple.darwin_temp_root()
        except apple.ApplePolicyError as error:
            self.skipTest(f"this host reports no usable Darwin temporary root: {error}")

    def patch(self, target, value):
        original = getattr(target[0], target[1])
        self.addCleanup(setattr, target[0], target[1], original)
        setattr(target[0], target[1], value)

    def root_from(self, value):
        return apple.darwin_temp_root(resolver=lambda: value)

    def refused(self, value, expected):
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            self.root_from(value)
        self.assertIn(expected, str(ctx.exception))

    def test_the_host_root_is_the_os_answer_and_passes_every_bound(self):
        self.assertTrue(self.real.is_absolute())
        self.assertFalse(str(self.real).endswith(os.sep))
        self.assertTrue(apple.DARWIN_TEMP_SHAPE_RE.match(str(self.real)), str(self.real))
        self.assertTrue(self.real.is_dir())
        self.assertFalse(self.real.is_symlink())
        self.assertEqual(os.stat(str(self.real)).st_uid, os.getuid())
        reported = apple.darwin_user_temp_dir()
        self.assertIsNotNone(reported)
        self.assertEqual(Path(os.path.realpath(str(reported))), self.real)
        self.assertEqual(apple.darwin_temp_root(), self.real)

    def test_the_os_query_answers_before_any_caller_supplied_path(self):
        reported = apple._confstr_darwin_user_temp_dir()
        if reported is None:
            self.skipTest("this CPython build answers neither confstr spelling")
        self.assertEqual(Path(os.path.realpath(str(reported))), self.real)
        self.assertEqual(apple.DARWIN_USER_TEMP_CONFSTR, 65537)
        self.assertEqual(apple.DARWIN_USER_TEMP_NAME, "CS_DARWIN_USER_TEMP_DIR")
        self.assertEqual(apple.GETCONF_NAME, "DARWIN_USER_TEMP_DIR")

    def test_trailing_separator_and_surrounding_blanks_are_canonicalized(self):
        for value in (str(self.real), str(self.real) + os.sep, str(self.real) + "/x/..",
                      " " + str(self.real) + "  "):
            with self.subTest(value=value):
                self.assertEqual(apple.darwin_temp_root(resolver=lambda value=value: value),
                                 self.real)

    def test_a_decoy_tmpdir_is_refused_rather_than_granted(self):
        # libc confstr consults TMPDIR when it names an accessible directory, so the grant
        # comes from validating the answer rather than from trusting it: a decoy TMPDIR is
        # refused, never granted, and never quietly widened to cover it
        decoy = sp.tmpdir()
        self.addCleanup(sp.cleanup, decoy)  # registered first, so removed last
        for name in ("TMPDIR", "TMP", "TEMP"):
            self.addCleanup(set_env, name, os.environ.get(name))
            os.environ[name] = str(decoy)
        # a decoy nested inside the granted root is exactly the plausible answer that must fail
        self.assertTrue(covers(self.real, decoy), str(decoy))
        self.assertNotEqual(Path(os.path.realpath(str(decoy))), self.real)
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            apple.darwin_temp_root(resolver=lambda: str(decoy))
        self.assertIn("must be the canonical", str(ctx.exception))
        # Some macOS versions cache/ignore TMPDIR in confstr. Either refusal or
        # the previously established canonical OS root is safe; a decoy grant is not.
        try:
            resolved = apple.darwin_temp_root()
        except apple.ApplePolicyError:
            pass
        else:
            self.assertEqual(resolved, self.real)
            self.assertNotEqual(resolved, Path(os.path.realpath(str(decoy))))
        for value in ("/tmp", "/private/tmp", "/var/tmp", "/Users"):
            os.environ["TMPDIR"] = value
            with self.subTest(tmpdir=value):
                with self.assertRaises(apple.ApplePolicyError):
                    apple.darwin_temp_root(resolver=lambda: value)
                try:
                    resolved = apple.darwin_temp_root()
                except apple.ApplePolicyError:
                    continue
                self.assertEqual(resolved, self.real)

    def test_no_tmpdir_either_answers_the_canonical_root_or_fails_closed(self):
        saved = {name: os.environ.get(name) for name in ("TMPDIR", "TMP", "TEMP")}
        for name, value in saved.items():
            self.addCleanup(set_env, name, value)
            os.environ.pop(name, None)
        reported = apple.darwin_user_temp_dir()
        if reported is None:
            with self.assertRaises(apple.ApplePolicyError) as ctx:
                apple.darwin_temp_root()
            self.assertIn("did not report", str(ctx.exception))
        else:
            self.assertEqual(Path(os.path.realpath(str(reported))), self.real)
            self.assertEqual(apple.darwin_temp_root(), self.real)

    def test_confstr_tries_the_numeric_constant_before_the_apple_string_name(self):
        seen = []

        def confstr(name):
            seen.append(name)
            return " " + str(self.real) + "\n"

        self.patch((os, "confstr"), confstr)
        self.assertEqual(apple._confstr_darwin_user_temp_dir(), str(self.real))
        self.assertEqual(seen, [apple.DARWIN_USER_TEMP_CONFSTR])

    def test_confstr_falls_back_to_the_string_name_the_numeric_build_refuses(self):
        seen = []

        def confstr(name):
            seen.append(name)
            if name == apple.DARWIN_USER_TEMP_CONFSTR:
                raise ValueError("unrecognized configuration name")
            return str(self.real)

        self.patch((os, "confstr"), confstr)
        self.assertEqual(apple._confstr_darwin_user_temp_dir(), str(self.real))
        self.assertEqual(seen, [apple.DARWIN_USER_TEMP_CONFSTR, apple.DARWIN_USER_TEMP_NAME])

    def test_neither_os_query_answered_is_reported_as_no_answer(self):
        def confstr(name):
            raise OSError("no configuration")

        self.patch((os, "confstr"), confstr)
        self.patch((apple, "subprocess"), FakeSubprocess(returncode=1, stdout=""))
        self.assertIsNone(apple._confstr_darwin_user_temp_dir())
        self.assertIsNone(apple.darwin_user_temp_dir())

    @unittest.skipUnless(HAS_GETCONF, "needs the fixed /usr/bin/getconf fallback binary")
    def test_getconf_fallback_argv_is_the_name_getconf_accepts(self):
        fake = FakeSubprocess(returncode=0, stdout=str(self.real) + "\n")
        self.patch((apple, "subprocess"), fake)
        self.assertEqual(apple._getconf_darwin_user_temp_dir(), str(self.real))
        self.assertEqual(len(fake.calls), 1)
        argv, kwargs = fake.calls[0]
        self.assertEqual(argv, [apple.GETCONF, apple.GETCONF_NAME])
        # the confstr spelling is NOT a getconf parameter: passing CS_DARWIN_USER_TEMP_DIR
        # exits 64 ("no such configuration parameter"), which silently kills the fallback
        self.assertNotEqual(apple.GETCONF_NAME, apple.DARWIN_USER_TEMP_NAME)
        for item in argv:
            self.assertNotIn("CS_", item, item)
        self.assertEqual(kwargs["timeout"], apple.GETCONF_TIMEOUT_SECONDS)
        self.assertTrue(kwargs["capture_output"] and kwargs["text"])
        fake.calls[:] = []
        fake.returncode = 64
        self.assertIsNone(apple._getconf_darwin_user_temp_dir())
        self.assertEqual(fake.calls[0][0], [apple.GETCONF, apple.GETCONF_NAME])

    @unittest.skipUnless(HAS_GETCONF, "needs the real /usr/bin/getconf")
    def test_the_real_getconf_answers_only_the_name_the_fallback_uses(self):
        wrong = subprocess.run([apple.GETCONF, apple.DARWIN_USER_TEMP_NAME],
                               capture_output=True, text=True, timeout=30)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("no such configuration parameter", wrong.stderr)
        right = subprocess.run([apple.GETCONF, apple.GETCONF_NAME],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(right.returncode, 0, right.stderr)
        self.assertEqual(Path(os.path.realpath(right.stdout.strip())), self.real)
        # with confstr unavailable the fallback alone has to resolve the same root
        self.patch((apple, "_confstr_darwin_user_temp_dir"), lambda: None)
        self.assertEqual(Path(os.path.realpath(str(apple.darwin_user_temp_dir()))), self.real)
        self.assertEqual(apple.darwin_temp_root(), self.real)

    def test_the_default_resolver_is_the_module_level_os_query(self):
        self.patch((apple, "darwin_user_temp_dir"), lambda: str(self.real) + "/Changed")
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            apple.darwin_temp_root()
        self.assertIn("Changed", str(ctx.exception))

    def test_any_root_that_is_not_the_current_users_temporary_root_is_refused(self):
        cases = [("/", "must be the canonical"), ("/private", "must be the canonical"),
                 ("/private/tmp", "must be the canonical"),
                 ("/private/var", "must be the canonical"),
                 ("/var/folders", "must be the canonical"),
                 ("/private/var/folders", "must be the canonical"),
                 (str(self.real.parent.parent), "must be the canonical"),
                 (str(self.real.parent), "must be the canonical"),
                 ("/private/var/folders/qq/zzzzzzzzzzzzzzzzzzzzzzz/T",
                  "not an existing directory"),
                 (str(self.real) + "/Extra", "not an existing directory"),
                 ("T", "absolute path"), ("var/folders/x/T", "absolute path")]
        for value, expected in cases:
            with self.subTest(value=value):
                self.refused(value, expected)
        self.refused("/tmp", "must not be a symlink" if os.path.islink("/tmp")
                     else "must be the canonical")

    def test_a_non_textual_or_blank_answer_is_refused(self):
        for value in (None, "", "   ", 7, [str(self.real)], {}, True):
            with self.subTest(value=value):
                self.refused(value, "did not report")

    def test_a_control_character_never_enters_a_profile(self):
        for value in (str(self.real) + "\x00", "\x01" + str(self.real),
                      str(self.real).replace("/T", "/T\x07")):
            with self.subTest(value=repr(value)):
                self.refused(value, "contains a control character")

    def test_another_uids_temporary_root_is_refused_by_name(self):
        foreign = self.FOREIGN
        if not os.path.isdir(foreign):
            self.skipTest(f"this host has no second per-user temporary root at {foreign}")
        if os.stat(foreign).st_uid == os.getuid():
            self.skipTest(f"{foreign} is owned by the current uid on this host")
        message = ""
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            self.root_from(foreign)
        message = str(ctx.exception)
        self.assertIn("someone else's temporary root", message)
        self.assertIn(str(os.getuid()), message)
        self.assertIn(foreign, message)

    def test_a_planted_symlink_is_refused_and_an_ancestor_link_is_canonicalized(self):
        base = sp.tmpdir()
        self.addCleanup(sp.cleanup, base)
        final = base / "link"
        ancestor = base / "ancestor"
        try:
            os.symlink(str(self.real), str(final))
            os.symlink(str(self.real.parent), str(ancestor))
        except OSError as error:
            self.skipTest(f"this host refuses symlink fixtures: {error}")
        self.refused(str(final), "must not be a symlink")
        self.assertEqual(apple.darwin_temp_root(resolver=lambda: str(ancestor / "T")),
                         self.real)

    def test_a_resolver_that_raises_fails_closed_instead_of_widening(self):
        for error in (OSError("no such directory"), ValueError("unrecognized name"),
                      TypeError("not a path")):
            with self.subTest(error=type(error).__name__):
                def raiser(error=error):
                    raise error

                with self.assertRaises(apple.ApplePolicyError) as ctx:
                    apple.darwin_temp_root(resolver=raiser)
                self.assertIn("could not be resolved", str(ctx.exception))

    def test_no_os_answer_fails_closed_without_widening_to_tmp_or_a_bucket(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                with self.assertRaises(apple.ApplePolicyError) as ctx:
                    self.root_from(value)
                message = str(ctx.exception)
                self.assertIn("did not report", message)
                self.assertIn("instead of widening /tmp or a bare /var/folders", message)
                self.assertIn("confstr CS_DARWIN_USER_TEMP_DIR #65537", message)
                self.assertIn("/usr/bin/getconf DARWIN_USER_TEMP_DIR", message)


class UnsafeRoots(Fixture):
    def assert_refused(self, label, *, expected=None, **overrides):
        with self.subTest(case=label):
            with self.assertRaises(apple.ApplePolicyError) as ctx:
                self.profile(**overrides)
            if expected:
                self.assertIn(expected, str(ctx.exception))

    def test_scratch_must_live_outside_the_writable_worktree(self):
        inside = self.worktree / "scratch"
        inside.mkdir()
        self.assert_refused("scratch in worktree", job_temp=inside,
                            expected="outside the worker-writable worktree")

    def test_scratch_must_live_outside_the_primary_checkout(self):
        inside = self.repo / "scratch"
        inside.mkdir()
        self.assert_refused("scratch in primary", job_temp=inside,
                            expected="outside the primary checkout")

    def test_worktree_must_live_outside_the_primary_checkout(self):
        inside = self.repo / "wt"
        inside.mkdir()
        self.assert_refused("worktree in primary", worktree=inside,
                            expected="must live outside the primary checkout")

    def test_home_and_filesystem_root_are_never_writable_roots(self):
        self.assert_refused("job temp is home", job_temp=self.home,
                            expected="whole home directory")
        self.assert_refused("job temp is root", job_temp=Path(os.sep),
                            expected="filesystem root is never a writable root")
        self.assert_refused("worktree is home", worktree=self.home,
                            expected="whole home directory")

    def test_missing_or_relative_directories_are_refused(self):
        self.assert_refused("scratch missing", job_temp=self.root / "nope",
                            expected="not an existing directory")
        self.assert_refused("relative worktree", worktree=Path("wt"),
                            expected="absolute path")

    def test_selected_device_must_be_a_real_directory_under_the_device_root(self):
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            apple.simulator_device_dir(self.home, sp.OTHER_UDID)
        self.assertIn("has no device directory", str(ctx.exception))
        escape_home = self.root / "escape-home"
        devices = escape_home / apple.DEVICES_RELATIVE
        devices.mkdir(parents=True)
        os.symlink(str(self.worktree), str(devices / sp.APPLE_UDID))
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            apple.simulator_device_dir(escape_home, sp.APPLE_UDID)
        self.assertIn("must not be a symlink", str(ctx.exception))

    def test_writable_roots_are_the_bounded_list(self):
        roots = self.roots()
        self.assertEqual([os.path.realpath(str(entry)) for entry in roots],
                         self.expected_roots())
        self.assertTrue(all(Path(entry).is_absolute() for entry in roots))

    def test_an_unusable_temporary_root_blocks_the_profile_instead_of_widening(self):
        # no fallback grant: if the OS root cannot be validated the whole profile is refused,
        # rather than granting /tmp, a bare bucket or a wider root to keep going
        original = apple.darwin_user_temp_dir
        self.addCleanup(setattr, apple, "darwin_user_temp_dir", original)
        for value, expected in ((None, "did not report"), ("/", "must be the canonical"),
                                ("/private/tmp", "must be the canonical"),
                                ("/private/var/folders", "must be the canonical"),
                                ("/Users", "must be the canonical"),
                                ("T", "absolute path"),
                                (str(self.scratch), "must be the canonical")):
            with self.subTest(value=value):
                apple.darwin_user_temp_dir = lambda value=value: value
                with self.assertRaises(apple.ApplePolicyError) as ctx:
                    self.profile()
                self.assertIn(expected, str(ctx.exception))
        apple.darwin_user_temp_dir = original
        self.assertIn(str(self.darwin_temp), self.profile())


class ArgvWrapping(unittest.TestCase):
    def test_every_command_is_prefixed_with_the_profile(self):
        wrapped = apple.sandbox_argv(Path("/r/apple-verify.sb"),
                                     ["python3", "-m", "unittest", "-v"])
        self.assertEqual(wrapped, [apple.SANDBOX_EXEC, "-f", "/r/apple-verify.sb",
                                   "python3", "-m", "unittest", "-v"])
        self.assertEqual(apple.sandbox_argv_template()[0], apple.SANDBOX_EXEC)

    def test_there_is_no_call_path_that_returns_bare_argv(self):
        for command in (["pytest"], ["/bin/sh", "-c", "echo hi"], ["x"], [" python3 ", "-V"]):
            with self.subTest(command=command):
                wrapped = apple.sandbox_argv(Path("/r/p.sb"), command)
                self.assertEqual(wrapped[0], apple.SANDBOX_EXEC)
                self.assertEqual(wrapped[1:3], ["-f", "/r/p.sb"])
                self.assertNotEqual(wrapped[3:], [])
        self.assertEqual(apple.sandbox_argv(Path("/r/p.sb"), ["x"],
                                            sandbox_exec="/opt/sbx"),
                         ["/opt/sbx", "-f", "/r/p.sb", "x"])

    def test_empty_command_and_profile_are_refused(self):
        for command in ([], [""], ["   "]):
            with self.subTest(command=command):
                with self.assertRaises(apple.ApplePolicyError):
                    apple.sandbox_argv(Path("/r/p.sb"), command)
        for profile in ("", "   ", ".", "..", Path(".")):
            with self.subTest(profile=profile):
                with self.assertRaises(apple.ApplePolicyError):
                    apple.sandbox_argv(profile, ["python3", "-V"])

    def test_verification_env_bounds_temp_and_module_cache(self):
        root = sp.tmpdir()
        self.addCleanup(sp.cleanup, root)
        scratch = root / "report" / "apple-scratch"
        scratch.mkdir(parents=True)
        original = {"HOME": "/Users/dev", "CODEX_HOME": "/Users/dev/.codex",
                    "TMPDIR": "/var/folders/host", "PATH": "/usr/bin"}
        bounded = apple.verification_env(dict(original), scratch)
        bounded_root = os.path.realpath(str(scratch))
        self.assertEqual(bounded, dict(original, **{
            "TMPDIR": bounded_root + "/tmp", "TMP": bounded_root + "/tmp",
            "TEMP": bounded_root + "/tmp",
            "CLANG_MODULE_CACHE_PATH": bounded_root + "/ModuleCache",
            apple.FOUNDATION_HOME_ENV: bounded_root + "/foundation-home",
            "IBToolNeverDeque": "1"}))
        self.assertEqual(bounded["HOME"], "/Users/dev")
        self.assertEqual(bounded["CODEX_HOME"], "/Users/dev/.codex")
        self.assertTrue((scratch / "tmp").is_dir() and (scratch / "ModuleCache").is_dir())
        # Foundation resolves ~/Library through CFFIXED_USER_HOME: an empty scratch dir,
        # nothing copied from the real account.
        self.assertTrue((scratch / apple.FOUNDATION_HOME_RELATIVE).is_dir())
        self.assertEqual(list((scratch / apple.FOUNDATION_HOME_RELATIVE).iterdir()), [])
        self.assertEqual(original["TMPDIR"], "/var/folders/host")

    def test_trust_limits_are_reported_not_promised(self):
        limits = apple.trust_limits()
        self.assertTrue(len(limits) >= 4)
        for limit in limits:
            self.assertTrue(limit.startswith("Apple backend: "))
        joined = " ".join(limits)
        for keyword in ("file-read*", "own privileges", "SHA-256"):
            self.assertIn(keyword, joined)

    def test_trust_limits_disclose_the_shared_temporary_root(self):
        # the granted Darwin temp root is that user's shared directory, so the limits must
        # say out loud that temporary writes are not job-confined
        joined = " ".join(apple.trust_limits())
        for keyword in ("OS-reported Darwin temporary root", "NOT confined to this job",
                        "job-only temporary confinement is not claimed"):
            self.assertIn(keyword, joined)
        for keyword in (apple.FOUNDATION_HOME_ENV, "foundation-home",
                        "passed through unchanged"):
            self.assertIn(keyword, joined)


class FamilyRuntime(unittest.TestCase):
    def test_only_the_tvos_family_token_counts(self):
        for runtime in ("tvOS-18.0", sp.APPLE_RUNTIME, "tvOS-26.1"):
            self.assertTrue(apple.is_family_runtime(runtime), runtime)
        for runtime in (sp.IOS_RUNTIME, "watchOS-11.0", "xROS-2.0", "AppleTVOS-18", "", None,
                        "com.apple.CoreSimulator.SimRuntime.iOS-18-0"):
            self.assertFalse(apple.is_family_runtime(runtime), runtime)


class ReadinessProbe(Fixture):
    def probe(self, apple_kw=None, runner=None, udid=None, secrets=(), deadline=None,
              home=None, python_executable=None):
        fake = runner if runner is not None else sp.FakeRun(apple=apple_kw or {})
        result = apple.probe_apple(self.worktree, self.repo, self.scratch, self.profile_path,
                                   udid or sp.APPLE_UDID, self.env(), secrets, deadline,
                                   runner=fake, home=home or self.home,
                                   python_executable=python_executable)
        return result, fake

    def env(self):
        return {"HOME": str(self.home), "CODEX_HOME": str(self.root / "codex-home"),
                "PATH": "/usr/bin:/bin", "TMPDIR": "/var/folders/host"}

    def test_happy_probe_reports_concrete_evidence(self):
        result, fake = self.probe()
        self.assertTrue(result["available"], result["detail"])
        self.assertEqual([entry["name"] for entry in result["checks"]], CHECK_NAMES)
        self.assertTrue(all(entry["ok"] for entry in result["checks"]))
        self.assertEqual(result["failed_checks"], [])
        self.assertEqual(result["backend"], apple.BACKEND)
        self.assertEqual(result["network"], "disabled")
        self.assertEqual(result["simulator_udid"], sp.APPLE_UDID)
        self.assertTrue(result["device_dir"].endswith(sp.APPLE_UDID))
        self.assertEqual(result["devices"]["available_count"], 1)
        self.assertTrue(result["devices"]["selected_present"])
        self.assertEqual(result["writable_roots"], self.expected_roots())
        self.assertIn(os.path.realpath(str(self.repo)), result["denied_write_roots"])
        self.assertIn("never a blanket allowance", result["mach_lookup_policy"])
        self.assertEqual(result["argv_prefix"], [apple.SANDBOX_EXEC, "-f",
                                                 str(self.profile_path)])
        self.assertEqual(result["trust_limits"], list(apple.trust_limits()))
        self.assertEqual(sorted(result["env"]),
                         [apple.FOUNDATION_HOME_ENV, "CLANG_MODULE_CACHE_PATH", "IBToolNeverDeque", "TEMP", "TMP",
                          "TMPDIR"])
        scratch = os.path.realpath(str(self.scratch))
        for name, value in result["env"].items():
            if name == "IBToolNeverDeque":
                self.assertEqual(value, "1")
            else:
                self.assertTrue(value.startswith(scratch + os.sep), name)
        self.assertEqual(result["env_preserved"], ["HOME", "CODEX_HOME"])
        # the one OS-derived grant, and the Foundation redirect, are both reported
        self.assertEqual(result["darwin_temp_dir"], str(self.darwin_temp))
        self.assertEqual(result["foundation_home"], scratch + "/foundation-home")
        self.assertTrue(Path(result["foundation_home"]).is_dir())
        self.assertTrue(result["negative_write_target"])
        self.assertTrue(json.dumps(result))

    def test_negative_write_target_is_outside_every_granted_and_denied_root(self):
        # a refusal only proves confinement when the probed path really is out of bounds:
        # the shared per-user temporary root is granted, so the scratch's own parent (a
        # fixture directory inside that root) must never be used as the negative target
        result, fake = self.probe()
        negative = result["negative_write_target"]
        self.assertTrue(Path(negative).is_absolute())
        for bound in result["writable_roots"] + result["denied_write_roots"]:
            self.assertFalse(covers(bound, negative), bound)
        self.assertFalse(covers(self.darwin_temp, negative))
        self.assertFalse(sp.seatbelt_write_allowed(self.profile(), negative))
        self.assertFalse(Path(negative).exists())
        touches = [entry for entry in result["checks"] if entry["argv"][3] == apple.TOUCH]
        self.assertEqual(len(touches), 2)
        refusal = [entry for entry in touches if entry["name"] == "deny-write-outside-worktree"]
        self.assertEqual(len(refusal), 1)
        self.assertEqual(refusal[0]["argv"], [apple.SANDBOX_EXEC, "-f", str(self.profile_path),
                                             apple.TOUCH, negative])
        self.assertTrue(refusal[0]["ok"], refusal[0]["detail"])
        self.assertIn("was refused by the sandbox", refusal[0]["detail"])
        positive = [entry for entry in result["checks"]
                    if entry["name"] == "allow-bounded-scratch-write"][0]["argv"][-1]
        self.assertEqual(positive,
                         os.path.realpath(str(self.scratch)) + "/seatbelt-write-marker")
        self.assertTrue(sp.seatbelt_write_allowed(self.profile(), positive))
        self.assertNotEqual(positive, negative)

    def test_unusable_negative_target_refuses_the_probe_instead_of_faking_confinement(self):
        # with the host fallback aimed at a granted root, and this fixture's worktree parent
        # itself inside the shared temporary root, no out-of-bounds candidate exists: the
        # backend has to refuse rather than probe a path the profile allows
        original = apple.HOST_TEMP_FALLBACK
        self.addCleanup(setattr, apple, "HOST_TEMP_FALLBACK", original)
        apple.HOST_TEMP_FALLBACK = str(self.darwin_temp)
        fake = sp.FakeRun()
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            apple.probe_apple(self.worktree, self.repo, self.scratch, self.profile_path,
                              sp.APPLE_UDID, self.env(), runner=fake, home=self.home)
        self.assertIn("cannot prove it confines writes", str(ctx.exception))
        self.assertEqual(fake.calls, [])

    def test_probe_runs_inside_the_very_profile_that_will_wrap_the_commands(self):
        result, fake = self.probe()
        text = self.profile()
        stored = Path(result["profile_path"]).read_text(encoding="utf-8")
        self.assertEqual(stored, text)
        self.assertEqual(result["profile_sha256"], apple.profile_sha256(text))
        self.assertEqual(result["profile_bytes"], len(text.encode("utf-8")))
        self.assertNotIn(str(self.worktree), str(result["profile_path"]))
        self.assertTrue(Path(result["profile_path"]).is_file())
        self.assertEqual(oct(os.stat(str(self.profile_path)).st_mode & 0o777), "0o600")
        self.assertTrue(fake.calls)
        for call in fake.calls:
            self.assertEqual(call["argv"][0], apple.SANDBOX_EXEC)
            self.assertEqual(call["argv"][1:3], ["-f", str(result["profile_path"])])
        self.assertEqual(fake.exec_calls, 0)

    def test_profile_inside_the_worktree_is_refused_before_anything_runs(self):
        fake = sp.FakeRun()
        with self.assertRaises(apple.ApplePolicyError) as ctx:
            apple.probe_apple(self.worktree, self.repo, self.scratch,
                              self.worktree / apple.PROFILE_NAME, sp.APPLE_UDID,
                              self.env(), runner=fake, home=self.home)
        self.assertIn("worker-writable worktree", str(ctx.exception))
        self.assertEqual(fake.calls, [])

    def test_missing_xcode_or_simctl_blocks_with_the_real_failure(self):
        result, _ = self.probe({"xcode_fail": True})
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], ["xcodebuild-version"])
        self.assertIn("no developer directory", result["detail"])
        result, _ = self.probe({"simctl_fail": True})
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], ["simctl-available-devices"])
        self.assertIn("unable to find utility simctl", result["detail"])

    def test_device_list_is_verified_not_assumed(self):
        result, _ = self.probe({"devices": []})
        self.assertFalse(result["available"])
        self.assertIn("no available tvos simulator", result["detail"])
        result, _ = self.probe({"simctl": sp.simctl_json(runtime=sp.IOS_RUNTIME)})
        self.assertFalse(result["available"])
        self.assertIn("no available tvos simulator", result["detail"])
        result, _ = self.probe({"devices": [{"udid": sp.OTHER_UDID, "name": "Other",
                                             "isAvailable": True}]})
        self.assertFalse(result["available"])
        self.assertIn("is not an available tvos device", result["detail"])
        self.assertFalse(result["devices"]["selected_present"])

    def test_availability_comes_from_the_real_simctl_flag(self):
        # simctl spells the flag isAvailable; false must not be read as available, and neither
        # must a missing flag or the snake_case guess that the real output never contains.
        result, _ = self.probe({"devices": [{"udid": sp.APPLE_UDID, "name": "Apple TV 4K 3",
                                             "isAvailable": False}]})
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], ["simctl-available-devices"])
        self.assertIn("no available tvos simulator", result["detail"])
        self.assertEqual(result["devices"]["available_count"], 0)
        self.assertFalse(result["devices"]["selected_present"])
        result, _ = self.probe({"devices": [
            {"udid": sp.APPLE_UDID, "name": "Apple TV 4K 3", "isAvailable": False},
            {"udid": sp.OTHER_UDID, "name": "Apple TV 4K", "isAvailable": True}]})
        self.assertFalse(result["available"])
        self.assertEqual(result["devices"]["available_count"], 1)
        self.assertFalse(result["devices"]["selected_present"])
        self.assertIn("is not an available tvos device", result["detail"])
        for device in ({"udid": sp.APPLE_UDID, "name": "Apple TV 4K 3"},
                       {"udid": sp.APPLE_UDID, "name": "Apple TV 4K 3", "is_available": True},
                       {"udid": sp.APPLE_UDID, "name": "Apple TV 4K 3", "isAvailable": None},
                       {"udid": sp.APPLE_UDID, "name": "Apple TV 4K 3", "isAvailable": ""}):
            with self.subTest(device=str(device)):
                result, _ = self.probe({"devices": [device]})
                self.assertFalse(result["available"])
                self.assertIn("no available tvos simulator", result["detail"])
                self.assertEqual(result["devices"]["available_count"], 0)

    def test_simctl_json_is_read_from_stdout_with_stderr_kept_as_diagnostics(self):
        # stderr carries simulator-set warnings that are not JSON: they must never be appended
        # to stdout, because that alone would turn a healthy device list into a parse failure.
        result, _ = self.probe({"simctl_stderr": "xcrun: warning: { simulator set is busy }"})
        self.assertTrue(result["available"], result["detail"])
        self.assertNotIn("did not return JSON", result["detail"])

        result, _ = self.probe({"simctl": "simctl: no such column",
                                "simctl_stderr": "xcrun: error: unable to find utility simctl"})
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], ["simctl-available-devices"])
        detail = [entry["detail"] for entry in result["checks"]
                  if entry["name"] == "simctl-available-devices"][0]
        self.assertIn("did not return JSON on stdout", detail)
        self.assertIn("simctl: no such column", detail)
        self.assertIn("unable to find utility simctl", detail)

        # JSON that only reached stderr is not evidence either: stdout is the authority.
        result, _ = self.probe({"simctl": "   ", "simctl_stderr": sp.simctl_json()})
        self.assertFalse(result["available"])
        detail = [entry["detail"] for entry in result["checks"]
                  if entry["name"] == "simctl-available-devices"][0]
        self.assertIn("did not return JSON on stdout", detail)
        self.assertIn("(empty stdout)", detail)
        self.assertIn("stderr:", detail)
        self.assertEqual(result["devices"]["available_count"], 0)
        self.assertFalse(result["devices"]["selected_present"])

    def test_writes_that_escape_are_reported_as_not_confined(self):
        result, _ = self.probe({"writes": "allow_all"})
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], ["deny-write-outside-worktree"])
        self.assertIn("outside the bounded writable roots", result["detail"])

    def test_refusal_is_required_not_any_failure(self):
        result, _ = self.probe({"connect": "CONNECTED"})
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], ["deny-loopback-tcp"])
        self.assertIn("127.0.0.1", result["detail"])
        result, _ = self.probe({"connect": "REFUSED"})
        self.assertFalse(result["available"])
        self.assertIn("closed port", result["detail"])
        result, _ = self.probe({"connect": "DENIED"})
        self.assertTrue(result["available"])
        detail = [entry["detail"] for entry in result["checks"]
                  if entry["name"] == "deny-loopback-tcp"][0]
        self.assertIn("refused by the sandbox", detail)

    def test_permission_refusal_everywhere_is_a_honest_failure(self):
        result, fake = self.probe({"deny_everything": True})
        self.assertFalse(result["available"])
        self.assertEqual(sorted(result["failed_checks"]), sorted(CHECK_NAMES))
        self.assertTrue(result["detail"].strip())
        self.assertTrue(all(call["argv"][0] == apple.SANDBOX_EXEC for call in fake.calls))

    def test_missing_sandbox_exec_never_falls_back_to_bare_commands(self):
        result, fake = self.probe({"no_backend": True})
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], CHECK_NAMES)
        self.assertIn("No such file or directory", result["detail"])
        self.assertEqual([call["argv"][0] for call in fake.calls], [apple.SANDBOX_EXEC] * len(
            fake.calls))

        def missing(argv, **kwargs):
            raise OSError(2, "No such file or directory")

        result, _ = self.probe(runner=missing)
        self.assertFalse(result["available"])
        self.assertEqual(result["failed_checks"], CHECK_NAMES)
        self.assertIn("cannot invoke " + apple.SANDBOX_EXEC, result["detail"])

    def test_exhausted_budget_records_that_the_probe_did_not_run(self):
        fake = sp.FakeRun()
        result, used = self.probe(runner=fake, deadline=lw.Deadline(2.0))
        self.assertFalse(result["available"])
        self.assertEqual(used.calls, [])
        self.assertEqual(result["failed_checks"], CHECK_NAMES)
        self.assertIn("was not run", result["detail"])

    def test_timeouts_are_reported_as_failures(self):
        def slow(argv, **kwargs):
            return sp.result(argv, -9, "", "killed", timed_out=True)

        result, _ = self.probe(runner=slow)
        self.assertFalse(result["available"])
        self.assertIn("timed out", result["detail"])

    def test_probe_detail_is_redacted(self):
        def leaking(argv, **kwargs):
            return sp.result(argv, 1, "", "token " + sp.SECRET)

        result, _ = self.probe(runner=leaking, secrets=[sp.SECRET])
        self.assertFalse(result["available"])
        self.assertNotIn(sp.SECRET, json.dumps(result))
        self.assertIn("[REDACTED]", result["detail"])

    def test_negative_checks_run_the_pinned_interpreter_wrapped(self):
        result, fake = self.probe(python_executable="/custom/python3")
        loopback = [entry for entry in result["checks"] if entry["name"] == "deny-loopback-tcp"]
        self.assertEqual(loopback[0]["argv"][:4], [apple.SANDBOX_EXEC, "-f",
                                                   str(self.profile_path), "/custom/python3"])
        self.assertNotIn("/bin/sh", json.dumps([entry["argv"] for entry in result["checks"]]))

    def test_unsafe_setup_raises_instead_of_returning_a_passing_probe(self):
        for kwargs in ({"udid": "not-a-uuid"}, {"home": self.root / "no-home"},
                       {"job_temp_is_home": True}):
            with self.subTest(case=str(kwargs)):
                home = kwargs.get("home", self.home)
                udid = kwargs.get("udid", sp.APPLE_UDID)
                scratch = self.home if kwargs.get("job_temp_is_home") else self.scratch
                with self.assertRaises(apple.ApplePolicyError):
                    apple.probe_apple(self.worktree, self.repo, scratch, self.profile_path,
                                      udid, self.env(), runner=sp.FakeRun(), home=home)


if __name__ == "__main__":
    unittest.main()

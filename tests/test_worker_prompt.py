"""Worker prompt: what the model is told about the shared job budget.

Two layers are proved here. A pure render must state the total budget and only state a
remainder it actually measured from a live ``Deadline``; a real job must hand the *same*
Deadline to the prompt builder on the first attempt and on the retry, so the remainder the
worker reads is the shared one, never a reset copy. The job tests reuse ``support.FakeRun``,
so nothing here calls a model, a server or a native tool.
"""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw
FIX = "def add(a, b):\n    return a + b\n"
TOTAL = 60
TOTAL_LINE = re.compile(r"- Total for this whole task: (\d+)s")
REMAINING_LINE = re.compile(r"- Remaining now: (\d+\.\d)s of that total")
DEADLINE_STAMP = re.compile(r"hard deadline (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) UTC")


def frozen(total_seconds, left):
    """A Deadline whose remainder is exactly what the test set: no clock, no sleep."""

    class Fixed(lw.Deadline):
        def remaining(self):
            return left

    return Fixed(total_seconds)


class SteppingDeadline(lw.Deadline):
    """The real single budget, but every reading of it costs one second.

    Nothing is mocked away: enforcement still sees one shared object with the same total. The
    drift only makes it observable in the prompt text that a retry read the budget *after* the
    first attempt and its verification, instead of re-deriving the total.
    """

    def __init__(self, total_seconds):
        super().__init__(total_seconds)
        self.readings = 0

    def remaining(self):
        self.readings += 1
        return max(0.0, super().remaining() - float(self.readings - 1))


def rendered(prompt):
    total = TOTAL_LINE.search(prompt)
    left = REMAINING_LINE.search(prompt)
    stamp = DEADLINE_STAMP.search(prompt)
    return (float(total.group(1)) if total else None,
            float(left.group(1)) if left else None,
            stamp.group(1) if stamp else None)


class BudgetRender(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        (self.root / "repo").mkdir()                    # the contract only needs it to exist
        self.task = lw.Task.from_payload(sp.task_payload(self.root / "repo",
                                                         timeout_seconds=TOTAL))
        self.worktree = self.root / "worktree"

    def test_total_is_always_labelled_as_a_total(self):
        for prompt in (lw.build_prompt(self.task, self.worktree),
                       lw.build_prompt(self.task, self.worktree, deadline=frozen(TOTAL, 7.5))):
            total, _, _ = rendered(prompt)
            self.assertEqual(total, float(TOTAL))
            self.assertIn("It never resets between attempts.", prompt)
            self.assertIn("the launcher's independent verification", prompt)

    def test_render_without_a_deadline_claims_no_remainder(self):
        prompt = lw.build_prompt(self.task, self.worktree)
        self.assertIsNone(REMAINING_LINE.search(prompt))
        self.assertIsNone(DEADLINE_STAMP.search(prompt))
        self.assertIn("- Remaining now: not measured by this render", prompt)
        self.assertEqual(prompt.count(f"{TOTAL}s"), 1)

    def test_measured_remainder_and_absolute_deadline_are_rendered(self):
        total, left, stamp = rendered(
            lw.build_prompt(self.task, self.worktree, deadline=frozen(TOTAL, 42.5)))
        self.assertEqual((total, left), (float(TOTAL), 42.5))
        self.assertTrue(stamp)

    def test_low_remainder_is_stated_as_low(self):
        _, left, _ = rendered(
            lw.build_prompt(self.task, self.worktree, deadline=frozen(TOTAL, 0.4)))
        self.assertEqual(left, 0.4)

    def test_expired_budget_never_renders_a_negative_remainder(self):
        _, left, _ = rendered(
            lw.build_prompt(self.task, self.worktree, deadline=frozen(TOTAL, -25.0)))
        self.assertEqual(left, 0.0)

    def test_apple_note_and_budget_coexist(self):
        prompt = lw.build_prompt(self.task, self.worktree, apple_udid=sp.APPLE_UDID,
                                 deadline=frozen(TOTAL, 42.5))
        self.assertIn("Native Apple verification", prompt)
        self.assertIn(sp.APPLE_UDID, prompt)
        self.assertEqual(rendered(prompt)[1], 42.5)
        self.assertIn(f"Worktree: {self.worktree}", prompt)

    def test_feedback_and_budget_coexist(self):
        prompt = lw.build_prompt(self.task, self.worktree, feedback="- fix it",
                                 deadline=frozen(TOTAL, 9.0))
        self.assertIn("Previous attempt feedback", prompt)
        self.assertEqual(rendered(prompt)[1], 9.0)


class ScopedResumeGuidance(unittest.TestCase):
    """The actual worker skips host skill discovery, so the header has to carry this itself."""

    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        (self.root / "repo").mkdir()
        self.task = lw.Task.from_payload(sp.task_payload(self.root / "repo"))
        self.worktree = self.root / "worktree"

    def test_header_carries_the_scoped_resume_rule(self):
        prompts = [lw.build_prompt(self.task, self.worktree),
                   lw.build_prompt(self.task, self.worktree, apple_udid=sp.APPLE_UDID,
                                   deadline=frozen(TOTAL, 5.0))]
        for prompt in prompts:
            self.assertIn("targeted reads", prompt)
            self.assertIn("do not repeat whole-repository discovery", prompt)
            self.assertIn("If your context is compacted, resume the current task from your "
                          "own git diff", prompt)


class BudgetReachesTheWorker(unittest.TestCase):
    def setUp(self):
        self.root = sp.tmpdir()
        self.addCleanup(sp.cleanup, self.root)
        self.repo = sp.make_repo(self.root)

    @staticmethod
    def fix(cwd, attempt=1):
        Path(cwd, "add.py").write_text(FIX)

    def run_job(self, options, fake):
        prompts, deadlines = [], []
        real = lw.build_prompt

        def spy(task, worktree, feedback=None, apple_udid=None, deadline=None):
            text = real(task, worktree, feedback=feedback, apple_udid=apple_udid,
                        deadline=deadline)
            prompts.append(text)
            deadlines.append(deadline)
            return text

        with mock.patch.object(lw, "build_prompt", spy), \
                mock.patch.object(lw, "Deadline", SteppingDeadline), \
                mock.patch.object(lw, "run_bounded", fake):
            report = lw.run_job(options)
        return report, prompts, deadlines

    def test_first_attempt_prompt_gets_the_live_deadline(self):
        fake = sp.FakeRun(worker_edit=self.fix)
        report, prompts, deadlines = self.run_job(sp.job_options(self.root / "one", self.repo),
                                                 fake)
        self.assertEqual(report["status"], "ok", report.get("failure_reason"))
        self.assertEqual(len(prompts), 1)
        self.assertIsInstance(deadlines[0], SteppingDeadline)
        total, left, stamp = rendered(prompts[0])
        self.assertEqual(total, float(TOTAL))
        self.assertTrue(40.0 < left < float(TOTAL), left)
        self.assertTrue(stamp)
        self.assertEqual(fake.calls_of("exec")[0]["stdin"], prompts[0])

    def test_initial_and_retry_prompts_carry_the_shared_remaining_budget(self):
        fake = sp.FakeRun(test_rc=1, worker_edit=self.fix)
        report, prompts, deadlines = self.run_job(
            sp.job_options(self.root / "two", self.repo, max_attempts=2), fake)

        self.assertEqual(report["status"], "tests_failed", report.get("failure_reason"))
        self.assertEqual(len(prompts), 2)
        self.assertIs(deadlines[0], deadlines[1])          # one shared budget, never reset
        self.assertEqual([rendered(text)[0] for text in prompts], [float(TOTAL)] * 2)
        first, second = rendered(prompts[0])[1], rendered(prompts[1])[1]
        self.assertGreater(second, 0.0)
        self.assertLess(second, first)                     # the retry spent budget
        self.assertLess(first, float(TOTAL))               # a remainder, not the total relabelled
        for prompt, call in zip(prompts, fake.calls_of("exec")):
            self.assertTrue(DEADLINE_STAMP.search(prompt))
            self.assertEqual(call["stdin"], prompt)        # what was built is what was sent
        self.assertNotIn("Previous attempt feedback", prompts[0])
        self.assertIn("Previous attempt feedback", prompts[1])


if __name__ == "__main__":
    unittest.main()

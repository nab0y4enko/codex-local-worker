"""Worker JSONL evidence: model routing, tool counts, tokens and unsupported-tool detection."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support as sp  # noqa: E402

lw = sp.lw

PHRASE = "unsupported tool"
CLEAN = {"model": lw.EXPECTED_ALIAS, "type": "token_count",
         "usage": {"input_tokens": 11, "output_tokens": 7}}
READER_SOURCE = ('UNSUPPORTED_RE = re.compile(r"unsupported (?:tool|function)"\n'
                 '        if evidence["unsupported_tools"]:\n')


def stream(*events):
    """Render a worker stream: dicts become JSON events, strings stay raw lines."""
    return "".join(f"{event if isinstance(event, str) else json.dumps(event)}\n"
                   for event in events)


def evidence(*events):
    return lw.parse_worker_log(stream(*events))


def reading_stream(*extra_events):
    """A successful tool run that reads launcher source quoting the forbidden phrase."""
    return evidence(
        CLEAN,
        {"type": "item.completed",
         "item": {"type": "command_execution", "status": "completed", "exit_code": 0,
                  "command": ["rg", "-n", PHRASE, "src/local_worker.py"],
                  "aggregated_output": READER_SOURCE}},
        {"type": "item.completed",
         "item": {"type": "file_read", "path": "src/local_worker.py",
                  "content": "the worker reported an unsupported tool"}},
        *extra_events)


class UnsupportedToolEvidence(unittest.TestCase):
    def test_source_text_that_quotes_the_phrase_is_not_a_failure(self):
        parsed = reading_stream()
        self.assertFalse(parsed["unsupported_tools"])
        self.assertEqual(parsed["tool_call_events"], 1)
        self.assertEqual(parsed["observed_models"], [lw.EXPECTED_ALIAS])
        self.assertEqual(parsed["token_counts"], {"input_tokens": 11, "output_tokens": 7})

    def test_failing_command_output_that_quotes_the_phrase_is_not_a_failure(self):
        parsed = reading_stream(
            {"type": "item.completed",
             "item": {"type": "command_execution", "status": "failed", "exit_code": 1,
                      "aggregated_output": "AssertionError: unsupported tool fixture\n"}})
        self.assertFalse(parsed["unsupported_tools"])

    def test_reasoning_and_agent_messages_that_quote_the_phrase_are_not_failures(self):
        parsed = reading_stream(
            {"type": "item.completed",
             "item": {"type": "reasoning",
                      "summary": [{"type": "summary_text",
                                   "text": "the task mentions an unsupported tool"}],
                      "content": ["unsupported function is not available"]}},
            {"type": "event_msg",
             "item": {"type": "agent_message",
                      "message": "Reader output quotes unsupported tool; that is data"}})
        self.assertFalse(parsed["unsupported_tools"])

    def test_raw_stream_text_that_quotes_the_phrase_is_not_a_failure(self):
        raw_command = '{"type": "item.completed", "item": {"type": "command_execution", ' \
                      '"command": ["rg", "unsupported tool"]}},'
        parsed = lw.parse_worker_log(stream(CLEAN, "note: source contains unsupported tool text",
                                            raw_command))
        self.assertFalse(parsed["unsupported_tools"])
        self.assertEqual(parsed["observed_models"], [lw.EXPECTED_ALIAS])

    def test_structured_tool_error_events_fail_closed(self):
        variants = (
            {"type": "error", "message": "Unsupported tool 'web_search' was requested"},
            {"type": "turn.failed",
             "error": {"message": "Response error: unsupported tool call 'web_search'"}},
            {"type": "response.failed",
             "response": {"error": {"type": "invalid_request",
                                    "message": "tool `web_search` is not supported"}}})
        for index, event in enumerate(variants):
            with self.subTest(variant=index):
                parsed = evidence(CLEAN, event)
                self.assertTrue(parsed["unsupported_tools"])
                self.assertEqual(parsed["observed_models"], [lw.EXPECTED_ALIAS])

    def test_failed_tool_result_errors_fail_closed(self):
        variants = (
            {"type": "item.completed",
             "item": {"type": "function_call_output", "call_id": "c1", "is_error": True,
                      "output": "Error: unsupported tool 'web_search' is not available"}},
            {"type": "item.completed",
             "item": {"type": "tool_result", "tool": "web_search", "status": "failed",
                      "error": "unsupported tool 'web_search' rejected"}},
            {"type": "item.completed",
             "item": {"type": "tool_result", "ok": False,
                      "result": {"message": "unsupported function 'search'"}}})
        for index, event in enumerate(variants):
            with self.subTest(variant=index):
                self.assertTrue(evidence(CLEAN, event)["unsupported_tools"])

    def test_raw_cli_diagnostic_lines_fail_closed(self):
        lines = ("2026-10-23T10:00:00Z ERROR codex_core::codex: unsupported tool 'web_search'",
                 "Error: unsupported tool 'web_search' is not supported by this model",
                 "warning: tool `web_search` is not available for model=qwen")
        for line in lines:
            with self.subTest(line=line):
                parsed = lw.parse_worker_log(stream(CLEAN, line))
                self.assertTrue(parsed["unsupported_tools"])
                self.assertEqual(parsed["observed_models"], [lw.EXPECTED_ALIAS])

    def test_malformed_json_never_breaks_evidence_and_fails_closed_on_errors(self):
        truncated_error = '{"type":"error","message":"unsupported tool \'web_search\''
        parsed = lw.parse_worker_log(stream(CLEAN, truncated_error))
        self.assertEqual(parsed["observed_models"], [lw.EXPECTED_ALIAS])
        self.assertEqual(parsed["token_counts"], {"input_tokens": 11, "output_tokens": 7})
        self.assertTrue(parsed["unsupported_tools"])

        truncated_command = '{"type":"item.completed","item":{"type":"command_execution",' \
                            '"command":["rg","unsupported tool"],'
        plain_text = "not json at all: unsupported tool appears here"
        for junk in (truncated_command, plain_text):
            with self.subTest(junk=junk):
                parsed = lw.parse_worker_log(stream(CLEAN, junk))
                self.assertFalse(parsed["unsupported_tools"])
                self.assertEqual(parsed["token_counts"], {"input_tokens": 11, "output_tokens": 7})

    def test_routing_and_tool_counting_are_unchanged_by_the_parser_fix(self):
        parsed = reading_stream({"type": "function_call", "name": "read_file"})
        self.assertEqual(parsed["tool_call_events"], 2)
        self.assertEqual(parsed["token_counts"], {"input_tokens": 11, "output_tokens": 7})
        self.assertEqual(lw.model_routing(parsed, lw.EXPECTED_ALIAS), "ok")
        self.assertEqual(lw.model_routing({"observed_models": []}, lw.EXPECTED_ALIAS), "absent")
        self.assertEqual(lw.model_routing({"observed_models": ["gpt-5"]}, lw.EXPECTED_ALIAS),
                         "mismatch")


if __name__ == "__main__":
    unittest.main()

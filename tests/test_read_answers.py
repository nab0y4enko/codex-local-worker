"""Schema-subset and source citation checks for read-job results."""
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401 - adds src/ to sys.path like the rest of the suite
import read_answers


class ReadAnswerTests(unittest.TestCase):
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp(prefix="read-answers-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.wt)

    def test_citation_must_quote_the_cited_lines(self):
        (self.wt / "a.swift").write_text("let x = 1\r\n\tlet  y = 2\n", encoding="utf-8")
        result = read_answers.verify_citations(self.wt, [
            {"path": "a.swift", "line_start": 2, "line_end": 2, "quote": "let y = 2"},
            {"path": "a.swift", "line_start": 9, "line_end": 9, "quote": "nope"},
            {"path": "../etc/passwd", "line_start": 1, "line_end": 1, "quote": "root"}])
        self.assertEqual((result["total"], result["verified"]), (3, 1))

    def test_schema_subset_validation(self):
        schema = {"type": "object", "required": ["controls"], "additionalProperties": False,
                  "properties": {"controls": {"type": "array",
                                               "items": {"type": "string"}, "maxItems": 2}}}
        self.assertEqual(read_answers.validate({"controls": ["a"]}, schema), [])
        self.assertTrue(read_answers.validate({"controls": ["a", "b", "c"], "x": 1}, schema))

    def test_final_message_json_extraction(self):
        body = '{"answer": 1, "citations": []}'
        self.assertEqual(read_answers.extract_json_text(body), body)
        self.assertEqual(read_answers.extract_json_text("```json\n" + body + "\n```"), body)
        self.assertEqual(read_answers.extract_json_text("Done.\n```json\n" + body + "\n```"), body)
        self.assertEqual(read_answers.extract_json_text(
            "```json\n{\"a\": 1}\n```\ntext\n```json\n" + body + "\n```"), body)
        for bad in ("no json here", "```python\nprint(1)\n```", "", "```json\n{oops\n```"):
            self.assertEqual(read_answers.extract_json_text(bad), bad)

    def test_envelope_schema_requires_answer_and_citations(self):
        schema = read_answers.envelope_schema({"type": "string"})
        self.assertEqual(schema["required"], ["answer", "citations"])
        self.assertEqual(schema["properties"]["answer"], {"type": "string"})
        self.assertEqual(schema["properties"]["citations"]["minItems"], 1)
        self.assertTrue(read_answers.validate({"answer": "ok", "citations": []}, schema))


if __name__ == "__main__":
    unittest.main()

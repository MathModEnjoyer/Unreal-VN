import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_client import DeepSeekClient, DEEPSEEK_MODEL


class DeepSeekModelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        log_dir = patch('deepseek_logging.default_log_dir', return_value=Path(directory.name))
        log_dir.start()
        self.addCleanup(log_dir.stop)

    def test_only_flash_is_sent_to_api(self):
        with patch("deepseek_client.httpx.Client") as http:
            response = http.return_value.__enter__.return_value.post.return_value
            response.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
            client = DeepSeekClient(api_key="test-key")
            self.assertEqual(client._call([{"role": "user", "content": "test"}], temperature=0.2), "{}")
            post = http.return_value.__enter__.return_value.post
            self.assertEqual(DEEPSEEK_MODEL, "deepseek-flash")
            self.assertEqual(post.call_args.kwargs["json"]["model"], DEEPSEEK_MODEL)
            self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", post.call_args.kwargs["json"])
            self.assertEqual(post.call_args.args, ("/chat/completions",))

    def test_disabled_reasoning_in_json_and_plain_text_requests(self):
        for json_mode in (True, False):
            with self.subTest(json_mode=json_mode), patch("deepseek_client.httpx.Client") as http:
                response = http.return_value.__enter__.return_value.post.return_value
                response.json.return_value = {"choices": [{"message": {"content": "answer"}}]}
                client = DeepSeekClient(api_key="test-key")
                self.assertEqual(client._call(
                    [{"role": "user", "content": "test"}], temperature=0.7, json_mode=json_mode,
                ), "answer")
                payload = http.return_value.__enter__.return_value.post.call_args.kwargs["json"]
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertEqual(payload["temperature"], 0.7)
                self.assertNotIn("reasoning_effort", payload)
                if json_mode:
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                else:
                    self.assertNotIn("response_format", payload)

    def test_every_operation_disables_reasoning(self):
        operations = ("review_segment_events", "generate_segment_events", "generate_segment_prose",
                      "generate_characters", "generate_story_plan", "validate_custom_choice",
                      "generate_custom_choice_events", "generate_clarifying_questions",
                      "generate_genre_tone_fit", "generate_premise_suggestions", "unspecified", "future_operation")
        for operation in operations:
            with self.subTest(operation=operation), patch("deepseek_client.httpx.Client") as http:
                response = http.return_value.__enter__.return_value.post.return_value
                response.json.return_value = {"choices": [{"message": {
                    "content": "{}", "reasoning_content": "private reasoning"}}]}
                messages = [{"role": "system", "content": "stable prefix"},
                            {"role": "user", "content": "history and task"}]
                client = DeepSeekClient(api_key="test-key")
                self.assertEqual(client._call(messages, temperature=0.2, operation=operation), "{}")
                payload = http.return_value.__enter__.return_value.post.call_args.kwargs["json"]
                self.assertEqual(payload["messages"], messages)
                self.assertEqual(payload["model"], "deepseek-flash")
                self.assertEqual(payload["thinking"], {"type": "disabled"})
                self.assertNotIn("reasoning_effort", payload)


if __name__ == "__main__":
    unittest.main()

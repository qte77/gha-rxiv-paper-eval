"""Tests for eval_papers.py.

Run locally:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import eval_papers


def _fake_response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


class GhModelsRestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {"GH_TOKEN": "fake-token"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_returns_assistant_content_from_response(self) -> None:
        resp = _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "YES"}}]}
        )
        with patch("urllib.request.urlopen", return_value=resp):
            result = eval_papers.gh_models_rest(
                model="openai/gpt-4o-mini",
                system_prompt="reply YES or NO",
                user_prompt="Title: foo",
                max_tokens=4,
            )
        self.assertEqual(result, "YES")

    def test_posts_json_to_github_models_endpoint_with_bearer_auth(self) -> None:
        resp = _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "NO"}}]}
        )
        with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
            eval_papers.gh_models_rest(
                model="openai/gpt-4o-mini",
                system_prompt="sys",
                user_prompt="user",
                max_tokens=4,
            )
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://models.github.ai/inference/chat/completions"
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-token")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "openai/gpt-4o-mini")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 4)
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "user"},
            ],
        )

    def test_http_error_raises_runtime_error(self) -> None:
        http_err = urllib.error.HTTPError(
            url=eval_papers.GITHUB_MODELS_URL,
            code=429,
            msg="Too Many Requests",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"message": "rate limited"}'),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            with self.assertRaises(RuntimeError) as ctx:
                eval_papers.gh_models_rest(
                    model="m", system_prompt="s", user_prompt="u", max_tokens=4
                )
        self.assertIn("429", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

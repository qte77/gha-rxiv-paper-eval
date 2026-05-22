"""Tests for eval_papers.py.

Run locally:
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
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

    def test_models_url_override_via_env(self) -> None:
        resp = _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "YES"}}]}
        )
        custom = "https://api.openai.example/v1/chat/completions"
        with (
            patch.dict(os.environ, {"RXIV_EVAL_MODELS_URL": custom}),
            patch("urllib.request.urlopen", return_value=resp) as mock_urlopen,
        ):
            eval_papers.gh_models_rest(
                model="gpt-4o", system_prompt="s", user_prompt="u", max_tokens=4
            )
        self.assertEqual(mock_urlopen.call_args.args[0].full_url, custom)

    def test_models_url_refuses_non_https(self) -> None:
        with (
            patch.dict(os.environ, {"RXIV_EVAL_MODELS_URL": "http://insecure.example/v1"}),
            self.assertRaises(ValueError) as ctx,
        ):
            eval_papers.gh_models_rest(
                model="m", system_prompt="s", user_prompt="u", max_tokens=4
            )
        self.assertIn("non-https", str(ctx.exception))

    def test_http_error_raises_runtime_error(self) -> None:
        http_err = urllib.error.HTTPError(
            url=eval_papers.GITHUB_MODELS_URL,
            code=429,
            msg="Too Many Requests",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"message": "rate limited"}'),
        )
        with (
            patch("urllib.request.urlopen", side_effect=http_err),
            self.assertRaises(RuntimeError) as ctx,
        ):
            eval_papers.gh_models_rest(
                model="m", system_prompt="s", user_prompt="u", max_tokens=4
            )
        self.assertIn("429", str(ctx.exception))


class IsRelevantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {"GH_TOKEN": "fake-token"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _paper(self, **overrides) -> eval_papers.Paper:
        defaults = dict(
            date="2026-05-04",
            iso_week="19",
            doi="10.64898/2026.05.01.000001",
            version="1",
            category="biophysics",
            title="Kinetics of process X under varying conditions",
            authors="Doe, J.",
        )
        defaults.update(overrides)
        return eval_papers.Paper(**defaults)

    def test_user_prompt_includes_title_category_and_abstract(self) -> None:
        paper = self._paper()
        abstract = "We characterize the dynamics of process X across temperatures."
        resp = _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "YES"}}]}
        )
        with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
            result = eval_papers.is_relevant(
                paper, abstract, model="openai/gpt-4o-mini", system_prompt="sys"
            )
        self.assertTrue(result)
        body = json.loads(mock_urlopen.call_args.args[0].data)
        user_msg = next(m["content"] for m in body["messages"] if m["role"] == "user")
        self.assertIn(paper.title, user_msg)
        self.assertIn(paper.category, user_msg)
        self.assertIn(abstract, user_msg)


def _http_error(code: int) -> urllib.error.HTTPError:
    """Helper: build an HTTPError with the given status code."""
    return urllib.error.HTTPError(
        url=eval_papers.GITHUB_MODELS_URL,
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(f'{{"message": "error {code}"}}'.encode()),
    )


def _yes_response() -> io.BytesIO:
    return _fake_response(
        {"choices": [{"message": {"role": "assistant", "content": "YES"}}]}
    )


def _no_response() -> io.BytesIO:
    return _fake_response(
        {"choices": [{"message": {"role": "assistant", "content": "NO"}}]}
    )


# ---------------------------------------------------------------------------
# RetryBackoffTests
# ---------------------------------------------------------------------------

class RetryBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "fake-token",
                "RXIV_EVAL_RETRY_BASE_SECS": "0.01",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_retries_on_429_then_succeeds(self) -> None:
        side_effects = [_http_error(429), _http_error(429), _yes_response()]
        sleep_calls: list[float] = []

        with (
            patch("urllib.request.urlopen", side_effect=side_effects) as mock_open,
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = eval_papers.gh_models_rest(
                model="openai/gpt-4o-mini",
                system_prompt="sys",
                user_prompt="user",
                max_tokens=4,
            )

        self.assertEqual(result, "YES")
        self.assertEqual(mock_open.call_count, 3)
        self.assertEqual(len(sleep_calls), 2)
        # Backoff must be positive for each sleep
        for s in sleep_calls:
            self.assertGreater(s, 0)
        # Monotonically non-decreasing (second sleep >= first sleep)
        self.assertGreaterEqual(sleep_calls[1], sleep_calls[0])

    def test_retries_on_5xx(self) -> None:
        side_effects = [_http_error(500), _http_error(502), _yes_response()]
        sleep_calls: list[float] = []

        with (
            patch("urllib.request.urlopen", side_effect=side_effects) as mock_open,
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = eval_papers.gh_models_rest(
                model="openai/gpt-4o-mini",
                system_prompt="sys",
                user_prompt="user",
                max_tokens=4,
            )

        self.assertEqual(result, "YES")
        self.assertEqual(mock_open.call_count, 3)
        self.assertEqual(len(sleep_calls), 2)

    def test_gives_up_after_max_attempts(self) -> None:
        max_attempts = 3
        with patch.dict(os.environ, {"RXIV_EVAL_RETRY_MAX_ATTEMPTS": str(max_attempts)}):
            side_effects = [_http_error(429)] * max_attempts

            with (
                patch("urllib.request.urlopen", side_effect=side_effects) as mock_open,
                patch("time.sleep"),
                self.assertRaises(RuntimeError) as ctx,
            ):
                eval_papers.gh_models_rest(
                    model="openai/gpt-4o-mini",
                    system_prompt="sys",
                    user_prompt="user",
                    max_tokens=4,
                )

        self.assertIn("429", str(ctx.exception))
        self.assertEqual(mock_open.call_count, max_attempts)

    def test_url_error_is_retried(self) -> None:
        side_effects = [
            urllib.error.URLError("transient"),
            urllib.error.URLError("transient"),
            _yes_response(),
        ]
        sleep_calls: list[float] = []

        with (
            patch("urllib.request.urlopen", side_effect=side_effects) as mock_open,
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = eval_papers.gh_models_rest(
                model="openai/gpt-4o-mini",
                system_prompt="sys",
                user_prompt="user",
                max_tokens=4,
            )

        self.assertEqual(result, "YES")
        self.assertEqual(mock_open.call_count, 3)
        self.assertEqual(len(sleep_calls), 2)


# ---------------------------------------------------------------------------
# OfflineStubTests
# ---------------------------------------------------------------------------

class OfflineStubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "fake-token",
                "RXIV_EVAL_OFFLINE": "1",
                "RXIV_EVAL_RETRY_BASE_SECS": "0.01",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_offline_hash_mode_is_deterministic(self) -> None:
        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "hash"}):
            result1 = eval_papers.gh_models_rest(
                model="m", system_prompt="s", user_prompt="same-prompt", max_tokens=4
            )
            result2 = eval_papers.gh_models_rest(
                model="m", system_prompt="s", user_prompt="same-prompt", max_tokens=4
            )
        self.assertEqual(result1, result2)
        self.assertIn(result1, ("YES", "NO"))

    def test_offline_yes_mode_returns_yes(self) -> None:
        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "yes"}), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("urlopen called in offline mode"),
        ):
            result = eval_papers.gh_models_rest(
                model="m", system_prompt="s", user_prompt="anything", max_tokens=4
            )
        self.assertEqual(result, "YES")

    def test_offline_no_mode_returns_no(self) -> None:
        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "no"}), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("urlopen called in offline mode"),
        ):
            result = eval_papers.gh_models_rest(
                model="m", system_prompt="s", user_prompt="anything", max_tokens=4
            )
        self.assertEqual(result, "NO")

    def test_offline_flaky_mode_exercises_retry(self) -> None:
        sleep_calls: list[float] = []
        successes = 0

        with (
            patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "flaky"}),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            for i in range(30):
                try:
                    result = eval_papers.gh_models_rest(
                        model="m",
                        system_prompt="s",
                        user_prompt=f"prompt-{i}",
                        max_tokens=4,
                    )
                    self.assertIn(result, ("YES", "NO"))
                    successes += 1
                except RuntimeError:
                    pass  # exhausted retries on some prompts is acceptable

        # At least some calls should succeed, and retry (sleep) should have fired
        self.assertGreater(successes, 0)
        self.assertGreater(len(sleep_calls), 0)

    def test_offline_skips_urlopen_completely(self) -> None:
        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "hash"}), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("urlopen must not be called in offline mode"),
        ):
            result = eval_papers.gh_models_rest(
                model="m", system_prompt="s", user_prompt="test-prompt", max_tokens=4
            )
        self.assertIn(result, ("YES", "NO"))


# ---------------------------------------------------------------------------
# ExtractFieldsErrorIsCaughtTests
# ---------------------------------------------------------------------------

_FIXTURE_FEED = """\
Date,ISOWeek,DOI,Version,Category,Title,Authors
2026-04-06,15,10.1101/2024.09.07.000001,1,microbiology,Paper one about bacterial enzymes,Smith J.
2026-04-06,15,10.1101/2024.09.07.000002,1,microbiology,Paper two about membrane transporters,Jones A.
"""  # noqa: E501


class ExtractFieldsErrorIsCaughtTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "fake-token",
                "RXIV_EVAL_OFFLINE": "1",
                "RXIV_EVAL_STUB_MODE": "yes",
                "RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "0",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_main_loop_continues_when_extract_fields_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            feed_csv = out / "feed.csv"
            feed_csv.write_text(_FIXTURE_FEED)

            call_count = {"n": 0}

            def fake_extract_fields(abstract, *, model, system_prompt):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("simulated 429")
                return eval_papers.ExtractedFields(summary="ok")

            def fake_fetch_feed(feed_repo, server, year, week, dest):
                # feed.csv already written above; nothing to do
                pass

            def fake_fetch_abstract(server, doi):
                return "some abstract text"

            saved_argv = sys.argv[:]
            try:
                sys.argv = [
                    "eval_papers.py",
                    "--feed-repo", "any/repo",
                    "--topic", "test topic",
                    "--categories", "microbiology",
                    "--max-papers", "5",
                    "--enrich",
                    "--output-dir", str(out),
                ]
                fetch_feed_patch = patch.object(
                    eval_papers, "fetch_feed", side_effect=fake_fetch_feed
                )
                fetch_abstract_patch = patch.object(
                    eval_papers, "fetch_abstract", side_effect=fake_fetch_abstract
                )
                extract_fields_patch = patch.object(
                    eval_papers, "extract_fields", side_effect=fake_extract_fields
                )
                with fetch_feed_patch, fetch_abstract_patch, extract_fields_patch:
                    import io as _io
                    stderr_capture = _io.StringIO()
                    with patch("sys.stderr", stderr_capture):
                        rc = eval_papers.main()
            finally:
                sys.argv = saved_argv

            self.assertEqual(rc, 0)
            self.assertTrue((out / "summary.md").exists())
            # The enrichment loop must not crash; extracts.jsonl should exist
            self.assertTrue((out / "extracts.jsonl").exists())
            # stderr must contain the WARN message for the failed extract
            stderr_output = stderr_capture.getvalue()
            self.assertIn("WARN: extract failed", stderr_output)


# ---------------------------------------------------------------------------
# DoiCacheTests
# ---------------------------------------------------------------------------

class DoiCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "fake-token",
                "RXIV_EVAL_RETRY_BASE_SECS": "0.01",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _paper(self, doi: str = "10.1101/2024.09.07.000001") -> eval_papers.Paper:
        return eval_papers.Paper(
            date="2026-04-06",
            iso_week="15",
            doi=doi,
            version="1",
            category="microbiology",
            title="Test paper about inhibitors",
            authors="Smith, J.",
        )

    def test_cache_hit_skips_urlopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            paper = self._paper()

            # First call — urlopen returns YES
            with patch("urllib.request.urlopen", return_value=_yes_response()) as mock_open:
                result1 = eval_papers.is_relevant(
                    paper,
                    "abstract text",
                    model="openai/gpt-4o-mini",
                    system_prompt="sys",
                    output_dir=out,
                )
            self.assertTrue(result1)
            self.assertEqual(mock_open.call_count, 1)

            # Cache file must exist
            cache_file = eval_papers._cache_path(out, paper.doi)
            self.assertTrue(cache_file.exists(), f"expected cache at {cache_file}")

            # Second call — urlopen must NOT be called
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("urlopen called on cache hit"),
            ) as mock_open2:
                result2 = eval_papers.is_relevant(
                    paper,
                    "abstract text",
                    model="openai/gpt-4o-mini",
                    system_prompt="sys",
                    output_dir=out,
                )
            self.assertEqual(result1, result2)
            self.assertEqual(mock_open2.call_count, 0)

    def test_cache_disabled_by_env(self) -> None:
        with (
            patch.dict(os.environ, {"RXIV_EVAL_NO_CACHE": "1"}),
            tempfile.TemporaryDirectory() as tmpdir,
        ):
            out = pathlib.Path(tmpdir)
            paper = self._paper()

            with patch("urllib.request.urlopen", return_value=_yes_response()):
                eval_papers.is_relevant(
                    paper, "abstract", model="m", system_prompt="sys", output_dir=out
                )

            # With cache disabled, no cache file should be written
            cache_file = eval_papers._cache_path(out, paper.doi)
            self.assertFalse(
                cache_file.exists(),
                "cache file should not exist when RXIV_EVAL_NO_CACHE=1",
            )

            # Second call should hit urlopen again (different response)
            with patch("urllib.request.urlopen", return_value=_no_response()) as mock_open2:
                result2 = eval_papers.is_relevant(
                    paper, "abstract", model="m", system_prompt="sys", output_dir=out
                )
            self.assertFalse(result2)  # got the new NO response
            self.assertEqual(mock_open2.call_count, 1)

    def test_cache_per_doi(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            paper_a = self._paper(doi="10.1101/2024.09.07.000001")
            paper_b = self._paper(doi="10.1101/2024.09.07.000002")

            responses = [_yes_response(), _no_response()]
            with patch("urllib.request.urlopen", side_effect=responses):
                eval_papers.is_relevant(
                    paper_a, "abstract a", model="m", system_prompt="sys", output_dir=out
                )
                eval_papers.is_relevant(
                    paper_b, "abstract b", model="m", system_prompt="sys", output_dir=out
                )

            cache_a = eval_papers._cache_path(out, paper_a.doi)
            cache_b = eval_papers._cache_path(out, paper_b.doi)

            self.assertTrue(cache_a.exists(), "cache for DOI A must exist")
            self.assertTrue(cache_b.exists(), "cache for DOI B must exist")
            self.assertNotEqual(cache_a, cache_b)

            data_a = json.loads(cache_a.read_text())
            data_b = json.loads(cache_b.read_text())
            self.assertNotEqual(data_a, data_b)


# ---------------------------------------------------------------------------
# OfflineEndToEndTests
# ---------------------------------------------------------------------------

_FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "feed-min.csv"


class OfflineEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "fake-token",
                "RXIV_EVAL_OFFLINE": "1",
                "RXIV_EVAL_STUB_MODE": "hash",
                "RXIV_EVAL_RETRY_BASE_SECS": "0.01",
                "RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "0",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_offline_full_run_writes_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)

            def fake_fetch_feed(feed_repo, server, year, week, dest):
                shutil.copy(_FIXTURE_PATH, dest)

            saved_argv = sys.argv[:]
            try:
                sys.argv = [
                    "eval_papers.py",
                    "--feed-repo", "any/repo",
                    "--topic", "test topic for offline run",
                    # microbiology appears 3 times in feed-min.csv (keepers)
                    # bioengineering appears 2 times (rejects for this filter)
                    "--categories", "microbiology",
                    "--max-papers", "5",
                    "--output-dir", str(out),
                ]
                with (
                    patch.object(eval_papers, "fetch_feed", side_effect=fake_fetch_feed),
                    patch(
                        "urllib.request.urlopen",
                        side_effect=AssertionError("urlopen must not fire in offline mode"),
                    ),
                ):
                    rc = eval_papers.main()
            finally:
                sys.argv = saved_argv

            self.assertEqual(rc, 0)

            relevant_csv = out / "relevant.csv"
            self.assertTrue(relevant_csv.exists(), "relevant.csv must be written")

            summary_md = out / "summary.md"
            self.assertTrue(summary_md.exists(), "summary.md must be written")

            # relevant.csv must have a header row (at minimum)
            lines = relevant_csv.read_text().splitlines()
            self.assertGreater(len(lines), 0)
            self.assertIn("DOI", lines[0])


# ---------------------------------------------------------------------------
# FetchAbstractArxivTests
# ---------------------------------------------------------------------------


class FetchAbstractArxivTests(unittest.TestCase):
    _ATOM_PAYLOAD = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<feed xmlns="http://www.w3.org/2005/Atom">'
        b'<entry>'
        b'<id>http://arxiv.org/abs/2406.09418v1</id>'
        b'<title>Some title</title>'
        b'<summary>This is the abstract text.</summary>'
        b'</entry>'
        b'</feed>'
    )

    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("RXIV_EVAL_OFFLINE", None)
        self.addCleanup(self._env.stop)

    def test_returns_summary_text_from_atom(self) -> None:
        resp = io.BytesIO(self._ATOM_PAYLOAD)
        with patch("urllib.request.urlopen", return_value=resp) as mock_open:
            result = eval_papers.fetch_abstract(server="arxiv", doi="2406.09418")
        self.assertEqual(result, "This is the abstract text.")
        called_with = mock_open.call_args.args[0]
        url_str = called_with.full_url if hasattr(called_with, "full_url") else str(called_with)
        self.assertIn("export.arxiv.org", url_str)
        self.assertIn("2406.09418", url_str)

    def test_returns_empty_on_offline(self) -> None:
        with patch.dict(os.environ, {"RXIV_EVAL_OFFLINE": "1"}), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("urlopen must not fire in offline mode"),
        ):
            result = eval_papers.fetch_abstract(server="arxiv", doi="2406.09418")
        self.assertEqual(result, "")

    def test_parse_error_returns_empty_string(self) -> None:
        resp = io.BytesIO(b"not xml at all")
        with patch("urllib.request.urlopen", return_value=resp):
            result = eval_papers.fetch_abstract(server="arxiv", doi="2406.09418")
        self.assertEqual(result, "")

    def test_url_error_returns_empty_string(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("transient"),
        ):
            result = eval_papers.fetch_abstract(server="arxiv", doi="2406.09418")
        self.assertEqual(result, "")

    def test_retries_on_http_429_then_succeeds(self) -> None:
        # arxiv 429 should retry per Settings, not give up immediately.
        success = io.BytesIO(self._ATOM_PAYLOAD)
        side_effects = [_http_error(429), _http_error(429), success]
        with (
            patch.dict(os.environ, {"RXIV_EVAL_RETRY_BASE_SECS": "0.01",
                                    "RXIV_EVAL_ARXIV_REQUEST_DELAY_SECS": "0"}),
            patch("urllib.request.urlopen", side_effect=side_effects),
            patch("time.sleep"),
        ):
            result = eval_papers.fetch_abstract(server="arxiv", doi="2406.09418")
        self.assertEqual(result, "This is the abstract text.")

    def test_sleeps_polite_delay_before_arxiv_fetch(self) -> None:
        resp = io.BytesIO(self._ATOM_PAYLOAD)
        sleep_calls: list[float] = []
        with (
            patch.dict(os.environ, {"RXIV_EVAL_ARXIV_REQUEST_DELAY_SECS": "2.5"}),
            patch("urllib.request.urlopen", return_value=resp),
            patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            eval_papers.fetch_abstract(server="arxiv", doi="2406.09418")
        self.assertIn(2.5, sleep_calls)


# ---------------------------------------------------------------------------
# LoadPapersServerDispatchTests
# ---------------------------------------------------------------------------

_FIXTURE_ARXIV_PATH = pathlib.Path(__file__).parent / "fixtures" / "feed-arxiv-min.csv"


class LoadPapersServerDispatchTests(unittest.TestCase):
    def test_biorxiv_path_unchanged(self) -> None:
        papers = eval_papers.load_papers(_FIXTURE_PATH, server="biorxiv")
        # feed-min.csv has 10 rows
        self.assertEqual(len(papers), 10)
        self.assertTrue(all(p.doi.startswith(("10.1101/", "10.64898/")) for p in papers))

    def test_arxiv_path_uses_arxiv_adapter(self) -> None:
        papers = eval_papers.load_papers(_FIXTURE_ARXIV_PATH, server="arxiv")
        self.assertEqual(len(papers), 3)
        # arxiv IDs survive as the doi field
        self.assertEqual(papers[0].doi, "2406.09418")
        self.assertEqual(papers[0].iso_week, "24")
        # title is unquoted
        self.assertFalse(papers[0].title.startswith("'"))


# ---------------------------------------------------------------------------
# CategoriesWarningWithArxivTests
# ---------------------------------------------------------------------------


class CategoriesWarningWithArxivTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "fake-token",
                "RXIV_EVAL_OFFLINE": "1",
                "RXIV_EVAL_STUB_MODE": "hash",
                "RXIV_EVAL_RETRY_BASE_SECS": "0.01",
                "RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "0",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_main_warns_and_ignores_categories_for_arxiv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)

            def fake_fetch_feed(feed_repo, server, year, week, dest):
                shutil.copy(_FIXTURE_ARXIV_PATH, dest)

            saved_argv = sys.argv[:]
            try:
                sys.argv = [
                    "eval_papers.py",
                    "--feed-repo", "any/repo",
                    "--server", "arxiv",
                    "--topic", "test",
                    "--categories", "cs.LG",  # arxiv CSV has no Category column
                    "--max-papers", "5",
                    "--output-dir", str(out),
                ]
                with patch.object(eval_papers, "fetch_feed", side_effect=fake_fetch_feed):
                    stderr_capture = io.StringIO()
                    with patch("sys.stderr", stderr_capture):
                        rc = eval_papers.main()
            finally:
                sys.argv = saved_argv

            self.assertEqual(rc, 0)
            stderr_output = stderr_capture.getvalue()
            self.assertIn("--categories", stderr_output)
            self.assertIn("arxiv", stderr_output)
            # The 3 arxiv fixture rows must reach the relevance pass — none
            # should be dropped by a phantom category prefilter.
            self.assertIn("Loaded 3 papers", stderr_output)

    def test_main_does_not_warn_when_no_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)

            def fake_fetch_feed(feed_repo, server, year, week, dest):
                shutil.copy(_FIXTURE_ARXIV_PATH, dest)

            saved_argv = sys.argv[:]
            try:
                sys.argv = [
                    "eval_papers.py",
                    "--feed-repo", "any/repo",
                    "--server", "arxiv",
                    "--topic", "test",
                    "--max-papers", "5",
                    "--output-dir", str(out),
                ]
                with patch.object(eval_papers, "fetch_feed", side_effect=fake_fetch_feed):
                    stderr_capture = io.StringIO()
                    with patch("sys.stderr", stderr_capture):
                        rc = eval_papers.main()
            finally:
                sys.argv = saved_argv

            self.assertEqual(rc, 0)
            self.assertNotIn("--categories", stderr_capture.getvalue())


# ---------------------------------------------------------------------------
# DefaultTopicTests
# ---------------------------------------------------------------------------


class DefaultTopicTests(unittest.TestCase):
    def test_default_topic_constant_is_non_empty(self) -> None:
        self.assertTrue(hasattr(eval_papers, "DEFAULT_TOPIC"))
        self.assertTrue(eval_papers.DEFAULT_TOPIC.strip())

    def test_parse_args_supplies_default_topic_when_omitted(self) -> None:
        saved_argv = sys.argv[:]
        try:
            sys.argv = ["eval_papers.py", "--feed-repo", "any/repo"]
            args = eval_papers.parse_args()
        finally:
            sys.argv = saved_argv
        self.assertEqual(args.topic, eval_papers.DEFAULT_TOPIC)


# ---------------------------------------------------------------------------
# PaperUrlTests
# ---------------------------------------------------------------------------


class PaperUrlTests(unittest.TestCase):
    def test_arxiv_uses_arxiv_abs_url(self) -> None:
        self.assertEqual(
            eval_papers._paper_url("arxiv", "2406.09418"),
            "https://arxiv.org/abs/2406.09418",
        )

    def test_biorxiv_uses_doi_resolver(self) -> None:
        self.assertEqual(
            eval_papers._paper_url("biorxiv", "10.1101/2024.09.07.000001"),
            "https://doi.org/10.1101/2024.09.07.000001",
        )

    def test_medrxiv_uses_doi_resolver(self) -> None:
        self.assertEqual(
            eval_papers._paper_url("medrxiv", "10.1101/2024.09.07.000002"),
            "https://doi.org/10.1101/2024.09.07.000002",
        )

    def test_unknown_server_falls_back_to_doi(self) -> None:
        # Defensive: any future server defaults to the doi.org resolver.
        self.assertEqual(
            eval_papers._paper_url("chemrxiv", "10.26434/x"),
            "https://doi.org/10.26434/x",
        )


# ---------------------------------------------------------------------------
# ArtifactNameTests
# ---------------------------------------------------------------------------


class ArtifactNameTests(unittest.TestCase):
    def test_biorxiv_two_digit_week(self) -> None:
        self.assertEqual(
            eval_papers._artifact_name("biorxiv", "2026", "21"),
            "rxiv-eval-biorxiv-2026-w21",
        )

    def test_arxiv_zero_padded_week(self) -> None:
        self.assertEqual(
            eval_papers._artifact_name("arxiv", "2026", "01"),
            "rxiv-eval-arxiv-2026-w01",
        )

    def test_medrxiv(self) -> None:
        self.assertEqual(
            eval_papers._artifact_name("medrxiv", "2025", "53"),
            "rxiv-eval-medrxiv-2025-w53",
        )


# ---------------------------------------------------------------------------
# AppendStepSummaryTests
# ---------------------------------------------------------------------------


class AppendStepSummaryTests(unittest.TestCase):
    """`append_step_summary` mirrors `output/summary.md` into $GITHUB_STEP_SUMMARY.

    The function is the python-side replacement for inline shell that cat'd the
    summary into the workflow's step-summary page. Behavior matrix:
      - env var set + summary.md exists  -> append both summary body + artifact footer
      - env var unset                    -> no-op (no exception)
      - env var set to empty string      -> no-op
    """

    def _write_summary(self, output_dir: pathlib.Path, body: str) -> None:
        (output_dir / "summary.md").write_text(body)

    def test_appends_summary_and_artifact_footer_when_env_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir) / "output"
            out.mkdir()
            self._write_summary(out, "# rxiv eval — biorxiv 2026-W21\n\nbody\n")
            step_summary = pathlib.Path(tmpdir) / "step_summary.md"
            step_summary.write_text("")  # GHA pre-creates this file
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(step_summary)}):
                eval_papers.append_step_summary(out, server="biorxiv", year="2026", week="21")
            content = step_summary.read_text()
            self.assertIn("# rxiv eval — biorxiv 2026-W21", content)
            self.assertIn("body", content)
            self.assertIn("rxiv-eval-biorxiv-2026-w21", content)

    def test_noop_when_env_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir) / "output"
            out.mkdir()
            self._write_summary(out, "anything\n")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
                # Must not raise; must not write anywhere observable.
                eval_papers.append_step_summary(out, server="biorxiv", year="2026", week="21")

    def test_noop_when_env_empty_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir) / "output"
            out.mkdir()
            self._write_summary(out, "anything\n")
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
                eval_papers.append_step_summary(out, server="biorxiv", year="2026", week="21")

    def test_appends_instead_of_overwriting(self) -> None:
        # GHA may have prior step summary content from earlier steps; we must
        # not clobber it.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir) / "output"
            out.mkdir()
            self._write_summary(out, "new content\n")
            step_summary = pathlib.Path(tmpdir) / "step_summary.md"
            step_summary.write_text("pre-existing\n")
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(step_summary)}):
                eval_papers.append_step_summary(out, server="biorxiv", year="2026", week="21")
            content = step_summary.read_text()
            self.assertIn("pre-existing", content)
            self.assertIn("new content", content)


# ---------------------------------------------------------------------------
# WriteWorkflowOutputsTests
# ---------------------------------------------------------------------------


class WriteWorkflowOutputsTests(unittest.TestCase):
    """`write_workflow_outputs` writes a key=value file the YAML caller appends
    to `$GITHUB_OUTPUT`. One line per output, format `key=value\\n`."""

    def test_writes_relevant_count_and_artifact_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            eval_papers.write_workflow_outputs(
                out,
                relevant_count=7,
                artifact_name="rxiv-eval-biorxiv-2026-w21",
            )
            content = (out / ".workflow_outputs").read_text()
            lines = content.splitlines()
            self.assertIn("relevant_count=7", lines)
            self.assertIn("artifact_name=rxiv-eval-biorxiv-2026-w21", lines)

    def test_file_ends_with_newline(self) -> None:
        # GHA's $GITHUB_OUTPUT is parsed line-by-line; a trailing newline keeps
        # the last entry from being merged with the next step's output.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            eval_papers.write_workflow_outputs(
                out, relevant_count=0, artifact_name="rxiv-eval-arxiv-2026-w01"
            )
            content = (out / ".workflow_outputs").read_text()
            self.assertTrue(content.endswith("\n"))


# ---------------------------------------------------------------------------
# RunRelevancePassTests
# ---------------------------------------------------------------------------


class RunRelevancePassTests(unittest.TestCase):
    """`_run_relevance_pass` must count how many LLM calls failed so the
    pipeline can distinguish a true-negative week from a rate-limited week
    (issue #6). Pre-fix it silently dropped failed papers via WARN+continue.
    """

    def setUp(self) -> None:
        # Default 0 so non-throttle tests don't pay the 1.5s/paper inter-call
        # gap. Throttle-specific tests override via their own patch.dict.
        self._env = patch.dict(os.environ, {"RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "0"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _papers(self, n: int) -> list:
        return [
            eval_papers.Paper(
                date="2026-04-06",
                iso_week="15",
                doi=f"10.1101/2024.09.07.{i:06d}",
                version="1",
                category="microbiology",
                title=f"Paper {i}",
                authors="Doe, J.",
            )
            for i in range(n)
        ]

    def test_returns_relevant_list_and_call_failure_count(self) -> None:
        # is_relevant raises on the 2nd and 4th call -> 2 failures, 3 successes.
        papers = self._papers(5)
        call_count = {"n": 0}

        def fake_is_relevant(paper, abstract, *, model, system_prompt, output_dir=None):
            call_count["n"] += 1
            if call_count["n"] in (2, 4):
                raise RuntimeError("simulated 429")
            return True

        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            with (
                patch.object(eval_papers, "fetch_abstract", return_value="abstract"),
                patch.object(eval_papers, "is_relevant", side_effect=fake_is_relevant),
            ):
                relevant, call_failures = eval_papers._run_relevance_pass(
                    papers,
                    server="biorxiv",
                    model="openai/gpt-4o-mini",
                    relevance_prompt="sys",
                    output_dir=out,
                )

        self.assertEqual(len(relevant), 3)
        self.assertEqual(call_failures, 2)

    def test_zero_failures_when_all_calls_succeed(self) -> None:
        papers = self._papers(3)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            with (
                patch.object(eval_papers, "fetch_abstract", return_value="abstract"),
                patch.object(eval_papers, "is_relevant", return_value=True),
            ):
                relevant, call_failures = eval_papers._run_relevance_pass(
                    papers,
                    server="biorxiv",
                    model="openai/gpt-4o-mini",
                    relevance_prompt="sys",
                    output_dir=out,
                )
        self.assertEqual(len(relevant), 3)
        self.assertEqual(call_failures, 0)

    def test_sleeps_between_calls_at_configured_interval(self) -> None:
        # 5 papers -> 4 sleeps (between, not after the last). The interval is
        # tunable via RXIV_EVAL_LLM_CALL_INTERVAL_SECS to fight steady-state
        # rate limits without forking the script.
        papers = self._papers(5)
        sleep_calls: list[float] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            with (
                patch.dict(os.environ, {"RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "0.5"}),
                patch.object(eval_papers, "fetch_abstract", return_value="abstract"),
                patch.object(eval_papers, "is_relevant", return_value=True),
                patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
            ):
                eval_papers._run_relevance_pass(
                    papers,
                    server="biorxiv",
                    model="openai/gpt-4o-mini",
                    relevance_prompt="sys",
                    output_dir=out,
                )
        self.assertEqual(sleep_calls, [0.5, 0.5, 0.5, 0.5])

    def test_no_sleep_with_single_paper(self) -> None:
        # 1 paper -> 0 throttle sleeps (no inter-call gap to bridge).
        papers = self._papers(1)
        sleep_calls: list[float] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            with (
                patch.dict(os.environ, {"RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "9.9"}),
                patch.object(eval_papers, "fetch_abstract", return_value="abstract"),
                patch.object(eval_papers, "is_relevant", return_value=True),
                patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
            ):
                eval_papers._run_relevance_pass(
                    papers,
                    server="biorxiv",
                    model="openai/gpt-4o-mini",
                    relevance_prompt="sys",
                    output_dir=out,
                )
        self.assertEqual(sleep_calls, [])

    def test_throttle_fires_even_on_call_failures(self) -> None:
        # If is_relevant raises, the throttle still sleeps before the next
        # call — keeping steady-state pacing under partial-failure runs.
        papers = self._papers(3)
        sleep_calls: list[float] = []

        def fake_is_relevant(paper, abstract, **kw):
            raise RuntimeError("simulated 429")

        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            with (
                patch.dict(os.environ, {"RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "0.25"}),
                patch.object(eval_papers, "fetch_abstract", return_value="abstract"),
                patch.object(eval_papers, "is_relevant", side_effect=fake_is_relevant),
                patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)),
            ):
                eval_papers._run_relevance_pass(
                    papers,
                    server="biorxiv",
                    model="openai/gpt-4o-mini",
                    relevance_prompt="sys",
                    output_dir=out,
                )
        self.assertEqual(sleep_calls, [0.25, 0.25])


# ---------------------------------------------------------------------------
# WriteSummaryFailureRateTests
# ---------------------------------------------------------------------------


class WriteSummaryFailureRateTests(unittest.TestCase):
    """`write_summary` must surface the LLM call-failure rate so a 100%-rate-
    limited week is visibly distinguishable from a true-negative week (#6).
    """

    def test_emits_call_failure_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            eval_papers.write_summary(
                out,
                server="biorxiv",
                year="2026",
                week="15",
                topic="anything",
                total=531,
                after_prefilter=424,
                relevant=[],
                call_failures=424,
            )
            content = (out / "summary.md").read_text()
            self.assertIn("LLM call failures: 424 / 424 (100 %)", content)

    def test_omits_call_failure_line_when_zero_after_prefilter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            eval_papers.write_summary(
                out,
                server="biorxiv",
                year="2026",
                week="15",
                topic="anything",
                total=0,
                after_prefilter=0,
                relevant=[],
                call_failures=0,
            )
            content = (out / "summary.md").read_text()
            self.assertNotIn("LLM call failures", content)

    def test_partial_failure_rate_rounds_to_nearest_percent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            eval_papers.write_summary(
                out,
                server="biorxiv",
                year="2026",
                week="15",
                topic="anything",
                total=10,
                after_prefilter=10,
                relevant=[],
                call_failures=3,
            )
            content = (out / "summary.md").read_text()
            self.assertIn("LLM call failures: 3 / 10 (30 %)", content)


# ---------------------------------------------------------------------------
# MainExitCodeFailureRateTests
# ---------------------------------------------------------------------------


def _feed_csv(n: int) -> str:
    """Build a `n`-row biorxiv-format CSV fixture inline."""
    header = "Date,ISOWeek,DOI,Version,Category,Title,Authors\n"
    rows = "".join(
        f"2026-04-06,15,10.1101/2024.09.07.{i:06d},1,microbiology,"
        f"Paper {i},Author {i}.\n"
        for i in range(n)
    )
    return header + rows


class MainExitCodeFailureRateTests(unittest.TestCase):
    """When >50% of LLM calls fail (after retries), `main()` must exit non-zero
    so a 100%-rate-limited weekly run cannot pass CI as a true-negative (#6).
    Boundary: exactly 50% still exits 0 (strict greater-than threshold).
    """

    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "fake-token",
                "RXIV_EVAL_OFFLINE": "1",
                "RXIV_EVAL_STUB_MODE": "yes",
                "RXIV_EVAL_LLM_CALL_INTERVAL_SECS": "0",
                "RXIV_EVAL_RETRY_BASE_SECS": "0.01",
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run_main_with_fixture(
        self, num_papers: int, num_failures: int
    ) -> int:
        """Run `main()` against an inline `num_papers`-row feed, with
        `is_relevant` raising on the first `num_failures` papers.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            out = pathlib.Path(tmpdir)
            (out / "feed.csv").write_text(_feed_csv(num_papers))

            call_count = {"n": 0}

            def fake_is_relevant(paper, abstract, **kw):
                call_count["n"] += 1
                if call_count["n"] <= num_failures:
                    raise RuntimeError("simulated 429")
                return True

            def fake_fetch_feed(feed_repo, server, year, week, dest):
                pass  # feed.csv already written above

            saved_argv = sys.argv[:]
            try:
                sys.argv = [
                    "eval_papers.py",
                    "--feed-repo", "any/repo",
                    "--topic", "test topic",
                    "--categories", "microbiology",
                    "--max-papers", "0",
                    "--output-dir", str(out),
                ]
                with (
                    patch.object(eval_papers, "fetch_feed", side_effect=fake_fetch_feed),
                    patch.object(eval_papers, "fetch_abstract", return_value="abstract"),
                    patch.object(eval_papers, "is_relevant", side_effect=fake_is_relevant),
                ):
                    return eval_papers.main()
            finally:
                sys.argv = saved_argv

    def test_returns_nonzero_when_failure_rate_above_threshold(self) -> None:
        # 3/5 = 60% > 50% -> exit 2
        rc = self._run_main_with_fixture(num_papers=5, num_failures=3)
        self.assertEqual(rc, 2)

    def test_returns_zero_at_exactly_fifty_percent(self) -> None:
        # 2/4 = 50% -> NOT > 50% -> exit 0
        rc = self._run_main_with_fixture(num_papers=4, num_failures=2)
        self.assertEqual(rc, 0)

    def test_returns_zero_on_zero_papers(self) -> None:
        # empty feed after prefilter -> must not raise ZeroDivisionError
        rc = self._run_main_with_fixture(num_papers=0, num_failures=0)
        self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# DefaultRelevancePromptTests
# ---------------------------------------------------------------------------


class DefaultRelevancePromptTests(unittest.TestCase):
    """Default relevance prompt must not bias the LLM toward NO on borderline
    methodology papers (#5). The pre-fix wording — "strict", "if and only if",
    "when uncertain, answer NO" — produced false negatives across multiple
    consumer repos. The replacement permits transferable methodology and tips
    borderline toward YES.
    """

    def test_drops_strict_framing(self) -> None:
        self.assertNotIn("strict", eval_papers.DEFAULT_RELEVANCE_PROMPT)

    def test_drops_if_and_only_if(self) -> None:
        self.assertNotIn("if and only if", eval_papers.DEFAULT_RELEVANCE_PROMPT)

    def test_drops_when_uncertain_answer_no(self) -> None:
        prompt_lower = eval_papers.DEFAULT_RELEVANCE_PROMPT.lower()
        self.assertNotIn("when uncertain, answer no", prompt_lower)

    def test_mentions_methodology(self) -> None:
        self.assertIn("methodology", eval_papers.DEFAULT_RELEVANCE_PROMPT.lower())

    def test_tips_borderline_toward_yes(self) -> None:
        prompt_lower = eval_papers.DEFAULT_RELEVANCE_PROMPT.lower()
        # Either "borderline" or "transferable" plus a "yes" lean; the exact
        # phrasing can evolve, but a tiebreaker toward YES must be present.
        self.assertIn("borderline", prompt_lower)
        self.assertIn("yes", prompt_lower)

    def test_preserves_topic_placeholder(self) -> None:
        # `{topic}` is consumed by `.format(topic=args.topic)` at runtime —
        # losing it breaks the user-facing CLI contract.
        self.assertIn("{topic}", eval_papers.DEFAULT_RELEVANCE_PROMPT)

    def test_preserves_yes_or_no_response_contract(self) -> None:
        # `is_relevant` parses the response by looking at the first token; the
        # prompt must still ask for a YES/NO answer.
        self.assertIn("YES or NO", eval_papers.DEFAULT_RELEVANCE_PROMPT)


if __name__ == "__main__":
    unittest.main()

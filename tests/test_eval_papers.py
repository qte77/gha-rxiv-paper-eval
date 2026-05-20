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


class IsRelevantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(os.environ, {"GH_TOKEN": "fake-token"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _paper(self, **overrides) -> "eval_papers.Paper":
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

        with patch("urllib.request.urlopen", side_effect=side_effects) as mock_open:
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
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

        with patch("urllib.request.urlopen", side_effect=side_effects) as mock_open:
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
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

            with patch("urllib.request.urlopen", side_effect=side_effects) as mock_open:
                with patch("time.sleep"):
                    with self.assertRaises(RuntimeError) as ctx:
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

        with patch("urllib.request.urlopen", side_effect=side_effects) as mock_open:
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
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
        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "yes"}):
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("urlopen called in offline mode"),
            ):
                result = eval_papers.gh_models_rest(
                    model="m", system_prompt="s", user_prompt="anything", max_tokens=4
                )
        self.assertEqual(result, "YES")

    def test_offline_no_mode_returns_no(self) -> None:
        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "no"}):
            with patch(
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

        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "flaky"}):
            with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
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
        with patch.dict(os.environ, {"RXIV_EVAL_STUB_MODE": "hash"}):
            with patch(
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
        with patch.dict(os.environ, {"RXIV_EVAL_NO_CACHE": "1"}):
            with tempfile.TemporaryDirectory() as tmpdir:
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
                with patch.object(eval_papers, "fetch_feed", side_effect=fake_fetch_feed):
                    with patch(
                        "urllib.request.urlopen",
                        side_effect=AssertionError("urlopen must not fire in offline mode"),
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
        with patch.dict(os.environ, {"RXIV_EVAL_OFFLINE": "1"}):
            with patch(
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


if __name__ == "__main__":
    unittest.main()

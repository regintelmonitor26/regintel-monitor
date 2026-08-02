import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import (
    GmailNotifier,
    MhlwScraper,
    ProcessedUrlStore,
    RegulatoryMonitor,
    SummarizedTranscript,
    Transcript,
)


class FakeHttpClient:
    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url):
        return self.pages[url]


class MhlwScraperTests(unittest.TestCase):
    def test_extracts_only_exact_minutes_links_and_deduplicates(self):
        index_url = "https://www.mhlw.go.jp/index.html"
        html = """
        <table>
          <tr><td>第10回</td><td><a href="/minutes.html">議事録</a></td></tr>
          <tr><td>第10回</td><td><a href="/minutes.html"> 議事録 </a></td></tr>
          <tr><td>第9回</td><td><a href="/summary.html">議事要旨</a></td></tr>
          <tr><td>第8回</td><td><a href="/files.pdf">資料</a></td></tr>
          <tr><td>第7回</td><td><a href="https://example.com/x">議事録</a></td></tr>
        </table>
        """
        scraper = MhlwScraper(FakeHttpClient({index_url: html}))

        result = scraper.list_transcripts(index_url)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "https://www.mhlw.go.jp/minutes.html")
        self.assertIn("第10回", result[0].title)

    def test_extracts_readable_transcript_body(self):
        url = "https://www.mhlw.go.jp/minutes.html"
        page = """
        <html><body><main><nav>navigation</nav>
          <h1>第10回 議事録</h1>
          <h2>議事</h2><p>{body}</p>
          <script>ignored()</script>
        </main></body></html>
        """.format(body="本文です。" * 50)
        scraper = MhlwScraper(FakeHttpClient({url: page}))

        result = scraper.fetch_transcript(Transcript("fallback", url))

        self.assertEqual(result.title, "第10回 議事録")
        self.assertIn("本文です。", result.body)
        self.assertNotIn("navigation", result.body)
        self.assertNotIn("ignored", result.body)


class ProcessedUrlStoreTests(unittest.TestCase):
    def test_round_trip_uses_sorted_unique_json_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "processed_urls.json"
            store = ProcessedUrlStore(path)

            store.save(["https://b", "https://a", "https://a"])

            self.assertEqual(store.load(), {"https://a", "https://b"})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                ["https://a", "https://b"],
            )


class RegulatoryMonitorTests(unittest.TestCase):
    def test_first_run_saves_all_existing_urls_without_credentials_or_email(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProcessedUrlStore(Path(directory) / "processed_urls.json")
            scraper = Mock()
            scraper.list_transcripts.return_value = [
                Transcript("one", "https://www.mhlw.go.jp/one"),
                Transcript("two", "https://www.mhlw.go.jp/two"),
            ]

            with patch.dict(os.environ, {}, clear=True):
                RegulatoryMonitor(scraper, store).run()

            self.assertEqual(
                store.load(),
                {
                    "https://www.mhlw.go.jp/one",
                    "https://www.mhlw.go.jp/two",
                },
            )
            scraper.fetch_transcript.assert_not_called()

    @patch("monitor.GmailNotifier")
    @patch("monitor.OpenAiSummarizer")
    def test_later_run_processes_only_new_urls_and_saves_after_send(
        self, summarizer_class, notifier_class
    ):
        with tempfile.TemporaryDirectory() as directory:
            store = ProcessedUrlStore(Path(directory) / "processed_urls.json")
            old_url = "https://www.mhlw.go.jp/old"
            new_url = "https://www.mhlw.go.jp/new"
            store.save([old_url])
            scraper = Mock()
            scraper.list_transcripts.return_value = [
                Transcript("old", old_url),
                Transcript("new", new_url),
            ]
            scraper.fetch_transcript.return_value = Transcript(
                "new", new_url, "body"
            )
            summarizer_class.return_value.summarize.return_value = (
                SummarizedTranscript("new", new_url, "summary")
            )

            environment = {
                "OPENAI_API_KEY": "test-key",
                "GMAIL_USERNAME": "sender@example.com",
                "GMAIL_APP_PASSWORD": "test-password",
            }
            with patch.dict(os.environ, environment, clear=True):
                RegulatoryMonitor(scraper, store).run()

            scraper.fetch_transcript.assert_called_once()
            notifier_class.return_value.send.assert_called_once()
            self.assertEqual(store.load(), {old_url, new_url})


class GmailNotifierTests(unittest.TestCase):
    def test_html_body_escapes_remote_content(self):
        body = GmailNotifier._html_body(
            [
                SummarizedTranscript(
                    title="<script>alert(1)</script>",
                    url='https://example.com/?q="bad"',
                    summary="<b>not markup</b>",
                )
            ]
        )

        self.assertNotIn("<script>", body)
        self.assertNotIn("<b>not markup</b>", body)
        self.assertIn("&lt;b&gt;not markup&lt;/b&gt;", body)


if __name__ == "__main__":
    unittest.main()

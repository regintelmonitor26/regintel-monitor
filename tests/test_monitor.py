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
    Transcript,
    parse_recipients,
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
        <header><a href="/header-minutes.html">議事録</a></header>
        <nav><a href="/breadcrumb-minutes.html">議事録</a></nav>
        <main id="content">
          <aside><a href="/right-menu-minutes.html">議事録</a></aside>
          <a href="/outside-table.html">議事録</a>
          <table>
            <tr><td>第10回</td><td><a href="/minutes.html">議事録</a></td></tr>
            <tr><td>第10回</td><td><a href="/minutes.html"> 議事録 </a></td></tr>
            <tr><td>第9回</td><td><a href="/summary.html">議事要旨</a></td></tr>
            <tr><td>第8回</td><td><a href="/files.pdf">資料</a></td></tr>
            <tr><td>第7回</td><td><a href="https://example.com/x">議事録</a></td></tr>
          </table>
        </main>
        <footer>
          <table><tr><td><a href="/footer-minutes.html">議事録</a></td></tr></table>
        </footer>
        """
        scraper = MhlwScraper(FakeHttpClient({index_url: html}))

        result = scraper.list_transcripts(index_url)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].url, "https://www.mhlw.go.jp/minutes.html")
        self.assertIn("第10回", result[0].title)

    def test_requires_a_main_or_content_region(self):
        index_url = "https://www.mhlw.go.jp/index.html"
        html = """
        <table>
          <tr><td>第10回</td><td><a href="/minutes.html">議事録</a></td></tr>
        </table>
        """
        scraper = MhlwScraper(FakeHttpClient({index_url: html}))

        with self.assertRaisesRegex(RuntimeError, "本文領域"):
            scraper.list_transcripts(index_url)

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

    @patch("monitor.GmailNotifier")
    def test_later_run_sends_only_new_urls_and_saves_after_send(
        self, notifier_class
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

            environment = {
                "GMAIL_USERNAME": "sender@example.com",
                "GMAIL_APP_PASSWORD": "test-password",
            }
            with patch.dict(os.environ, environment, clear=True):
                RegulatoryMonitor(scraper, store).run()

            notifier_class.assert_called_once_with(
                username="sender@example.com",
                app_password="test-password",
                recipients=["sender@example.com"],
            )
            notifier_class.return_value.send.assert_called_once_with(
                [Transcript("new", new_url)]
            )
            self.assertEqual(store.load(), {old_url, new_url})

    @patch("monitor.GmailNotifier")
    def test_no_new_urls_does_not_send_email(self, notifier_class):
        with tempfile.TemporaryDirectory() as directory:
            store = ProcessedUrlStore(Path(directory) / "processed_urls.json")
            url = "https://www.mhlw.go.jp/existing"
            store.save([url])
            scraper = Mock()
            scraper.list_transcripts.return_value = [Transcript("existing", url)]

            with patch.dict(os.environ, {}, clear=True):
                RegulatoryMonitor(scraper, store).run()

            notifier_class.assert_not_called()


class GmailNotifierTests(unittest.TestCase):
    @patch("monitor.smtplib.SMTP_SSL")
    def test_send_passes_all_recipients_to_smtp_envelope(self, smtp_ssl):
        recipients = ["first@example.com", "second@example.com"]
        notifier = GmailNotifier(
            username="sender@example.com",
            app_password="test-password",
            recipients=recipients,
        )

        notifier.send(
            [Transcript("第10回 議事録", "https://www.mhlw.go.jp/minutes")]
        )

        smtp = smtp_ssl.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("sender@example.com", "test-password")
        sent_message = smtp.send_message.call_args.args[0]
        self.assertEqual(
            smtp.send_message.call_args.kwargs,
            {
                "from_addr": "sender@example.com",
                "to_addrs": recipients,
            },
        )
        self.assertEqual(
            sent_message["To"],
            "first@example.com, second@example.com",
        )

    def test_parse_recipients_supports_multiple_addresses_and_whitespace(self):
        self.assertEqual(
            parse_recipients(
                "first@example.com, second@example.com, ,third@example.com",
                "fallback@example.com",
            ),
            [
                "first@example.com",
                "second@example.com",
                "third@example.com",
            ],
        )

    def test_parse_recipients_falls_back_for_missing_or_empty_value(self):
        self.assertEqual(
            parse_recipients(None, "fallback@example.com"),
            ["fallback@example.com"],
        )
        self.assertEqual(
            parse_recipients(" , ", "fallback@example.com"),
            ["fallback@example.com"],
        )

    def test_html_body_contains_title_and_url_and_escapes_content(self):
        body = GmailNotifier._html_body(
            [
                Transcript(
                    title="<script>alert(1)</script>",
                    url='https://example.com/?q="bad"',
                )
            ]
        )

        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertIn("https://example.com/?q=&quot;bad&quot;", body)


if __name__ == "__main__":
    unittest.main()

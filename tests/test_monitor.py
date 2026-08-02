import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import (
    GmailNotifier,
    MhlwScraper,
    PmdaIchMonitor,
    PmdaIchScraper,
    ProcessedUrlStore,
    RegulatoryMonitor,
    TrackedLink,
    TrackedLinkStore,
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


class PmdaIchScraperTests(unittest.TestCase):
    def test_extracts_only_first_link_after_heading_in_main_content(self):
        page_url = "https://www.pmda.go.jp/ich/index.html"
        html = """
        <header><a href="/header.pdf">header</a></header>
        <nav><h3>ガイドラインの進捗状況</h3><a href="/breadcrumb.pdf">wrong</a></nav>
        <main>
          <a href="/before.pdf">before</a>
          <h3>ガイドラインの進捗状況</h3>
          <p><a href="/first.pdf">2026年7月30日現在の進捗状況 [215.47KB]</a></p>
          <p><a href="/second.pdf">second</a></p>
        </main>
        <footer><a href="/footer.pdf">footer</a></footer>
        """
        scraper = PmdaIchScraper(FakeHttpClient({page_url: html}))

        result = scraper.get_progress_link(page_url)

        self.assertEqual(
            result,
            TrackedLink(
                text="2026年7月30日現在の進捗状況 [215.47KB]",
                url="https://www.pmda.go.jp/first.pdf",
            ),
        )

    def test_requires_the_progress_heading(self):
        page_url = "https://www.pmda.go.jp/ich/index.html"
        scraper = PmdaIchScraper(FakeHttpClient({page_url: "<main></main>"}))

        with self.assertRaisesRegex(RuntimeError, "ガイドラインの進捗状況"):
            scraper.get_progress_link(page_url)


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


class TrackedLinkStoreTests(unittest.TestCase):
    def test_round_trip_preserves_text_and_url(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "pmda_ich_link.json"
            store = TrackedLinkStore(path)
            link = TrackedLink("現在の進捗状況", "https://www.pmda.go.jp/current.pdf")

            store.save(link)

            self.assertEqual(store.load(), link)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"text": link.text, "url": link.url},
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


class PmdaIchMonitorTests(unittest.TestCase):
    @patch("monitor.create_notifier")
    def test_first_run_saves_current_link_without_email(self, create_notifier):
        with tempfile.TemporaryDirectory() as directory:
            store = TrackedLinkStore(Path(directory) / "pmda.json")
            current = TrackedLink("current", "https://www.pmda.go.jp/current.pdf")
            scraper = Mock()
            scraper.get_progress_link.return_value = current

            PmdaIchMonitor(scraper, store).run()

            self.assertEqual(store.load(), current)
            create_notifier.assert_not_called()

    @patch("monitor.create_notifier")
    def test_unchanged_link_does_not_send_email(self, create_notifier):
        with tempfile.TemporaryDirectory() as directory:
            store = TrackedLinkStore(Path(directory) / "pmda.json")
            current = TrackedLink("current", "https://www.pmda.go.jp/current.pdf")
            store.save(current)
            scraper = Mock()
            scraper.get_progress_link.return_value = current

            PmdaIchMonitor(scraper, store).run()

            create_notifier.assert_not_called()
            self.assertEqual(store.load(), current)

    @patch("monitor.create_notifier")
    def test_text_or_url_change_sends_email_and_updates_state(self, create_notifier):
        changes = [
            TrackedLink("new text", "https://www.pmda.go.jp/old.pdf"),
            TrackedLink("old text", "https://www.pmda.go.jp/new.pdf"),
        ]
        for current in changes:
            with self.subTest(current=current), tempfile.TemporaryDirectory() as directory:
                store = TrackedLinkStore(Path(directory) / "pmda.json")
                previous = TrackedLink(
                    "old text", "https://www.pmda.go.jp/old.pdf"
                )
                store.save(previous)
                scraper = Mock()
                scraper.get_progress_link.return_value = current
                notifier = Mock()
                create_notifier.return_value = notifier

                PmdaIchMonitor(scraper, store).run()

                notifier.send_pmda_change.assert_called_once_with(previous, current)
                self.assertEqual(store.load(), current)
                create_notifier.reset_mock()


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

    @patch("monitor.smtplib.SMTP_SSL")
    def test_pmda_email_contains_previous_current_and_new_url(self, smtp_ssl):
        notifier = GmailNotifier(
            username="sender@example.com",
            app_password="test-password",
            recipients=["recipient@example.com"],
        )
        previous = TrackedLink("old text", "https://www.pmda.go.jp/old.pdf")
        current = TrackedLink("new text", "https://www.pmda.go.jp/new.pdf")

        notifier.send_pmda_change(previous, current)

        smtp = smtp_ssl.return_value.__enter__.return_value
        message = smtp.send_message.call_args.args[0]
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("PMDA ICHガイドライン進捗状況", plain)
        self.assertIn("変更前のリンク文字列: old text", plain)
        self.assertIn("変更後のリンク文字列: new text", plain)
        self.assertIn("新しいURL: https://www.pmda.go.jp/new.pdf", plain)


if __name__ == "__main__":
    unittest.main()

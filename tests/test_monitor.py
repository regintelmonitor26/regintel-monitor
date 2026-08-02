import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor import (
    GmailNotifier,
    JPMA_ICH_URL,
    JPMA_MESSAGES_URL,
    JPMA_RESULTS_URL,
    JpmaMonitor,
    JpmaScraper,
    MhlwScraper,
    NotificationLink,
    PmdaIchMonitor,
    PmdaIchScraper,
    ProcessedUrlStore,
    RegulatoryMonitor,
    TrackedLink,
    TrackedLinkListStore,
    TrackedLinkStore,
    TestNotificationRunner,
    Transcript,
    environment_flag,
    main,
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


class JpmaScraperTests(unittest.TestCase):
    def test_extracts_only_links_from_each_designated_main_list(self):
        pages = {
            JPMA_ICH_URL: """
                <header><a href="/wrong">wrong</a></header>
                <main><aside><a href="/menu">menu</a></aside>
                  <h2>お知らせ・更新情報</h2>
                  <ul class="list-news">
                    <li><a href="/ich/one">ICH one</a></li>
                    <li><a href="/ich/two">ICH two</a></li>
                  </ul>
                  <h2>別一覧</h2><a href="/other">other</a>
                </main>
            """,
            JPMA_RESULTS_URL: """
                <main><h1>医薬品評価委員会の成果物 一覧</h1>
                  <a href="/breadcrumb">breadcrumb</a>
                  <div class="link-list-a"><a href="/result/one">Result one</a></div>
                </main>
            """,
            JPMA_MESSAGES_URL: """
                <main><h1>医薬品評価委員会からの連絡 すべての連絡一覧</h1>
                  <div class="link-list-a"><a href="/message/one">Message one</a></div>
                  <footer><a href="/footer">footer</a></footer>
                </main>
            """,
        }
        scraper = JpmaScraper(FakeHttpClient(pages))

        self.assertEqual([x.text for x in scraper.get_ich_links()], ["ICH one", "ICH two"])
        self.assertEqual([x.text for x in scraper.get_results_links()], ["Result one"])
        self.assertEqual([x.text for x in scraper.get_message_links()], ["Message one"])


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


class TrackedLinkListStoreTests(unittest.TestCase):
    def test_round_trip_preserves_link_text_and_url(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TrackedLinkListStore(Path(directory) / "links.json")
            links = [
                TrackedLink("one", "https://www.jpma.or.jp/one"),
                TrackedLink("two", "https://www.jpma.or.jp/two"),
            ]

            store.save(links)

            self.assertEqual(store.load(), links)

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


class JpmaMonitorTests(unittest.TestCase):
    def _stores(self, directory):
        return {
            "JPMA ICH": TrackedLinkListStore(Path(directory) / "ich.json"),
            "JPMA 成果物一覧": TrackedLinkListStore(Path(directory) / "results.json"),
            "JPMA 連絡一覧": TrackedLinkListStore(Path(directory) / "messages.json"),
        }

    @patch("monitor.create_notifier")
    def test_first_run_saves_each_target_without_notification(self, create_notifier):
        with tempfile.TemporaryDirectory() as directory:
            stores = self._stores(directory)
            scraper = Mock()
            scraper.get_ich_links.return_value = [
                TrackedLink("ich", "https://www.jpma.or.jp/ich")
            ]
            scraper.get_results_links.return_value = [
                TrackedLink("result", "https://www.jpma.or.jp/result")
            ]
            scraper.get_message_links.return_value = [
                TrackedLink("message", "https://www.jpma.or.jp/message")
            ]

            JpmaMonitor(scraper, stores).run()

            create_notifier.assert_not_called()
            self.assertTrue(all(store.exists() for store in stores.values()))

    @patch("monitor.create_notifier")
    def test_order_changes_do_not_notify(self, create_notifier):
        with tempfile.TemporaryDirectory() as directory:
            stores = self._stores(directory)
            one = TrackedLink("one", "https://www.jpma.or.jp/one")
            two = TrackedLink("two", "https://www.jpma.or.jp/two")
            for store in stores.values():
                store.save([one, two])
            scraper = Mock()
            scraper.get_ich_links.return_value = [two, one]
            scraper.get_results_links.return_value = [two, one]
            scraper.get_message_links.return_value = [two, one]

            JpmaMonitor(scraper, stores).run()

            create_notifier.assert_not_called()

    @patch("monitor.create_notifier")
    def test_new_links_from_changed_targets_are_aggregated_once(self, create_notifier):
        with tempfile.TemporaryDirectory() as directory:
            stores = self._stores(directory)
            old = TrackedLink("old", "https://www.jpma.or.jp/old")
            new_ich = TrackedLink("new ICH", "https://www.jpma.or.jp/new-ich")
            new_message = TrackedLink(
                "new message", "https://www.jpma.or.jp/new-message"
            )
            for store in stores.values():
                store.save([old])
            scraper = Mock()
            scraper.get_ich_links.return_value = [new_ich, old]
            scraper.get_results_links.return_value = [old]
            scraper.get_message_links.return_value = [new_message, old]
            notifier = create_notifier.return_value

            JpmaMonitor(scraper, stores).run()

            notifier.send_jpma_additions.assert_called_once_with(
                {
                    "JPMA ICH": [new_ich],
                    "JPMA 連絡一覧": [new_message],
                }
            )


class TestNotificationRunnerTests(unittest.TestCase):
    def test_sends_latest_mhlw_and_current_pmda_links_in_one_email(self):
        mhlw_scraper = Mock()
        mhlw_scraper.list_transcripts.return_value = [
            Transcript("総会 第651回議事録", "https://www.mhlw.go.jp/latest"),
            Transcript("総会 第650回議事録", "https://www.mhlw.go.jp/older"),
        ]
        pmda_scraper = Mock()
        pmda_scraper.get_progress_link.return_value = TrackedLink(
            "2026年7月30日現在の進捗状況",
            "https://www.pmda.go.jp/current.pdf",
        )
        notifier = Mock()

        TestNotificationRunner(mhlw_scraper, pmda_scraper, notifier).run()

        notifier.send_test_notification.assert_called_once_with(
            [
                NotificationLink(
                    target_name="MHLW 総会 第651回議事録",
                    link_text="議事録",
                    url="https://www.mhlw.go.jp/latest",
                ),
                NotificationLink(
                    target_name="PMDA ICHガイドライン進捗状況",
                    link_text="2026年7月30日現在の進捗状況",
                    url="https://www.pmda.go.jp/current.pdf",
                ),
            ]
        )

    def test_environment_flag_accepts_boolean_workflow_input(self):
        for value in ("true", "TRUE", "1", "yes", "on"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"SEND_TEST_EMAIL": value}, clear=True
            ):
                self.assertTrue(environment_flag("SEND_TEST_EMAIL"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(environment_flag("SEND_TEST_EMAIL"))

    @patch("monitor.TrackedLinkStore")
    @patch("monitor.ProcessedUrlStore")
    @patch("monitor.TestNotificationRunner")
    @patch("monitor.create_notifier")
    @patch("monitor.HttpClient")
    def test_main_test_mode_does_not_construct_state_stores(
        self,
        http_client,
        create_notifier,
        runner_class,
        processed_store,
        tracked_store,
    ):
        with patch.dict(os.environ, {"SEND_TEST_EMAIL": "true"}, clear=True):
            main()

        runner_class.return_value.run.assert_called_once_with()
        processed_store.assert_not_called()
        tracked_store.assert_not_called()


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

    @patch("monitor.smtplib.SMTP_SSL")
    def test_test_email_contains_both_targets_in_one_message(self, smtp_ssl):
        notifier = GmailNotifier(
            username="sender@example.com",
            app_password="test-password",
            recipients=["recipient@example.com"],
        )
        links = [
            NotificationLink(
                "MHLW 中央社会保険医療協議会",
                "議事録",
                "https://www.mhlw.go.jp/latest",
            ),
            NotificationLink(
                "PMDA ICHガイドライン進捗状況",
                "現在の進捗状況",
                "https://www.pmda.go.jp/current.pdf",
            ),
        ]

        notifier.send_test_notification(links)

        smtp = smtp_ssl.return_value.__enter__.return_value
        self.assertEqual(smtp.send_message.call_count, 1)
        message = smtp.send_message.call_args.args[0]
        plain = message.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(
            message["Subject"],
            "【テスト】Regulatory Monitor 通知確認",
        )
        for link in links:
            self.assertIn(f"対象名: {link.target_name}", plain)
            self.assertIn(f"リンク文字列: {link.link_text}", plain)
            self.assertIn(f"URL: {link.url}", plain)


if __name__ == "__main__":
    unittest.main()

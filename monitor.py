"""Monitor MHLW minutes and the PMDA ICH progress link."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger(__name__)
TARGET_URL = "https://www.mhlw.go.jp/stf/shingi/shingi-chuo_128154.html"
STATE_PATH = Path("data/processed_urls.json")
PMDA_TARGET_URL = "https://www.pmda.go.jp/int-activities/int-harmony/ich/0070.html"
PMDA_STATE_PATH = Path("data/pmda_ich_link.json")
PMDA_HEADING = "ガイドラインの進捗状況"
JPMA_ICH_URL = "https://www.jpma.or.jp/information/ich/index.html"
JPMA_RESULTS_URL = (
    "https://www.jpma.or.jp/information/evaluation/results/allotment/index.html"
)
JPMA_MESSAGES_URL = (
    "https://www.jpma.or.jp/information/evaluation/results/message/index.html"
)
REQUEST_TIMEOUT = 30
USER_AGENT = "regintel-monitor/1.0 (+https://github.com/regintelmonitor26/regintel-monitor)"


@dataclass(frozen=True)
class Transcript:
    """A meeting transcript discovered on the monitored page."""

    title: str
    url: str


@dataclass(frozen=True)
class TrackedLink:
    """The text and absolute URL of a single monitored link."""

    text: str
    url: str


@dataclass(frozen=True)
class NotificationLink:
    """A labeled link rendered in a manual test notification."""

    target_name: str
    link_text: str
    url: str


class HttpClient:
    """Fetch UTF-8-compatible HTML pages with consistent error handling."""

    def __init__(self, timeout: int = REQUEST_TIMEOUT) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def get_text(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        return response.text


class MhlwScraper:
    """Extract transcript links from meeting-table rows in MHLW main content."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    def list_transcripts(self, index_url: str = TARGET_URL) -> list[Transcript]:
        page = self.http_client.get_text(index_url)
        soup = BeautifulSoup(page, "html.parser")
        content = (
            soup.select_one("main#content")
            or soup.select_one("main")
            or soup.select_one("#content")
        )
        if content is None:
            raise RuntimeError("対象ページの本文領域を特定できませんでした。")

        transcripts: list[Transcript] = []
        seen: set[str] = set()

        for row in content.select("table tr"):
            for anchor in row.select("a[href]"):
                label = self._normalize_text(anchor.get_text(" ", strip=True))
                if label != "議事録":
                    continue

                url = urljoin(index_url, anchor["href"])
                if not self._is_mhlw_http_url(url) or url in seen:
                    continue

                row_text = self._normalize_text(row.get_text(" ", strip=True))
                title = (
                    self._title_from_row(row_text)
                    or "中央社会保険医療協議会 議事録"
                )
                transcripts.append(Transcript(title=title, url=url))
                seen.add(url)

        if not transcripts:
            raise RuntimeError("対象ページから「議事録」リンクを1件も抽出できませんでした。")
        return transcripts

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _is_mhlw_http_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname == "www.mhlw.go.jp"

    @staticmethod
    def _title_from_row(row_text: str) -> str:
        match = re.search(r"第\d+回", row_text)
        return (
            f"中央社会保険医療協議会 総会 {match.group(0)}議事録"
            if match
            else ""
        )


class PmdaIchScraper:
    """Extract the first link after the ICH progress heading in PMDA main content."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    def get_progress_link(self, page_url: str = PMDA_TARGET_URL) -> TrackedLink:
        page = self.http_client.get_text(page_url)
        soup = BeautifulSoup(page, "html.parser")
        content = soup.select_one("main") or soup.select_one("#contents")
        if content is None:
            raise RuntimeError("PMDAページの本文領域を特定できませんでした。")

        heading = next(
            (
                element
                for element in content.find_all(
                    ["h1", "h2", "h3", "h4", "h5", "h6"]
                )
                if self._normalize_text(element.get_text(" ", strip=True))
                == PMDA_HEADING
            ),
            None,
        )
        if heading is None:
            raise RuntimeError(f"PMDAページに見出し「{PMDA_HEADING}」がありません。")

        anchor = next(
            (
                element
                for element in heading.find_all_next("a", href=True)
                if element in content.descendants
            ),
            None,
        )
        if anchor is None:
            raise RuntimeError(f"見出し「{PMDA_HEADING}」の後にリンクがありません。")

        text = self._normalize_text(anchor.get_text(" ", strip=True))
        url = urljoin(page_url, anchor["href"])
        if not text or not self._is_pmda_http_url(url):
            raise RuntimeError("PMDA進捗状況リンクの文字列またはURLが不正です。")
        return TrackedLink(text=text, url=url)

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _is_pmda_http_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname == "www.pmda.go.jp"


class JpmaScraper:
    """Extract links only from the three designated JPMA main-content lists."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    def get_ich_links(self) -> list[TrackedLink]:
        content = self._main_content(JPMA_ICH_URL)
        heading = self._find_heading(content, "お知らせ・更新情報")
        link_list = heading.find_next("ul", class_="list-news")
        if link_list is None or content not in link_list.parents:
            raise RuntimeError("JPMA ICHのお知らせ・更新情報一覧を特定できません。")
        return self._extract_links(link_list, JPMA_ICH_URL)

    def get_results_links(self) -> list[TrackedLink]:
        content = self._main_content(JPMA_RESULTS_URL)
        heading = content.find("h1")
        if heading is None or "成果物" not in self._normalize(heading.get_text(" ")):
            raise RuntimeError("JPMA成果物一覧の見出しを特定できません。")
        link_list = content.select_one(".link-list-a")
        if link_list is None:
            raise RuntimeError("JPMA成果物一覧（新着順）を特定できません。")
        return self._extract_links(link_list, JPMA_RESULTS_URL)

    def get_message_links(self) -> list[TrackedLink]:
        content = self._main_content(JPMA_MESSAGES_URL)
        heading = content.find("h1")
        if heading is None or "連絡一覧" not in self._normalize(heading.get_text(" ")):
            raise RuntimeError("JPMA連絡一覧の見出しを特定できません。")
        link_list = content.select_one(".link-list-a")
        if link_list is None:
            raise RuntimeError("JPMA連絡一覧を特定できません。")
        return self._extract_links(link_list, JPMA_MESSAGES_URL)

    def _main_content(self, url: str):
        soup = BeautifulSoup(self.http_client.get_text(url), "html.parser")
        content = soup.select_one("main")
        if content is None:
            raise RuntimeError("JPMAページの本文領域を特定できません。")
        return content

    def _find_heading(self, content, text: str):
        heading = next(
            (
                item
                for item in content.find_all(["h1", "h2", "h3", "h4"])
                if self._normalize(item.get_text(" ")) == text
            ),
            None,
        )
        if heading is None:
            raise RuntimeError(f"JPMAページに見出し「{text}」がありません。")
        return heading

    def _extract_links(self, container, base_url: str) -> list[TrackedLink]:
        links: list[TrackedLink] = []
        seen: set[tuple[str, str]] = set()
        for anchor in container.select("a[href]"):
            text = self._normalize(anchor.get_text(" ", strip=True))
            url = urljoin(base_url, anchor["href"])
            key = (text, url)
            if text and self._is_jpma_url(url) and key not in seen:
                links.append(TrackedLink(text=text, url=url))
                seen.add(key)
        if not links:
            raise RuntimeError("JPMA対象一覧からリンクを取得できませんでした。")
        return links

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _is_jpma_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and parsed.hostname in {
            "www.jpma.or.jp",
            "jpma.or.jp",
        }


class ProcessedUrlStore:
    """Load and atomically save the set of already processed transcript URLs."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> set[str]:
        if not self.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"状態ファイルを読み込めません: {self.path}") from exc
        if not isinstance(data, list) or not all(isinstance(url, str) for url in data):
            raise RuntimeError(f"状態ファイルの形式が不正です: {self.path}")
        return set(data)

    def save(self, urls: Iterable[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        payload = json.dumps(sorted(set(urls)), ensure_ascii=False, indent=2) + "\n"
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(self.path)


class TrackedLinkStore:
    """Load and atomically save one monitored text-and-URL pair."""

    def __init__(self, path: Path = PMDA_STATE_PATH) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TrackedLink:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"状態ファイルを読み込めません: {self.path}") from exc
        if (
            not isinstance(data, dict)
            or set(data) != {"text", "url"}
            or not all(isinstance(data[key], str) for key in ("text", "url"))
        ):
            raise RuntimeError(f"状態ファイルの形式が不正です: {self.path}")
        return TrackedLink(text=data["text"], url=data["url"])

    def save(self, link: TrackedLink) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        payload = json.dumps(
            {"text": link.text, "url": link.url},
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(self.path)


class TrackedLinkListStore:
    """Store an ordered snapshot while comparing links as an unordered set."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> list[TrackedLink]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"状態ファイルを読み込めません: {self.path}") from exc
        if not isinstance(data, list) or not all(
            isinstance(item, dict)
            and set(item) == {"text", "url"}
            and isinstance(item["text"], str)
            and isinstance(item["url"], str)
            for item in data
        ):
            raise RuntimeError(f"状態ファイルの形式が不正です: {self.path}")
        return [TrackedLink(item["text"], item["url"]) for item in data]

    def save(self, links: list[TrackedLink]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        payload = json.dumps(
            [{"text": item.text, "url": item.url} for item in links],
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(self.path)


class GmailNotifier:
    """Send new transcript titles and URLs as a multipart email through Gmail."""

    def __init__(
        self,
        username: str,
        app_password: str,
        recipients: list[str],
    ) -> None:
        self.username = username
        self.app_password = app_password
        self.recipients = recipients

    def send(self, transcripts: list[Transcript]) -> None:
        message = EmailMessage()
        count = len(transcripts)
        message["Subject"] = f"【中医協】新しい議事録 {count}件"
        message["From"] = self.username
        message["To"] = ", ".join(self.recipients)
        message.set_content(self._plain_body(transcripts))
        message.add_alternative(self._html_body(transcripts), subtype="html")

        self._deliver(message)

    def send_pmda_change(self, previous: TrackedLink, current: TrackedLink) -> None:
        message = EmailMessage()
        message["Subject"] = "【PMDA】ICHガイドライン進捗状況の更新"
        message["From"] = self.username
        message["To"] = ", ".join(self.recipients)
        message.set_content(
            "PMDA ICHガイドライン進捗状況\n\n"
            f"変更前のリンク文字列: {previous.text}\n"
            f"変更後のリンク文字列: {current.text}\n"
            f"新しいURL: {current.url}\n"
        )
        message.add_alternative(
            '<!doctype html><html lang="ja"><body style="font-family:'
            '-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#222">'
            "<h1>PMDA ICHガイドライン進捗状況</h1>"
            f"<p><strong>変更前のリンク文字列:</strong> {html.escape(previous.text)}</p>"
            f"<p><strong>変更後のリンク文字列:</strong> {html.escape(current.text)}</p>"
            f'<p><strong>新しいURL:</strong> <a href="'
            f'{html.escape(current.url, quote=True)}">{html.escape(current.url)}</a></p>'
            "</body></html>",
            subtype="html",
        )
        self._deliver(message)

    def send_test_notification(self, links: list[NotificationLink]) -> None:
        message = EmailMessage()
        message["Subject"] = "【テスト】Regulatory Monitor 通知確認"
        message["From"] = self.username
        message["To"] = ", ".join(self.recipients)
        message.set_content(self._test_plain_body(links))
        message.add_alternative(self._test_html_body(links), subtype="html")
        self._deliver(message)

    def send_jpma_additions(
        self, additions: dict[str, list[TrackedLink]]
    ) -> None:
        message = EmailMessage()
        message["Subject"] = "【JPMA】Regulatory Monitor 新着通知"
        message["From"] = self.username
        message["To"] = ", ".join(self.recipients)
        plain_sections = []
        html_sections = []
        for label, links in additions.items():
            plain_sections.append(
                f"【{label}】\n"
                + "\n\n".join(f"{item.text}\n{item.url}" for item in links)
            )
            html_sections.append(
                f"<h2>【{html.escape(label)}】</h2>"
                + "".join(
                    f'<p>{html.escape(item.text)}<br><a href="'
                    f'{html.escape(item.url, quote=True)}">{html.escape(item.url)}</a></p>'
                    for item in links
                )
            )
        message.set_content("\n\n".join(plain_sections))
        message.add_alternative(
            '<!doctype html><html lang="ja"><body>' + "".join(html_sections)
            + "</body></html>",
            subtype="html",
        )
        self._deliver(message)

    def _deliver(self, message: EmailMessage) -> None:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(self.username, self.app_password)
            smtp.send_message(
                message,
                from_addr=self.username,
                to_addrs=self.recipients,
            )

    @staticmethod
    def _plain_body(transcripts: list[Transcript]) -> str:
        sections = [f"{item.title}\n{item.url}" for item in transcripts]
        return "中央社会保険医療協議会の新しい議事録です。\n\n" + (
            "\n\n" + "=" * 60 + "\n\n"
        ).join(sections)

    @staticmethod
    def _html_body(transcripts: list[Transcript]) -> str:
        cards = []
        for item in transcripts:
            title = html.escape(item.title)
            url = html.escape(item.url, quote=True)
            cards.append(
                '<section style="margin:24px 0;padding:20px;border:1px solid #ddd;'
                'border-radius:8px">'
                f'<h2 style="margin-top:0">{title}</h2>'
                f'<p><a href="{url}">厚生労働省の議事録を開く</a></p>'
                "</section>"
            )
        return (
            '<!doctype html><html lang="ja"><body style="font-family:'
            '-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#222">'
            "<h1>中央社会保険医療協議会 新着議事録</h1>"
            + "".join(cards)
            + "</body></html>"
        )

    @staticmethod
    def _test_plain_body(links: list[NotificationLink]) -> str:
        sections = [
            f"対象名: {item.target_name}\n"
            f"リンク文字列: {item.link_text}\n"
            f"URL: {item.url}"
            for item in links
        ]
        return "Regulatory Monitorのテスト通知です。\n\n" + "\n\n".join(sections)

    @staticmethod
    def _test_html_body(links: list[NotificationLink]) -> str:
        sections = []
        for item in links:
            url = html.escape(item.url, quote=True)
            sections.append(
                '<section style="margin:24px 0;padding:20px;border:1px solid #ddd;'
                'border-radius:8px">'
                f"<h2>{html.escape(item.target_name)}</h2>"
                f"<p><strong>リンク文字列:</strong> {html.escape(item.link_text)}</p>"
                f'<p><strong>URL:</strong> <a href="{url}">{html.escape(item.url)}</a></p>'
                "</section>"
            )
        return (
            '<!doctype html><html lang="ja"><body style="font-family:'
            '-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;color:#222">'
            "<h1>Regulatory Monitor テスト通知</h1>"
            + "".join(sections)
            + "</body></html>"
        )


class RegulatoryMonitor:
    """Coordinate discovery, first-run initialization, and email delivery."""

    def __init__(
        self,
        scraper: MhlwScraper,
        store: ProcessedUrlStore,
    ) -> None:
        self.scraper = scraper
        self.store = store

    def run(self) -> None:
        discovered = self.scraper.list_transcripts()
        discovered_urls = {item.url for item in discovered}

        if not self.store.exists():
            self.store.save(discovered_urls)
            LOGGER.info(
                "初回実行: 既存の議事録 %d件を保存しました。メールは送信しません。",
                len(discovered_urls),
            )
            return

        processed = self.store.load()
        new_transcripts = [item for item in discovered if item.url not in processed]
        if not new_transcripts:
            LOGGER.info("新しい議事録はありません。")
            return

        username = required_environment("GMAIL_USERNAME")
        app_password = required_environment("GMAIL_APP_PASSWORD")
        recipients = parse_recipients(os.getenv("GMAIL_RECIPIENTS"), username)
        notifier = GmailNotifier(
            username=username,
            app_password=app_password,
            recipients=recipients,
        )

        notifier.send(new_transcripts)
        self.store.save(processed | {item.url for item in new_transcripts})
        LOGGER.info("新しい議事録 %d件をメール送信しました。", len(new_transcripts))


class PmdaIchMonitor:
    """Detect and notify changes to the PMDA ICH progress link."""

    def __init__(self, scraper: PmdaIchScraper, store: TrackedLinkStore) -> None:
        self.scraper = scraper
        self.store = store

    def run(self) -> None:
        current = self.scraper.get_progress_link()
        if not self.store.exists():
            self.store.save(current)
            LOGGER.info("PMDA初回実行: 現在の進捗状況リンクを保存しました。")
            return

        previous = self.store.load()
        if current == previous:
            LOGGER.info("PMDA ICH進捗状況リンクに変更はありません。")
            return

        notifier = create_notifier()
        notifier.send_pmda_change(previous, current)
        self.store.save(current)
        LOGGER.info("PMDA ICH進捗状況リンクの変更をメール送信しました。")


class JpmaMonitor:
    """Monitor three independent JPMA lists and aggregate new links."""

    def __init__(self, scraper: JpmaScraper, stores: dict[str, TrackedLinkListStore]):
        self.scraper = scraper
        self.stores = stores

    def run(self) -> None:
        snapshots = {
            "JPMA ICH": self.scraper.get_ich_links(),
            "JPMA 成果物一覧": self.scraper.get_results_links(),
            "JPMA 連絡一覧": self.scraper.get_message_links(),
        }
        additions: dict[str, list[TrackedLink]] = {}
        initialized: set[str] = set()
        for label, current in snapshots.items():
            store = self.stores[label]
            if not store.exists():
                store.save(current)
                initialized.add(label)
                continue
            previous = {(item.text, item.url) for item in store.load()}
            new_links = [
                item for item in current if (item.text, item.url) not in previous
            ]
            if new_links:
                additions[label] = new_links

        if additions:
            create_notifier().send_jpma_additions(additions)

        for label, current in snapshots.items():
            if label not in initialized:
                self.stores[label].save(current)
        LOGGER.info("JPMA監視を完了しました（新着対象: %d）。", len(additions))


class TestNotificationRunner:
    """Send current MHLW and PMDA links without touching monitor state."""

    def __init__(
        self,
        mhlw_scraper: MhlwScraper,
        pmda_scraper: PmdaIchScraper,
        notifier: GmailNotifier,
    ) -> None:
        self.mhlw_scraper = mhlw_scraper
        self.pmda_scraper = pmda_scraper
        self.notifier = notifier

    def run(self) -> None:
        latest_transcript = self.mhlw_scraper.list_transcripts()[0]
        pmda_link = self.pmda_scraper.get_progress_link()
        self.notifier.send_test_notification(
            [
                NotificationLink(
                    target_name=f"MHLW {latest_transcript.title}",
                    link_text="議事録",
                    url=latest_transcript.url,
                ),
                NotificationLink(
                    target_name="PMDA ICHガイドライン進捗状況",
                    link_text=pmda_link.text,
                    url=pmda_link.url,
                ),
            ]
        )
        LOGGER.info("現在のMHLW・PMDAリンクをテストメール送信しました。")


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません。")
    return value


def parse_recipients(value: str | None, fallback: str) -> list[str]:
    """Parse comma-separated recipients, falling back when none are configured."""
    recipients = [address.strip() for address in (value or "").split(",")]
    recipients = [address for address in recipients if address]
    return recipients or [fallback]


def create_notifier() -> GmailNotifier:
    username = required_environment("GMAIL_USERNAME")
    return GmailNotifier(
        username=username,
        app_password=required_environment("GMAIL_APP_PASSWORD"),
        recipients=parse_recipients(os.getenv("GMAIL_RECIPIENTS"), username),
    )


def environment_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    http_client = HttpClient()
    if environment_flag("SEND_TEST_EMAIL"):
        TestNotificationRunner(
            mhlw_scraper=MhlwScraper(http_client),
            pmda_scraper=PmdaIchScraper(http_client),
            notifier=create_notifier(),
        ).run()
        return

    mhlw_monitor = RegulatoryMonitor(
        scraper=MhlwScraper(HttpClient()),
        store=ProcessedUrlStore(),
    )
    pmda_monitor = PmdaIchMonitor(
        scraper=PmdaIchScraper(http_client),
        store=TrackedLinkStore(),
    )
    jpma_monitor = JpmaMonitor(
        scraper=JpmaScraper(http_client),
        stores={
            "JPMA ICH": TrackedLinkListStore(Path("data/jpma_ich_links.json")),
            "JPMA 成果物一覧": TrackedLinkListStore(
                Path("data/jpma_results_links.json")
            ),
            "JPMA 連絡一覧": TrackedLinkListStore(
                Path("data/jpma_messages_links.json")
            ),
        },
    )
    mhlw_monitor.run()
    pmda_monitor.run()
    jpma_monitor.run()


if __name__ == "__main__":
    main()

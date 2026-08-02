"""Monitor MHLW meeting minutes and email newly published links."""

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
REQUEST_TIMEOUT = 30
USER_AGENT = "regintel-monitor/1.0 (+https://github.com/regintelmonitor26/regintel-monitor)"


@dataclass(frozen=True)
class Transcript:
    """A meeting transcript discovered on the monitored page."""

    title: str
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    monitor = RegulatoryMonitor(
        scraper=MhlwScraper(HttpClient()),
        store=ProcessedUrlStore(),
    )
    monitor.run()


if __name__ == "__main__":
    main()

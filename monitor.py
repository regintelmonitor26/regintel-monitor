"""Monitor MHLW meeting minutes, summarize new entries, and email them."""

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
from openai import OpenAI


LOGGER = logging.getLogger(__name__)
TARGET_URL = "https://www.mhlw.go.jp/stf/shingi/shingi-chuo_128154.html"
STATE_PATH = Path("data/processed_urls.json")
DEFAULT_MODEL = "gpt-5.6-luna"
REQUEST_TIMEOUT = 30
SUMMARY_CHUNK_SIZE = 30_000
USER_AGENT = "regintel-monitor/1.0 (+https://github.com/regintelmonitor26/regintel-monitor)"


@dataclass(frozen=True)
class Transcript:
    """A meeting transcript discovered on the monitored page."""

    title: str
    url: str
    body: str = ""


@dataclass(frozen=True)
class SummarizedTranscript:
    """A transcript paired with its generated Japanese summary."""

    title: str
    url: str
    summary: str


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
    """Extract transcript links and readable transcript text from MHLW HTML."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    def list_transcripts(self, index_url: str = TARGET_URL) -> list[Transcript]:
        page = self.http_client.get_text(index_url)
        soup = BeautifulSoup(page, "html.parser")
        transcripts: list[Transcript] = []
        seen: set[str] = set()

        for anchor in soup.select("a[href]"):
            label = self._normalize_text(anchor.get_text(" ", strip=True))
            if label != "議事録":
                continue

            url = urljoin(index_url, anchor["href"])
            if not self._is_mhlw_http_url(url) or url in seen:
                continue

            row = anchor.find_parent("tr")
            row_text = self._normalize_text(row.get_text(" ", strip=True)) if row else ""
            title = self._title_from_row(row_text) or "中央社会保険医療協議会 議事録"
            transcripts.append(Transcript(title=title, url=url))
            seen.add(url)

        if not transcripts:
            raise RuntimeError("対象ページから「議事録」リンクを1件も抽出できませんでした。")
        return transcripts

    def fetch_transcript(self, transcript: Transcript) -> Transcript:
        page = self.http_client.get_text(transcript.url)
        soup = BeautifulSoup(page, "html.parser")
        content = soup.select_one("main") or soup.select_one("#content") or soup.body
        if content is None:
            raise RuntimeError(f"議事録本文の領域を取得できませんでした: {transcript.url}")

        for unwanted in content.select(
            "script, style, noscript, nav, header, footer, form, "
            ".breadcrumb, .m-h, .m-footer, .p-breadcrumb"
        ):
            unwanted.decompose()

        heading = content.find("h1")
        title = (
            self._normalize_text(heading.get_text(" ", strip=True))
            if heading
            else transcript.title
        )
        body = content.get_text("\n", strip=True)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if len(body) < 200:
            raise RuntimeError(f"議事録本文が短すぎます: {transcript.url}")
        return Transcript(title=title, url=transcript.url, body=body)

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


class OpenAiSummarizer:
    """Summarize long Japanese transcripts with the OpenAI Responses API."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def summarize(self, transcript: Transcript) -> SummarizedTranscript:
        chunks = list(self._split_text(transcript.body))
        partial_summaries = [
            self._create_summary(
                "以下は会議議事録の一部です。重要な決定、論点、委員の意見、"
                "今後の対応を、事実に忠実な日本語の箇条書きで要約してください。"
                "本文にない情報を補わないでください。",
                chunk,
            )
            for chunk in chunks
        ]

        if len(partial_summaries) == 1:
            summary = partial_summaries[0]
        else:
            summary = self._create_summary(
                "以下は同じ会議議事録を分割して要約したものです。重複を除き、"
                "「概要」「主な議題・決定」「主な意見」「今後の対応」の見出しを"
                "使って、簡潔で読みやすい最終要約に統合してください。",
                "\n\n--- 分割要約 ---\n\n".join(partial_summaries),
            )

        return SummarizedTranscript(
            title=transcript.title,
            url=transcript.url,
            summary=summary,
        )

    def _create_summary(self, instructions: str, content: str) -> str:
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": "low"},
            instructions=instructions,
            input=content,
        )
        summary = response.output_text.strip()
        if not summary:
            raise RuntimeError("OpenAI APIから空の要約が返されました。")
        return summary

    @staticmethod
    def _split_text(text: str) -> Iterable[str]:
        paragraphs = text.splitlines()
        chunk: list[str] = []
        length = 0
        for paragraph in paragraphs:
            if length + len(paragraph) + 1 > SUMMARY_CHUNK_SIZE and chunk:
                yield "\n".join(chunk)
                chunk = []
                length = 0
            while len(paragraph) > SUMMARY_CHUNK_SIZE:
                if chunk:
                    yield "\n".join(chunk)
                    chunk = []
                    length = 0
                yield paragraph[:SUMMARY_CHUNK_SIZE]
                paragraph = paragraph[SUMMARY_CHUNK_SIZE:]
            chunk.append(paragraph)
            length += len(paragraph) + 1
        if chunk:
            yield "\n".join(chunk)


class GmailNotifier:
    """Send summarized transcripts as a multipart HTML email through Gmail."""

    def __init__(self, username: str, app_password: str) -> None:
        self.username = username
        self.app_password = app_password

    def send(self, transcripts: list[SummarizedTranscript]) -> None:
        message = EmailMessage()
        count = len(transcripts)
        message["Subject"] = f"【中医協】新しい議事録 {count}件"
        message["From"] = self.username
        message["To"] = self.username
        message.set_content(self._plain_body(transcripts))
        message.add_alternative(self._html_body(transcripts), subtype="html")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(self.username, self.app_password)
            smtp.send_message(message)

    @staticmethod
    def _plain_body(transcripts: list[SummarizedTranscript]) -> str:
        sections = [
            f"{item.title}\n{item.url}\n\n{item.summary}" for item in transcripts
        ]
        return "中央社会保険医療協議会の新しい議事録です。\n\n" + (
            "\n\n" + "=" * 60 + "\n\n"
        ).join(sections)

    @staticmethod
    def _html_body(transcripts: list[SummarizedTranscript]) -> str:
        cards = []
        for item in transcripts:
            summary = html.escape(item.summary).replace("\n", "<br>")
            title = html.escape(item.title)
            url = html.escape(item.url, quote=True)
            cards.append(
                '<section style="margin:24px 0;padding:20px;border:1px solid #ddd;'
                'border-radius:8px">'
                f'<h2 style="margin-top:0">{title}</h2>'
                f'<p><a href="{url}">厚生労働省の議事録を開く</a></p>'
                f'<div style="line-height:1.7">{summary}</div>'
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
    """Coordinate discovery, first-run initialization, summaries, and delivery."""

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

        api_key = required_environment("OPENAI_API_KEY")
        username = required_environment("GMAIL_USERNAME")
        app_password = required_environment("GMAIL_APP_PASSWORD")
        model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        summarizer = OpenAiSummarizer(api_key=api_key, model=model)
        notifier = GmailNotifier(username=username, app_password=app_password)

        summaries = [
            summarizer.summarize(self.scraper.fetch_transcript(item))
            for item in new_transcripts
        ]
        notifier.send(summaries)
        self.store.save(processed | {item.url for item in new_transcripts})
        LOGGER.info("新しい議事録 %d件を要約し、メール送信しました。", len(summaries))


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"環境変数 {name} が設定されていません。")
    return value


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    monitor = RegulatoryMonitor(
        scraper=MhlwScraper(HttpClient()),
        store=ProcessedUrlStore(),
    )
    monitor.run()


if __name__ == "__main__":
    main()

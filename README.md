# regintel-monitor

厚生労働省の中医協議事録と、PMDAのICHガイドライン進捗状況リンクを監視し、変更をGmailで通知します。

## 動作

監視対象:

- MHLW: https://www.mhlw.go.jp/stf/shingi/shingi-chuo_128154.html
- PMDA: https://www.pmda.go.jp/int-activities/int-harmony/ich/0070.html

### MHLW議事録

1. 対象ページから、リンク文字列が「議事録」のリンクだけを抽出します。
2. `data/processed_urls.json` が存在しない初回実行では、既存のURLを保存して終了します。要約とメール送信は行いません。
3. 2回目以降は、保存済みURLに含まれない議事録だけを検出します。
4. 新しい議事録の会議タイトルとURLを1通のHTMLメールにまとめて送信します。
5. メール送信に成功した場合だけ、処理済みURLを更新します。

新しい議事録がない場合、メールは送信しません。

### PMDA ICHガイドライン

1. 本文の見出し「ガイドラインの進捗状況」を特定します。
2. 見出し直後の最初のリンク1件について、リンク文字列とURLを監視します。
3. `data/pmda_ich_link.json`が存在しない初回実行では、現在値を保存してメールは送信しません。
4. 2回目以降、リンク文字列またはURLが変わった場合だけ、変更前後の文字列と新しいURLを通知します。

## GitHub Secrets

リポジトリの `Settings` → `Secrets and variables` → `Actions` に以下を登録してください。

| Secret | 内容 |
| --- | --- |
| `GMAIL_USERNAME` | 送信元のGmailアドレス |
| `GMAIL_APP_PASSWORD` | Gmailのアプリパスワード |
| `GMAIL_RECIPIENTS` | 送信先。複数の場合はカンマ区切り（未設定時は`GMAIL_USERNAME`） |

Gmailアカウントでは2段階認証を有効にし、通常のログインパスワードではなくアプリパスワードを使用してください。

複数の送信先を指定する例:

```text
GMAIL_RECIPIENTS=regintel.monitor26@gmail.com,hideki.jinguji@abbvie.com
```

`GMAIL_RECIPIENTS`の各アドレス前後の空白は無視されます。Secretを登録しない場合や値が空の場合は、`GMAIL_USERNAME`が送信先になります。

## 実行方法

GitHubの `Actions` タブから `Run regulatory monitor` を選び、`Run workflow` を実行します。

初回実行後、ワークフローが作成した監視対象ごとの状態ファイルは自動的にコミットされます。以後の実行でも、正常に通知した変更が更新・コミットされます。

## ローカル実行

Python 3.11以降を推奨します。

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python monitor.py
```

新しい議事録がある場合のみ、次の環境変数が必要です。

- `GMAIL_USERNAME`
- `GMAIL_APP_PASSWORD`
- `GMAIL_RECIPIENTS`（任意。未設定時は`GMAIL_USERNAME`）

## 状態ファイル

- `data/processed_urls.json`: MHLWの処理済み議事録URL
- `data/pmda_ich_link.json`: PMDAのリンク文字列とURL

状態ファイルを削除すると、該当する監視対象だけが次回実行時に初回扱いとなり、現在値を保存して通知せず終了します。

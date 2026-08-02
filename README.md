# regintel-monitor

厚生労働省の「中央社会保険医療協議会（中央社会保険医療協議会総会）」ページを監視し、新しく公開された議事録のタイトルとURLをGmailで通知します。

## 動作

監視対象:

https://www.mhlw.go.jp/stf/shingi/shingi-chuo_128154.html

1. 対象ページから、リンク文字列が「議事録」のリンクだけを抽出します。
2. `data/processed_urls.json` が存在しない初回実行では、既存のURLを保存して終了します。要約とメール送信は行いません。
3. 2回目以降は、保存済みURLに含まれない議事録だけを検出します。
4. 新しい議事録の会議タイトルとURLを1通のHTMLメールにまとめて送信します。
5. メール送信に成功した場合だけ、処理済みURLを更新します。

新しい議事録がない場合、メールは送信しません。

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

初回実行後、ワークフローが作成した `data/processed_urls.json` は自動的にコミットされます。以後の実行でも、新しい議事録を正常に通知した場合に同ファイルが更新・コミットされます。

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

`data/processed_urls.json` は監視状態そのものなので、削除しないでください。削除すると次回実行が初回扱いとなり、その時点の議事録をすべて保存して通知せず終了します。

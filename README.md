# GnuCash Web

GnuCash の SQLite ファイル（`.gnucash`）を直接読み書きする Web 版。Python 標準ライブラリのみで動く（パスキー認証を使う場合のみ py_webauthn が必要）。

## データの置き場所

GnuCash ファイルはプロジェクト直下の `data/` に置く。`data/` は `.gitignore` 済みで Git には入らない。

```
data/
├── gnucash.gnucash      本体（GnuCash の SQLite ファイル）
└── backup/              編集モードで起動するたびに作られるバックアップ
```

- シンボリックリンクではなく実体を置く（Docker のコンテナ内ではリンク先が見えないため）
- GnuCash デスクトップと Web 版で同時に書き込まない。デスクトップ版で開いている間は、Web 版は書き込みを拒否する（`--force` を付けない場合）

## 起動

```bash
# 読み取り専用（既定・ファイルを一切変更しない）
python3 web/server.py

# 編集可能（起動時に data/backup/gnucash.gnucash.YYYYMMDDHHMMSS.bak を作成）
python3 web/server.py --write

# 別のファイルを開く
python3 web/server.py path/to/other.gnucash
```

ブラウザで http://127.0.0.1:8000/ を開く（`--port` で変更可）。サーバは 127.0.0.1 のみで待ち受ける。

GnuCash デスクトップのロック（`gnclock`）が残っていて書き込めない場合は、デスクトップ版を閉じていることを確認してから `--force` を付ける。

## Docker（docker compose）で起動

```bash
cp .env.example .env      # 初回のみ
docker compose up -d --build
```

http://127.0.0.1:8000/ を開く。停止は `docker compose down`、ログは `docker compose logs -f`。

| 設定（.env） | 内容 |
|---|---|
| `GNUCASH_DATA_DIR` | GnuCash ファイルを置くフォルダ（既定 `./data`）。バックアップはその中の `backup/` に作られる |
| `GNUCASH_FILE_NAME` | ファイル名（既定 `gnucash.gnucash`） |
| `GNUCASH_WEB_PORT` | 公開ポート（既定 8000。127.0.0.1 のみで公開） |
| `GNUCASH_WRITE` / `GNUCASH_FORCE` | `1` で `--write` / `--force` と同じ |
| `GNUCASH_TZ` | 取引日の表示に使うタイムゾーン（既定 Asia/Tokyo）。UTC のままだと古い形式の日付が 1 日ずれる |

- イメージには `web/` だけが入り、GnuCash ファイルやバックアップは含まれない（`.dockerignore`）
- コード（`web/`）を変更したら `docker compose up -d --build` で作り直す

## パスキー認証

`.env` の `GNUCASH_AUTH=1` でログインが必要になる（ID・パスワードなし、パスキーのみ）。利用者は 1 人を想定。

### 有効にする手順（このMacだけで使う場合）

1. `.env` を `GNUCASH_AUTH=1`、`GNUCASH_AUTH_RP_ID=localhost`、`GNUCASH_AUTH_ORIGIN=http://localhost:8000` にする
2. `docker compose up -d --build`
3. ログに出る登録用トークンを確認する：`docker compose logs gnucash-web | grep トークン`
4. **http://localhost:8000/** を開き（`127.0.0.1` では使えない）、トークンを入力して「パスキーを作成して登録」。1Password の保存画面が出たら保存する
5. 以降は「パスキーでログイン」だけでログインできる（有効期間 30 日）

### 外出先から使う（Tailscale 経由・推奨）

インターネットに直接公開せず、Tailscale の VPN 内だけで使う。正式な HTTPS 証明書とドメイン名が自動で付く。

1. Mac とスマートフォンに Tailscale を入れ、同じアカウントでログインする
2. Tailscale の管理画面（DNS 設定）で MagicDNS と HTTPS Certificates を有効にする
3. Mac で `tailscale serve --bg 8000` を実行する（`https://<マシン名>.<tailnet名>.ts.net` → `localhost:8000` に転送される）
4. `.env` を次のようにして `docker compose up -d`
   ```
   GNUCASH_AUTH=1
   GNUCASH_AUTH_RP_ID=<マシン名>.<tailnet名>.ts.net
   GNUCASH_AUTH_ORIGIN=https://<マシン名>.<tailnet名>.ts.net
   ```
5. Mac からも `https://<マシン名>.<tailnet名>.ts.net` で開いて初回登録する（パスキーはこのドメインに結び付く。localhost で登録したものは使えない）

公開ポートは `127.0.0.1:8000` のままでよい（LAN には公開されず、Tailscale 経由でのみ外から届く）。

### パスキーの管理・復旧

- 左メニュー下の「パスキー」から一覧・追加・削除。別の端末は、その端末でログインしてから追加する（1Password のパスキーは端末間で同期されるので、通常は追加不要）
- パスキーをすべて失った場合：`.env` を `GNUCASH_AUTH=0` にして起動すれば認証なしで入れる。または `data/auth.json` を削除して起動すると、新しい登録用トークンが出る
- 最後の 1 本を削除すると全端末がログアウトされ、新しい登録用トークンがログに出る

### 保存場所と注意

- 認証情報は `data/auth.json`（Git の管理外）。公開鍵のみで秘密鍵は持たないが、**ログイン状態の署名鍵を含むので外部に出さないこと**
- 直接起動（Docker なし）で認証を使う場合は py_webauthn が必要：
  ```bash
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  GNUCASH_AUTH=1 .venv/bin/python web/server.py --write
  ```

## 構成

| ファイル | 内容 |
|---|---|
| `server.py` | HTTP サーバ + SQLite アクセス（API） |
| `auth.py` | パスキー（WebAuthn）認証。`GNUCASH_AUTH=1` のときだけ読み込まれる |
| `static/index.html` | 画面（勘定科目ツリー・登録簿・入力フォーム） |

## 画面

| 画面 | 内容 |
|---|---|
| 勘定科目の取引一覧 | 左のツリーで科目を選ぶ。その科目から見た入金・出金と残高を表示し、一番下の入力欄から追加する |
| 最近の取引 | 左メニューの「最近の取引」。入力日時の新しい順に全科目の取引を表示（今日／7日／30日）。一番上の入力欄から「出金元 → 入金先 ＋ 金額」で追加でき、追加後も日付と出金元は残る（連続入力） |

どちらの画面でも、行をクリックすると編集・削除・分割・コピーができる。

## API

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/book` | 勘定科目ツリー・各科目の残高・説明文の候補・ロック状態 |
| GET | `/api/accounts/{guid}/register` | 科目の取引一覧（日付順・累計残高つき） |
| GET | `/api/recent?days=7` | 入力日時（`enter_date`）が直近 N 日（今日を含む）の取引を新しい順に返す。各取引の全 split つき |
| GET | `/api/quickfill?account=&desc=` | 同じ説明の直近取引から相手科目と金額を返す。`account` を省略すると出金元（貸方）・入金先（借方）・金額を返す |
| GET | `/api/transactions/{guid}` | 取引 1 件の全 split（分割の表示・編集用）。`editable` / `reason` で編集可否を返す |
| POST | `/api/transactions` | 取引の追加。単純な取引は `{account, other, date, desc, amount}`（amount は account 側の借方 = 正）、分割取引は `{date, desc, splits: [{account, memo, amount}]}` |
| PUT | `/api/transactions/{guid}` | 取引の更新。`splits` を送ると分割行で置き換え（`guid` のある行は更新、ない行は追加、送らなかった既存行は削除） |
| POST | `/api/transactions/{guid}/copy` | 取引の複製 `{date}`（省略時は本日）。全 split・メモ・処理・備考を写し、番号・照合状態・ロットは引き継がない |
| DELETE | `/api/transactions/{guid}` | 取引の削除。戻り値の `snapshot` で元に戻せる |
| POST | `/api/transactions/restore` | 削除した取引の復元 |

## 書き込み時の仕様（GnuCash 本体に合わせている点）

- `transactions.post_date` はその日の `10:59:00` UTC。`slots` の `date-posted`（GDATE）も同時に更新する
- 日時の書式は `books` の `features/ISO-8601...` slot があれば `YYYY-MM-DD HH:MM:SS`、なければ `YYYYMMDDHHMMSS`
- 金額は `commodity.fraction` を分母とする有理数（JPY なら `n/1`）
- 新規 split は `reconcile_state='n'`, `reconcile_date=1970-01-01 00:00:00`
- 削除時は取引・split に紐づく slot（FRAME の子も含む）もまとめて削除

## 現状の制限

- 照合済み（`y`）の取引、ロット・請求書に紐づく取引は編集・削除できない
- 通貨・銘柄の異なる科目間の取引（外貨・株式）の追加・金額変更は未対応（表示は可能）
- 分割取引（split が 3 つ以上）は GnuCash と同様に「分割取引」と表示し、展開・分割行の編集ができる。貸借が一致しないと保存できない（GnuCash のように 貸借不一致 科目へ自動計上はしない）
- 分割の「処理（action）」欄は表示のみで編集できない
- 子科目の合計は同じ通貨のものだけを足す（為替換算はしない）
- `post_date` は 2 形式が混在するため、SQL で並べ替えるときは区切り文字を除いた値で比較している

## ライセンス

[MIT](LICENSE)

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

## AIアシスタント

画面左メニューの「AIアシスタント」から Anthropic Claude とチャット形式でやりとりできる。「分析」「記帳」の2つのタブがあり、読み取り専用の分析と、実際に取引を書き込む記帳とを画面上はっきり分けている。

### 有効にする

環境変数 `GNUCASH_AI_API_KEY`（Anthropic の API キー）を設定する。未設定でも他の機能はそのまま使える（AIアシスタントは「利用できません」と表示される）。

```bash
GNUCASH_AI_API_KEY=sk-ant-... python3 web/server.py --write
```

Docker の場合は `.env` に追加する。

| 環境変数 | 内容 |
|---|---|
| `GNUCASH_AI_API_KEY` | Anthropic の API キー（必須。未設定時は AIアシスタント機能全体が無効） |
| `GNUCASH_AI_MODEL` | 使用するモデル（既定 `claude-sonnet-5-5`） |

### 分析タブ：送信するデータ

GnuCash ファイルそのものや個々の取引明細は送信しない。送るのは選択した期間について集計した数値のみ：

- 収益合計・費用合計・純損益
- 収益・費用の科目別内訳（全科目）
- 現在の純資産（資産・負債の合計）

月ごとの内訳（推移）は毎回は送らず、AI が必要と判断したときだけツール呼び出し（`get_monthly_breakdown`）で追加取得する。

設定画面の「取引の説明・メモを考慮する」をオンにすると、その期間の取引の説明・メモ（金額・科目は含まない）も合わせて送信する（既定オフ）。件数の上限は期間が長いほど大きくし、直近の取引だけに偏らないようにしている（3ヶ月／6ヶ月／1年／今年／前年は最大200件、2年は最大400件、全期間は最大800件）。店名や個人名などが含まれる場合があるので注意。

### 分析タブ：分析対象期間

3ヶ月／6ヶ月／1年／2年／今年／前年／全期間から選択できる。「今年」「前年」は暦年（1月〜12月）、それ以外は当月を含む直近の期間（例：3ヶ月なら当月とその前2か月）。「2年」は前年の1月〜当月までで、今年・前年それぞれの収益合計・費用合計もあわせて渡すため、年ごとの比較を1回の質問で聞ける。

### 記帳タブ：取引の追加

「食費 500円」のように話しかけると、科目名を解決したうえで取引の記録を準備する。科目名が曖昧な場合（同名科目が複数ある・該当がない）は取引を作らず、候補を提示して確認を求める。確認した対応は MCP連携（後述）と同じ `mcp-mappings.json` に保存され、次回から自動解決できる。

**AIが`add_transaction`ツールを呼んでも、その時点ではまだ記帳されない。** チャット上に内容の確認カードが表示され、「記録する」を押したときだけ実際に取引が作られる（「キャンセル」で取り消せる）。分析タブとは別に、書き込み前に必ず人が確認する一手間を挟んでいる。

読み取り専用モード、または GnuCash デスクトップでロックされている間は記帳タブを使えない（分析タブは引き続き使える）。

### 会話の保存

会話はタブごとに別々のファイルに保存され（`data/` 配下、Git には入らない。分析タブは `ai-conversation.json`、記帳タブは `ai-ledger-conversation.json`）、画面を開くたびに復元される。無期限に増え続けないよう、保存のたびに直近20往復のみを残す（古いやり取りは自動的に削除され、画面にもその旨が表示される）。「会話をクリア」でいつでも削除できる。

### 回答の表示

回答は一度に返すのではなく、Claude からのストリーミング応答をそのままチャットのように逐次表示する。

### 料金について

質問・回答のたびに Anthropic API の利用料金が発生する（サーバー側に設定した `GNUCASH_AI_API_KEY` に課金される）。

### 分析タブ：Notion連携

分析タブから「この内容をNotionに書き込んで」のように頼むと、AIがNotionにページを作成・更新できる。Anthropic の Messages API の「MCPコネクタ」機能を使っており、Notionの公式リモートMCPサーバー（`https://mcp.notion.com/mcp`）にAnthropic側が直接接続する。GnuCash Web自体がNotionのAPIを呼ぶわけではない。

#### 有効にする

環境変数 `NOTION_MCP_TOKEN` を設定する。これはNotionの通常のインテグレーションシークレットとは別物の、**「MCPトークン」**という非対話用の静的トークン（Business・Enterpriseプランのみ、ワークスペースのオーナーがNotionのDeveloper portalで発行）。

```bash
GNUCASH_AI_API_KEY=sk-ant-... NOTION_MCP_TOKEN=ntn-mcp-... python3 web/server.py --write
```

未設定でもAIアシスタントの他の機能（分析タブ・記帳タブとも）には影響しない。Business・Enterpriseプラン以外では、この連携機能自体を使う手段がない（Notion公式のホスト型MCPサーバーは非対話での接続にMCPトークンが必須）。

#### 注意点

- **現時点では分析タブのみ**。記帳タブ（取引の追加）では使えない
- Notionへの書き込みは、**記帳タブのadd_transactionと違って画面での確認ステップを挟まない**。MCPコネクタはAnthropic側でツール呼び出しから実行まで完結するため、こちら側で止めて確認を求めることができない。システムプロンプトでは「明示的に頼まれたときだけ書き込む」よう指示しているが、確実な歯止めではない点に注意
- 書き込み先のページ・データベースの範囲は、Notion側でそのワークスペースに許可した権限（MCPトークンを使う場合はワークスペース全体）に従う

## MCP連携（外部のAIエージェントから取引を記録する）

`mcp-server/server.py` を使うと、Claude Desktop・Claude Code などの MCP クライアントから「食費 500円」のような指示で GnuCash Web に取引を追加できる。GnuCash Web 本体とは別プロセスで動く、標準ライブラリのみの MCP サーバー（stdio）。

### 有効にする

1. 画面右下の「設定」→「MCP連携」の「AIエージェントからの取引追加を許可する」をオンにする
2. 表示された「発行」でAPIキーを作る（名前は任意。例: `Claude Desktop`）。**キーはこのとき一度だけ表示される**ので控えておく
3. MCP クライアントの設定に `mcp-server/server.py` を登録する（Claude Code の場合の例）

   ```bash
   claude mcp add gnucash-web \
     --env GNUCASH_WEB_URL=http://127.0.0.1:8000 \
     --env GNUCASH_API_KEY=発行したキー \
     -- python3 /path/to/gnucash-web/mcp-server/server.py
   ```

| 環境変数（MCPサーバー側） | 内容 |
|---|---|
| `GNUCASH_WEB_URL` | GnuCash Web のURL（必須。例 `http://127.0.0.1:8000`） |
| `GNUCASH_API_KEY` | 設定画面で発行したAPIキー（必須） |
| `GNUCASH_MCP_DEFAULT_PAYMENT_ACCOUNT` | 支払い元・入金先を省略したときに使う科目のヒント（例 `現金`。省略可） |

キーは複数発行でき、使っているMCPサーバーごとに分けておくと、不要になったものだけ設定画面から個別に失効できる。生のキーはサーバーに保存されず（ソルト付きハッシュのみ保存）、発行直後の画面以外では二度と表示されない。「AIエージェントからの取引追加を許可する」をオフにすると、発行済みのキーを残したまま一時的に無効化できる。

### 科目名の解決と学習

「食費」のような文字列は、学習済みマッピング → 科目名の完全一致 → 一意な一致 → 部分一致（大文字小文字は区別しない）の順で勘定科目に解決する。例えば「paypayカード」でも「負債:クレジットカード:PayPayカード(JCB)」のように科目名の一部から推測できる。それでも候補が複数残る・該当がない場合は取引を作らず、候補一覧をエージェント経由でユーザーに確認する。確認した対応は `mcp-mappings.json`（`data/` 配下、Git には入らない）に保存され、次回から同じ文字列をそのまま自動解決できる。

## 構成

| ファイル | 内容 |
|---|---|
| `server.py` | HTTP サーバ + SQLite アクセス（API） |
| `auth.py` | パスキー（WebAuthn）認証。`GNUCASH_AUTH=1` のときだけ読み込まれる |
| `static/index.html` | 画面（勘定科目ツリー・登録簿・入力フォーム） |
| `mcp-server/server.py` | MCPサーバー（別プロセス）。AIエージェントから取引を追加する |

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
| GET | `/api/mcp/resolve-account?q=` | MCP連携用：科目名のヒントを勘定科目に解決する（学習済みマッピング→完全一致→一意一致→部分一致の順、大文字小文字は区別しない）。候補が複数・0件の場合は候補一覧を返す |
| POST | `/api/mcp/mappings` | MCP連携用：`{query, account}` の対応を記憶する（確認後の学習） |
| DELETE | `/api/mcp/mappings` | MCP連携用：`{query}` の対応を削除する |
| GET | `/api/mcp/keys` | MCP連携用APIキーの一覧（ハッシュ・生のキーは含まない） |
| POST | `/api/mcp/keys` | APIキーを発行する `{label}`。生のキーはこのレスポンスでのみ返る |
| DELETE | `/api/mcp/keys/{id}` | APIキーを失効させる |

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

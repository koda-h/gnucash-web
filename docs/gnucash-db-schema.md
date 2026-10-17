# GnuCash SQLite DB スキーマ

対象ファイル: `gnucash.gnucash`（SQLite 3 / GnuCash 2.6 系で作成 → 新しい版で再保存）
参照: [gnucash/gnucash](https://github.com/gnucash/gnucash) の `libgnucash/backend/sql/` 配下（`gnc-account-sql.cpp`, `gnc-transaction-sql.cpp`, `gnc-slots-sql.cpp`, `gnc-invoice-sql.cpp` など）

---

## 0. 全体を読むための前提知識

| 項目 | 内容 |
|---|---|
| **外部キー制約なし** | DDL 上は FK も多くの INDEX も定義されていない。関連はすべて GnuCash 本体（C++）側の論理的な参照。Web 版では JOIN 時に孤立データがあり得る前提で実装すること。 |
| **GUID** | 主キーは 32 桁の 16 進文字列（ハイフンなし UUID）。新規作成時は `uuid4().hex` 相当で生成する。 |
| **金額 = 有理数** | 金額・数量はすべて `*_num`（分子）/ `*_denom`（分母）の 2 カラム。実値 = `num / denom`。浮動小数点で保存しないこと。JPY なら通常 `denom = 1`、USD なら `100`。 |
| **日時** | `text(19)` で UTC 保存。**このファイルでは 2 形式が混在**：古い行は `YYYYMMDDHHMMSS`（14 桁, 12,810 件）、新しい行は `YYYY-MM-DD HH:MM:SS`（19 桁, 10,542 件）。`books` の slot `features/ISO-8601 formatted date strings...` が立っていると以降の書き込みは ISO 形式。読み込みは両対応必須。 |
| **日付のみ** | `text(8)` は `YYYYMMDD`（スケジュール取引・recurrence 等）。 |
| **post_date の時刻** | GnuCash 3.x 以降は取引日を「その日の 10:59:00 UTC」で保存する（タイムゾーンずれ対策）。古いデータは `ローカル 00:00 → UTC`（例: JST なら前日 15:00:00）。日付表示はタイムゾーン変換後に日付部分を使う。 |
| **真偽値** | `integer` の 0 / 1。 |
| **排他ロック** | `gnclock` に行がある間はデスクトップ版が開いている。Web 版で書き込む場合は尊重すべき。 |
| **拡張属性 (KVP)** | テーブルに無い属性は `slots` に Key-Value で格納（備考・勘定科目の色・請求書関連付けなど）。 |

### このファイルの実データ件数

| テーブル | 件数 | テーブル | 件数 |
|---|---:|---|---:|
| accounts | 330 | splits | 49,598 |
| transactions | 23,352 | slots | 20,586 |
| commodities | 2（JPY, template） | books | 1 |
| その他（budgets, prices, lots, schedxactions, ビジネス系） | 0 | | |

→ 当面の Web 版は **会計コア（books / accounts / commodities / transactions / splits / slots）** の実装が中心になる。

---

## 1. ER 図（会計コア）

```mermaid
erDiagram
    books ||--|| accounts : "root_account_guid（勘定ツリーのルート）"
    books ||--|| accounts : "root_template_guid（テンプレート用ルート）"
    accounts |o--o{ accounts : "parent_guid（親子階層）"
    commodities ||--o{ accounts : "commodity_guid（科目の通貨/銘柄）"
    commodities ||--o{ transactions : "currency_guid（取引通貨）"
    transactions ||--|{ splits : "tx_guid（仕訳明細）"
    accounts ||--o{ splits : "account_guid"
    accounts ||--o{ lots : "account_guid"
    lots |o--o{ splits : "lot_guid"
    commodities ||--o{ prices : "commodity_guid（対象）"
    commodities ||--o{ prices : "currency_guid（価格の通貨）"
    budgets ||--o{ budget_amounts : "budget_guid"
    accounts ||--o{ budget_amounts : "account_guid"
    accounts ||--o{ schedxactions : "template_act_guid（テンプレート科目）"

    books {
        text guid PK "帳簿 GUID（通常 1 行のみ）"
        text root_account_guid FK "ルート勘定科目（account_type=ROOT）"
        text root_template_guid FK "スケジュール取引テンプレートのルート科目"
    }

    accounts {
        text guid PK "勘定科目 GUID"
        text name "科目名（表示名）"
        text account_type "科目種別 ASSET/BANK/CASH/CREDIT/LIABILITY/EQUITY/INCOME/EXPENSE 等"
        text commodity_guid FK "この科目で扱う通貨・銘柄"
        integer commodity_scu "最小単位（Smallest Commodity Unit）例 JPY=1 USD=100"
        integer non_std_scu "1=commodity の既定と異なる SCU を使う"
        text parent_guid FK "親科目（ROOT のみ NULL）"
        text code "科目コード（任意・並び順に使われる）"
        text description "説明"
        integer hidden "1=非表示"
        integer placeholder "1=プレースホルダ（集計専用・直接記帳不可）"
    }

    commodities {
        text guid PK "コモディティ GUID"
        text namespace "名前空間 CURRENCY / 取引所名(NASDAQ等) / FUND / template"
        text mnemonic "略号 JPY, USD, AAPL など"
        text fullname "正式名称"
        text cusip "CUSIP / ISIN 等の識別コード"
        integer fraction "最小単位の分母 JPY=1 USD=100"
        integer quote_flag "1=オンライン相場取得対象"
        text quote_source "相場取得元（Finance::Quote のソース名）"
        text quote_tz "相場のタイムゾーン"
    }

    transactions {
        text guid PK "取引 GUID"
        text currency_guid FK "取引通貨（splits.value の通貨）"
        text num "番号（小切手番号・伝票番号など）"
        text post_date "取引日（UTC）"
        text enter_date "入力日時（UTC）"
        text description "摘要"
    }

    splits {
        text guid PK "スプリット（仕訳行）GUID"
        text tx_guid FK "所属取引"
        text account_guid FK "勘定科目"
        text memo "行メモ"
        text action "アクション（Buy/Sell/Deposit 等、自由入力）"
        text reconcile_state "照合状態 n=未照合 c=確認済 y=照合済 f=凍結 v=無効"
        text reconcile_date "照合日時"
        bigint value_num "金額 分子（取引通貨建て）"
        bigint value_denom "金額 分母"
        bigint quantity_num "数量 分子（科目の commodity 建て）"
        bigint quantity_denom "数量 分母"
        text lot_guid FK "所属ロット（株式・請求書消込用）"
    }

    lots {
        text guid PK "ロット GUID"
        text account_guid FK "所属科目"
        integer is_closed "1=クローズ（残高0）"
    }

    prices {
        text guid PK "価格 GUID"
        text commodity_guid FK "対象コモディティ（株式・外貨）"
        text currency_guid FK "価格の表示通貨"
        text date "価格日時（UTC）"
        text source "取得元 user:price-editor / Finance::Quote / user:xfer-dialog 等"
        text type "種別 last/bid/ask/nav/transaction/unknown"
        bigint value_num "価格 分子"
        bigint value_denom "価格 分母"
    }

    budgets {
        text guid PK "予算 GUID"
        text name "予算名"
        text description "説明"
        integer num_periods "期間数（例 12 か月）"
    }

    budget_amounts {
        integer id PK "連番"
        text budget_guid FK "予算"
        text account_guid FK "科目"
        integer period_num "期間番号（0 始まり）"
        bigint amount_num "予算額 分子"
        bigint amount_denom "予算額 分母"
    }

    schedxactions {
        text guid PK "スケジュール取引 GUID"
        text name "名称"
        integer enabled "1=有効"
        text start_date "開始日 YYYYMMDD"
        text end_date "終了日 YYYYMMDD"
        text last_occur "最終発生日 YYYYMMDD"
        integer num_occur "発生回数の上限（0=無制限）"
        integer rem_occur "残り回数"
        integer auto_create "1=自動作成"
        integer auto_notify "1=自動作成時に通知"
        integer adv_creation "何日前に作成するか"
        integer adv_notify "何日前に通知するか"
        integer instance_count "生成済みインスタンス数"
        text template_act_guid FK "テンプレート取引を保持する科目"
    }
```

### 複式簿記の核：transactions ↔ splits

- 1 件の **transaction（取引）** は 2 件以上の **split（仕訳行）** を持つ。
- 1 取引内の `value` の合計は **必ず 0**（借方 = 正、貸方 = 負）。
- `value` は取引通貨（`transactions.currency_guid`）建て、`quantity` は科目の通貨（`accounts.commodity_guid`）建て。同一通貨なら両者は同じ値。外貨や株式では `quantity / value` が為替レート・単価になる。
- 科目残高 = その科目の `splits.quantity` の合計。

**符号の慣例（GnuCash 内部値）**

| 科目種別 | 増加時の split 値 | 画面表示 |
|---|---|---|
| ASSET / BANK / CASH / STOCK / MUTUAL / RECEIVABLE / EXPENSE | 正（借方） | そのまま |
| LIABILITY / CREDIT / PAYABLE / EQUITY / INCOME | 負（貸方） | 通常は符号反転して表示（設定「残高の符号を反転」） |

**account_type 一覧**（`Account.h` の `GNCAccountType`）

| 値 | 意味 | 本ファイル件数 |
|---|---|---:|
| ROOT | ルート（不可視） | 2（通常＋テンプレート） |
| BANK | 銀行口座 | 19 |
| CASH | 現金 | 46 |
| ASSET | 資産 | 90 |
| CREDIT | クレジットカード | 39 |
| LIABILITY | 負債 | 9 |
| STOCK / MUTUAL | 株式 / 投資信託 | 0 |
| CURRENCY | 通貨取引（旧式） | 0 |
| INCOME | 収益 | 21 |
| EXPENSE | 費用 | 100 |
| EQUITY | 資本（純資産） | 2 |
| RECEIVABLE / PAYABLE | 売掛金 / 買掛金（ビジネス機能） | 2 / 0 |
| TRADING | 通貨売買勘定（トレーディング勘定） | 0 |

> 注: `books.root_template_guid` 配下の科目（commodity は `namespace=template`）はスケジュール取引のテンプレート用。通常の科目ツリー表示からは除外する。

---

## 2. ER 図（拡張属性・繰り返し・メタ情報）

```mermaid
erDiagram
    slots }o--|| ANY_OBJECT : "obj_guid（任意オブジェクトの GUID）"
    slots |o--o{ slots : "guid_val → 子 slots.obj_guid（ネストした FRAME）"
    recurrences }o--|| schedxactions : "obj_guid"
    recurrences }o--|| budgets : "obj_guid"

    ANY_OBJECT {
        text guid PK "books/accounts/transactions/splits/lots/invoices/customers 等の GUID"
    }

    slots {
        integer id PK "連番"
        text obj_guid "所有オブジェクトの GUID（または親 FRAME の guid_val）"
        text name "キー名。ネスト時は 親/子 のパス表記"
        integer slot_type "値の型 1=INT64 2=DOUBLE 3=NUMERIC 4=STRING 5=GUID 6=TIME64 8=GLIST 9=FRAME 10=GDATE"
        bigint int64_val "INT64 の値"
        text string_val "STRING の値"
        float8 double_val "DOUBLE の値"
        text timespec_val "TIME64 の値（日時）"
        text guid_val "GUID の値 / FRAME・GLIST の場合は子要素グループの ID"
        bigint numeric_val_num "NUMERIC 分子"
        bigint numeric_val_denom "NUMERIC 分母"
        text gdate_val "GDATE の値 YYYYMMDD"
    }

    recurrences {
        integer id PK "連番"
        text obj_guid FK "所有オブジェクト（schedxactions または budgets）"
        integer recurrence_mult "間隔の倍数（例 2 と week で隔週）"
        text recurrence_period_type "周期 once/day/week/month/end of month/nth weekday/last weekday/year"
        text recurrence_period_start "起点日 YYYYMMDD"
        text recurrence_weekend_adjust "週末調整 none/back/forward"
    }

    versions {
        text table_name PK "テーブル名、または Gnucash / Gnucash-Resave"
        integer table_version "テーブルのスキーマバージョン"
    }

    gnclock {
        varchar Hostname "ロック中のホスト名"
        int PID "ロック中のプロセス ID"
    }
```

### slots（KVP）の読み方

- `slot_type = 9 (FRAME)` の行は「フォルダ」。その `guid_val` を `obj_guid` に持つ行が子要素。`name` は `親/子` のフルパスで保存される。
  - 例（本ファイル）: `reconcile-info`(FRAME, guid_val=d8db…) → 子 `reconcile-info/last-date`, `reconcile-info/include-children`
- **本ファイルで使われているキー**

| name | 型 | 所有者 | 意味 |
|---|---|---|---|
| `date-posted` | GDATE | transactions | 取引日（日付のみ）。post_date のタイムゾーン問題回避用。20,537 件 |
| `notes` | STRING | transactions | 取引の備考（2 行目メモ） |
| `placeholder` | STRING | accounts | `"true"` で placeholder（旧形式の名残） |
| `balance-limit` | FRAME | accounts | 残高上限/下限アラート設定 |
| `reconcile-info/*` | FRAME/INT64 | accounts | 最終照合日・子科目を含めるか |
| `trans-txn-type` | STRING | transactions | 取引種別 `I`=請求書 `P`=支払 等 |
| `last-num` | STRING | accounts | 最後に使った伝票番号 |
| `features/*` | STRING | books | ファイルが要求する機能フラグ（古い GnuCash で開けなくする） |

- その他よく使われるキー: `color`（科目色）, `tax-related`, `equity-type`（期首残高科目）, `void-reason` / `void-time` / `void-former-amount`（取消取引）, `gncInvoice/invoice-guid`（取引⇔請求書）, `gncOwner/*`（ロットの取引先）, `title`（ロット名）, `assoc_uri`（添付リンク）, `options/*`（帳簿オプション）。

---

## 3. ER 図（ビジネス機能：請求書・取引先）

> 本ファイルでは全テーブル 0 件。将来対応用。
> `owner_type` / `billto_type` は `gncOwner.h` の `GncOwnerType`：0=NONE 1=UNDEFINED 2=CUSTOMER 3=JOB 4=VENDOR 5=EMPLOYEE。`owner_guid` はその種別に応じたテーブルを指す**ポリモーフィック参照**。

```mermaid
erDiagram
    customers ||--o{ invoices : "owner_guid（owner_type=2 請求書）"
    vendors ||--o{ invoices : "owner_guid（owner_type=4 支払請求書）"
    employees ||--o{ invoices : "owner_guid（owner_type=5 経費精算）"
    jobs ||--o{ invoices : "owner_guid（owner_type=3）"
    customers ||--o{ jobs : "owner_guid（owner_type=2）"
    vendors ||--o{ jobs : "owner_guid（owner_type=4）"
    customers ||--o{ orders : "owner_guid"
    invoices ||--o{ entries : "invoice（売上明細）"
    invoices ||--o{ entries : "bill（仕入/経費明細）"
    orders |o--o{ entries : "order_guid"
    billterms |o--o{ invoices : "terms"
    billterms |o--o{ customers : "terms"
    billterms |o--o{ vendors : "terms"
    billterms |o--o{ billterms : "parent"
    taxtables ||--|{ taxtable_entries : "taxtable"
    taxtables |o--o{ taxtables : "parent"
    taxtables |o--o{ customers : "taxtable"
    taxtables |o--o{ vendors : "tax_table"
    taxtables |o--o{ entries : "i_taxtable / b_taxtable"
    accounts ||--o{ taxtable_entries : "account（税金計上科目）"
    accounts |o--o{ entries : "i_acct / b_acct"
    accounts |o--o{ invoices : "post_acc（売掛/買掛科目）"
    accounts |o--o{ employees : "ccard_guid"
    transactions |o--o| invoices : "post_txn（転記取引）"
    lots |o--o| invoices : "post_lot（消込ロット）"
    commodities ||--o{ invoices : "currency"
    commodities ||--o{ customers : "currency"
    commodities ||--o{ vendors : "currency"
    commodities ||--o{ employees : "currency"

    customers {
        text guid PK "顧客 GUID"
        text name "顧客名"
        text id "顧客番号（表示用 ID）"
        text notes "備考"
        integer active "1=有効"
        bigint discount_num "既定割引率 分子"
        bigint discount_denom "既定割引率 分母"
        bigint credit_num "与信限度額 分子"
        bigint credit_denom "与信限度額 分母"
        text currency FK "取引通貨 commodities.guid"
        integer tax_override "1=税テーブルを上書き"
        text addr_name "請求先 宛名"
        text addr_addr1 "請求先 住所1（addr2〜4 も同様）"
        text addr_phone "請求先 電話"
        text addr_fax "請求先 FAX"
        text addr_email "請求先 メール"
        text shipaddr_name "配送先 宛名（shipaddr_* は addr_* と同構成）"
        text terms FK "支払条件 billterms.guid"
        integer tax_included "税込区分 1=YES 2=NO 3=USEGLOBAL"
        text taxtable FK "税テーブル taxtables.guid"
    }

    vendors {
        text guid PK "仕入先 GUID"
        text name "仕入先名"
        text id "仕入先番号"
        text notes "備考"
        text currency FK "取引通貨"
        integer active "1=有効"
        integer tax_override "1=税テーブル上書き"
        text addr_name "住所等（addr_* は customers と同構成）"
        text terms FK "支払条件"
        text tax_inc "税込区分（文字列）"
        text tax_table FK "税テーブル"
    }

    employees {
        text guid PK "従業員 GUID"
        text username "ユーザー名"
        text id "従業員番号"
        text language "言語"
        text acl "アクセス権（未使用）"
        integer active "1=有効"
        text currency FK "通貨"
        text ccard_guid FK "経費精算用クレジットカード科目"
        bigint workday_num "1 日の労働時間 分子"
        bigint workday_denom "1 日の労働時間 分母"
        bigint rate_num "時給 分子"
        bigint rate_denom "時給 分母"
        text addr_name "住所等（addr_*）"
    }

    jobs {
        text guid PK "ジョブ（案件）GUID"
        text id "ジョブ番号"
        text name "ジョブ名"
        text reference "参照番号"
        integer active "1=有効"
        integer owner_type "所有者種別 2=顧客 4=仕入先"
        text owner_guid "所有者 GUID"
    }

    orders {
        text guid PK "注文 GUID"
        text id "注文番号"
        text notes "備考"
        text reference "参照"
        integer active "1=有効"
        text date_opened "開始日時"
        text date_closed "終了日時"
        integer owner_type "所有者種別"
        text owner_guid "所有者 GUID"
    }

    invoices {
        text guid PK "請求書 GUID（請求書/支払請求書/経費精算 共通）"
        text id "請求書番号"
        text date_opened "作成日"
        text date_posted "転記日（NULL=未転記）"
        text notes "備考"
        integer active "1=有効"
        text currency FK "通貨"
        integer owner_type "所有者種別 2/3/4/5"
        text owner_guid "所有者 GUID"
        text terms FK "支払条件"
        text billing_id "請求 ID（先方参照番号）"
        text post_txn FK "転記で生成された取引"
        text post_lot FK "消込ロット"
        text post_acc FK "転記先 売掛金/買掛金 科目"
        integer billto_type "請求先種別（経費の付け替え先）"
        text billto_guid "請求先 GUID"
        bigint charge_amt_num "立替請求額 分子"
        bigint charge_amt_denom "立替請求額 分母"
    }

    entries {
        text guid PK "明細行 GUID"
        text date "明細日付"
        text date_entered "入力日時"
        text description "品名・内容"
        text action "単位/アクション（Hours, Material 等）"
        text notes "備考"
        bigint quantity_num "数量 分子"
        bigint quantity_denom "数量 分母"
        text i_acct FK "売上側 計上科目"
        bigint i_price_num "売上側 単価 分子"
        bigint i_price_denom "売上側 単価 分母"
        bigint i_discount_num "割引 分子"
        bigint i_discount_denom "割引 分母"
        text invoice FK "所属する請求書（売上）"
        text i_disc_type "割引タイミング PRETAX/SAMETIME/POSTTAX"
        text i_disc_how "割引方法 VALUE/PERCENT"
        integer i_taxable "1=課税"
        integer i_taxincluded "1=税込"
        text i_taxtable FK "売上側 税テーブル"
        text b_acct FK "仕入側 計上科目"
        bigint b_price_num "仕入側 単価 分子"
        bigint b_price_denom "仕入側 単価 分母"
        text bill FK "所属する支払請求書/経費精算"
        integer b_taxable "1=課税"
        integer b_taxincluded "1=税込"
        text b_taxtable FK "仕入側 税テーブル"
        integer b_paytype "支払方法 1=現金 2=クレジットカード（経費精算）"
        integer billable "1=顧客へ再請求可"
        integer billto_type "再請求先種別"
        text billto_guid "再請求先 GUID"
        text order_guid FK "所属する注文"
    }

    billterms {
        text guid PK "支払条件 GUID"
        text name "名称"
        text description "説明"
        integer refcount "参照数"
        integer invisible "1=非表示"
        text parent FK "親（コピー元）"
        text type "種別 GNC_TERM_TYPE_DAYS / GNC_TERM_TYPE_PROXIMO"
        integer duedays "支払期日（日数 または 翌月の日）"
        integer discountdays "早期支払割引の期限日数"
        bigint discount_num "早期支払割引率 分子"
        bigint discount_denom "早期支払割引率 分母"
        integer cutoff "締め日（PROXIMO 用）"
    }

    taxtables {
        text guid PK "税テーブル GUID"
        text name "名称"
        bigint refcount "参照数"
        integer invisible "1=非表示"
        text parent FK "親（コピー元）"
    }

    taxtable_entries {
        integer id PK "連番"
        text taxtable FK "所属税テーブル"
        text account FK "税金を計上する科目"
        bigint amount_num "税率または税額 分子"
        bigint amount_denom "税率または税額 分母"
        integer type "1=金額(VALUE) 2=率(PERCENT)"
    }
```

---

## 4. テーブル一覧（日本語サマリ）

| テーブル | 日本語名 | 役割 | versions |
|---|---|---|---:|
| `versions` | バージョン管理 | 各テーブルのスキーマ版と GnuCash 本体の版（`Gnucash`=2060000 → 2.6.0 系で作成） | – |
| `gnclock` | ロック | 同時オープン防止 | – |
| `books` | 帳簿 | ファイル全体のルート。科目ツリーとテンプレートツリーの起点 | 1 |
| `accounts` | 勘定科目 | 科目ツリー（`parent_guid` による隣接リスト） | 1 |
| `commodities` | 通貨・銘柄 | 通貨、株式、投資信託など | 1 |
| `transactions` | 取引 | 仕訳のヘッダ（日付・摘要・通貨） | 4 |
| `splits` | 仕訳明細 | 取引の各行（科目・金額・数量・照合状態） | 5 |
| `lots` | ロット | 株式の取得単位、請求書と支払の消込単位 | 2 |
| `prices` | 価格 | 株価・為替レートの履歴（Price DB） | 3 |
| `budgets` | 予算 | 予算のヘッダ | 1 |
| `budget_amounts` | 予算額 | 予算 × 科目 × 期間の金額 | 1 |
| `schedxactions` | スケジュール取引 | 定期取引の設定 | 1 |
| `recurrences` | 繰り返し規則 | スケジュール取引・予算の周期 | 2 |
| `slots` | 拡張属性 (KVP) | 任意オブジェクトの追加属性 | 4 |
| `customers` | 顧客 | ビジネス機能 | 2 |
| `vendors` | 仕入先 | ビジネス機能 | 1 |
| `employees` | 従業員 | ビジネス機能（経費精算） | 2 |
| `jobs` | ジョブ（案件） | 顧客/仕入先に紐づく案件 | 1 |
| `orders` | 注文 | ビジネス機能 | 1 |
| `invoices` | 請求書 | 請求書・支払請求書・経費精算書 共通 | 4 |
| `entries` | 請求明細 | 請求書の各行 | 4 |
| `billterms` | 支払条件 | 支払期日・早期割引 | 2 |
| `taxtables` | 税テーブル | 消費税等の定義 | 2 |
| `taxtable_entries` | 税テーブル明細 | 税率と計上科目 | 3 |

---

## 5. Web 版実装時の注意点

1. **金額は Decimal / 有理数で扱う**（JS なら `BigInt` で num/denom を保持、または decimal ライブラリ）。表示は `commodities.fraction` で丸める。
2. **日付パースは 2 形式対応**（`YYYYMMDDHHMMSS` と `YYYY-MM-DD HH:MM:SS`）。書き込み時は `books` の `features` slot に ISO-8601 フラグがあれば ISO 形式で書く。
3. **取引の保存は splits の value 合計 = 0 を検証**してから、トランザクション（DB の BEGIN/COMMIT）で transactions・splits・slots をまとめて書く。
4. **取引日を変更したら `slots.date-posted`（GDATE）も更新**する。
5. 科目ツリーは `WITH RECURSIVE` で `books.root_account_guid` から辿る。テンプレート側ツリーは除外。
6. 残高計算は `splits.quantity_num / quantity_denom` を科目ごとに SUM（既存 INDEX `splits_account_guid_index` が効く）。日付で絞る場合は `transactions.post_date` と JOIN。
7. 書き込み時は `gnclock` を確認し、デスクトップ版が開いている間は読み取り専用にするのが安全。
8. 未知の slot は消さずに保持する（デスクトップ版の機能が壊れるため）。

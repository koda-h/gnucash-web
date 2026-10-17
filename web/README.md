# GnuCash Web

GnuCash の SQLite ファイル（`.gnucash`）を直接読み書きする Web 版。Python 標準ライブラリのみで動く（追加インストール不要）。

## 起動

```bash
# 読み取り専用（既定・ファイルを一切変更しない）
python3 web/server.py gnucash.gnucash

# 編集可能（起動時に gnucash.gnucash.YYYYMMDDHHMMSS.bak を作成）
python3 web/server.py gnucash.gnucash --write

# GnuCash デスクトップのロック（gnclock）が残っていても書き込む
python3 web/server.py gnucash.gnucash --write --force
```

ブラウザで http://127.0.0.1:8000/ を開く（`--port` で変更可）。サーバは 127.0.0.1 のみで待ち受ける。

## 構成

| ファイル | 内容 |
|---|---|
| `server.py` | HTTP サーバ + SQLite アクセス（API） |
| `static/index.html` | 画面（勘定科目ツリー・登録簿・入力フォーム） |

## API

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/book` | 勘定科目ツリー・各科目の残高・説明文の候補・ロック状態 |
| GET | `/api/accounts/{guid}/register` | 科目の取引一覧（日付順・累計残高つき） |
| GET | `/api/quickfill?account=&desc=` | 同じ説明の直近取引から相手科目と金額を返す |
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

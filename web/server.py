#!/usr/bin/env python3
"""GnuCash の SQLite ファイルを読み書きする最小の Web サーバ（標準ライブラリのみ）。

使い方:
    python3 web/server.py                  # data/gnucash.gnucash を読み取り専用で開く
    python3 web/server.py --write          # 書き込み可（起動時に data/backup/ へバックアップを作成）
    python3 web/server.py other.gnucash    # 別のファイルを開く
    docker compose up                                # Docker で起動（compose.yaml を参照）

各オプションは環境変数 GNUCASH_FILE / GNUCASH_HOST / GNUCASH_PORT / GNUCASH_WRITE /
GNUCASH_FORCE / GNUCASH_BACKUP_DIR でも指定できる（Docker 用）。

パスキー認証（GNUCASH_AUTH=1）を使う場合は py_webauthn が必要（pip install -r requirements.txt）。
設定は GNUCASH_AUTH_RP_ID / GNUCASH_AUTH_ORIGIN / GNUCASH_AUTH_SESSION_DAYS / GNUCASH_AUTH_FILE。

AI分析機能（画面の「AI分析」）を使うには GNUCASH_AI_API_KEY（Anthropic の API キー）が必要。
モデルは GNUCASH_AI_MODEL で変更できる（既定 claude-sonnet-5-5）。未設定でもそれ以外の機能は使える。

スキーマは docs/gnucash-db-schema.md を参照。
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import threading
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
import urllib.error
import urllib.request

STATIC_DIR = Path(__file__).parent / "static"
# 既定のデータファイル（プロジェクト直下の data/。Git では管理しない）
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "gnucash.gnucash"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

ACCOUNT_TYPES = {"ASSET", "BANK", "CASH", "STOCK", "MUTUAL", "RECEIVABLE", "CURRENCY", "CREDIT",
                  "LIABILITY", "PAYABLE", "INCOME", "EXPENSE", "EQUITY", "TRADING"}

# post_date は 'YYYYMMDDHHMMSS' と 'YYYY-MM-DD HH:MM:SS' が混在するため、並べ替えには正規化した値を使う
NORM_POST_DATE = "replace(replace(replace(t.post_date,'-',''),' ',''),':','')"
NORM_ENTER_DATE = "replace(replace(replace(t.enter_date,'-',''),' ',''),':','')"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 変換ヘルパ

def parse_ts(s):
    """GnuCash の日時文字列（UTC）を aware datetime に変換。2 形式に対応。"""
    if not s:
        return None
    s = s.strip()
    fmt = "%Y%m%d%H%M%S" if len(s) == 14 and s.isdigit() else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)


def local_date(s):
    """post_date → ローカルタイムゾーンでの日付（GnuCash の表示と同じ）。"""
    d = parse_ts(s)
    return d.astimezone().date().isoformat() if d else None


def frac(num, denom):
    return Fraction(int(num), int(denom or 1))


def to_number(f, fraction):
    """表示用の数値。commodity の最小単位で丸める。"""
    digits = len(str(int(fraction))) - 1 if fraction else 0
    v = round(Fraction(f), digits)
    return int(v) if digits == 0 else float(v)


def new_guid():
    return uuid.uuid4().hex


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def month_range(frm, to):
    """"YYYY-MM" 同士の間の年月のリスト（両端含む）。統計・グラフ画面の期間指定用。"""
    if not MONTH_RE.match(frm or "") or not MONTH_RE.match(to or ""):
        raise ApiError(400, "期間が正しくありません")
    y, m = map(int, frm.split("-"))
    ty, tm = map(int, to.split("-"))
    months = []
    while (y, m) <= (ty, tm) and len(months) < 120:
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if not months:
        raise ApiError(400, "期間が正しくありません")
    return months


def month_bounds_utc(ym):
    """"YYYY-MM" の月初0時〜月末23:59:59.999999 を、NORM_POST_DATE と比較できる
    UTC の 'YYYYMMDDHHMMSS' 文字列にする（ローカルタイムゾーン基準、他の日付絞り込みと同じ扱い）"""
    y, m = map(int, ym.split("-"))
    d_from = date(y, m, 1)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    d_to = date(ny, nm, 1) - timedelta(days=1)
    return date_range_utc(d_from, d_to)


def date_range_utc(d_from, d_to):
    """ローカル日付の from〜to（両端含む）を、NORM_POST_DATE と比較できる UTC の
    'YYYYMMDDHHMMSS' 文字列にする。ローカルタイムゾーンへの変換で year が 1〜9999 の
    範囲を超えないよう、年の両端ぎりぎりの日付（AI分析の「全期間」など）は1日だけ内側に収める"""
    d_from = max(d_from, date(1, 1, 2))
    d_to = min(d_to, date(9999, 12, 30))
    start = datetime.combine(d_from, dtime.min).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    end = datetime.combine(d_to, dtime.max).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    return start, end


# ---------------------------------------------------------------- 帳簿アクセス

class Book:
    def __init__(self, path, writable, force):
        self.path = path
        self.writable = writable
        self.force = force
        self._mf_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._conversation_lock = threading.Lock()
        self._mcp_lock = threading.Lock()
        self._mcp_keys_lock = threading.Lock()

    def connect(self):
        if self.writable:
            con = sqlite3.connect(self.path, timeout=10)
        else:
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    # ---- 書式

    @staticmethod
    def iso_dates(con):
        """books の features slot に ISO-8601 フラグがあれば 'YYYY-MM-DD HH:MM:SS' で書く。"""
        row = con.execute(
            "select 1 from slots where name like 'features/ISO-8601%' limit 1").fetchone()
        return row is not None

    @staticmethod
    def fmt_ts(dt, iso):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S" if iso else "%Y%m%d%H%M%S")

    def locked_by(self, con):
        return [dict(r) for r in con.execute("select Hostname, PID from gnclock")]

    def ensure_indexes(self):
        """取引数が多いと enter_date/post_date の正規化式がフルスキャンになる（#59）ため、
        書き込み可（--write）で起動したときにインデックスを作っておく。読み取り専用では変更できないため何もしない。"""
        if not self.writable:
            return
        # CREATE INDEX の式にはテーブルエイリアス（t.）を書けないため、列名だけの式を使う
        norm_enter = NORM_ENTER_DATE.replace("t.", "")
        norm_post = NORM_POST_DATE.replace("t.", "")
        try:
            with self.connect() as con:
                con.execute(f"create index if not exists idx_tx_enter_date_norm on transactions ({norm_enter})")
                con.execute(f"create index if not exists idx_tx_post_date_norm on transactions ({norm_post})")
        except sqlite3.OperationalError as e:
            print(f"インデックス作成に失敗しました（続行します）: {e}", file=sys.stderr)

    # ---- 読み取り

    def book(self):
        with self.connect() as con:
            root, template_root = con.execute(
                "select root_account_guid, root_template_guid from books").fetchone()
            rows = con.execute("""
                select a.guid, a.name, a.account_type, a.parent_guid, a.placeholder, a.hidden,
                       a.description, a.code, a.commodity_guid,
                       c.mnemonic, c.fraction, c.namespace
                from accounts a left join commodities c on c.guid = a.commodity_guid
            """).fetchall()
            # 古い形式では placeholder が slot（文字列 "true"）に入っていることがある
            ph_slots = {r[0] for r in con.execute(
                "select obj_guid from slots where name='placeholder' and string_val='true'")}
            own, count = {}, {}
            for g, n, d, c in con.execute(
                    "select account_guid, sum(quantity_num), quantity_denom, count(*) from splits group by 1, 3"):
                own[g] = own.get(g, Fraction(0)) + frac(n, d)
                count[g] = count.get(g, 0) + c
            # group by + max() は取引全件を読む必要があり取引数が多いと遅い（#59）ため、
            # 日付の新しい順に一定件数だけ読んで重複を除く方式にする（NORM_POST_DATE のインデックスがあれば使われる）
            descs, seen = [], set()
            for (desc,) in con.execute(f"""
                    select t.description from transactions t where t.description <> ''
                    order by {NORM_POST_DATE} desc limit 5000"""):
                if desc not in seen:
                    seen.add(desc)
                    descs.append(desc)
                    if len(descs) >= 1000:
                        break
            currency = con.execute("""
                select c.mnemonic from transactions t join commodities c on c.guid = t.currency_guid
                group by 1 order by count(*) desc limit 1""").fetchone()
            commodities = [dict(r) for r in con.execute("""
                select guid, mnemonic, fraction from commodities
                where namespace in ('CURRENCY', 'ISO4217') order by mnemonic""")]
            locks = self.locked_by(con)

        by_parent = {}
        for r in rows:
            by_parent.setdefault(r["parent_guid"], []).append(r)
        for kids in by_parent.values():
            kids.sort(key=lambda r: ((r["code"] or ""), r["name"]))
        # 最上位は 資産 → 負債 → 収益 → 費用 → 純資産 の順に並べる
        order = ["ASSET", "BANK", "CASH", "STOCK", "MUTUAL", "RECEIVABLE", "LIABILITY", "CREDIT", "PAYABLE",
                 "INCOME", "EXPENSE", "EQUITY"]
        by_parent.get(root, []).sort(key=lambda r: (
            order.index(r["account_type"]) if r["account_type"] in order else len(order), r["code"] or "", r["name"]))

        # ルート配下を木の順に並べる（テンプレート用の木は除外）
        accounts = []

        def walk(parent, top):
            for r in by_parent.get(parent, []):
                fraction = r["fraction"] or 1
                accounts.append({
                    "guid": r["guid"],
                    "name": r["name"],
                    "type": r["account_type"],
                    "parent": None if top else parent,
                    "placeholder": bool(r["placeholder"]) or r["guid"] in ph_slots,
                    "hidden": bool(r["hidden"]),
                    "description": r["description"] or "",
                    "code": r["code"] or "",
                    "commodity": {"mnemonic": r["mnemonic"] or "", "fraction": fraction,
                                  "isCurrency": r["namespace"] in ("CURRENCY", "ISO4217")},
                    "balance": to_number(own.get(r["guid"], 0), fraction),
                    "count": count.get(r["guid"], 0),
                })
                walk(r["guid"], False)

        walk(root, True)
        return {
            "file": Path(self.path).name,
            "readOnly": not self.writable,
            "locked": locks,
            "writeBlocked": bool(locks) and not self.force,
            "currency": currency[0] if currency else "JPY",
            "accounts": accounts,
            "commodities": commodities,
            "descriptions": descs,
            "today": date.today().isoformat(),
        }

    def register(self, account):
        with self.connect() as con:
            acc = self._account(con, account)
            fraction = acc["fraction"] or 1
            rows = con.execute(f"""
                select t.guid, t.post_date, t.num, t.description,
                       s.quantity_num, s.quantity_denom, s.reconcile_state, s.memo,
                       {NORM_POST_DATE} as pd, {NORM_ENTER_DATE} as ed
                from splits s join transactions t on t.guid = s.tx_guid
                where s.account_guid = ?
            """, (account,)).fetchall()
            others, nsplits = {}, {}
            for tx, a in con.execute("""
                    select s.tx_guid, s.account_guid from splits s
                    where s.tx_guid in (select tx_guid from splits where account_guid = ?)""",
                                     (account,)):
                nsplits[tx] = nsplits.get(tx, 0) + 1
                if a != account:
                    others.setdefault(tx, set()).add(a)

        txs = {}
        for r in rows:  # 同じ取引に同じ科目の split が複数ある場合はまとめる
            t = txs.get(r["guid"])
            if t is None:
                t = txs[r["guid"]] = {
                    "guid": r["guid"], "date": local_date(r["post_date"]), "num": r["num"],
                    "desc": r["description"] or "", "memo": r["memo"] or "", "amt": Fraction(0),
                    "rec": "n", "key": (r["pd"] or "", r["ed"] or "")}
            t["amt"] += frac(r["quantity_num"], r["quantity_denom"])
            if r["reconcile_state"] == "y" or (r["reconcile_state"] == "c" and t["rec"] == "n"):
                t["rec"] = r["reconcile_state"]

        out, bal = [], Fraction(0)
        for t in sorted(txs.values(), key=lambda t: t["key"]):
            bal += t["amt"]
            o = others.get(t["guid"], set())
            # GnuCash と同様、split が 3 つ以上（または相手科目が 1 つに定まらない）なら分割取引として扱う
            simple = nsplits.get(t["guid"], 0) == 2 and len(o) == 1
            out.append({
                "guid": t["guid"], "date": t["date"], "num": t["num"], "desc": t["desc"] or t["memo"],
                "amt": to_number(t["amt"], fraction), "bal": to_number(bal, fraction),
                "other": next(iter(o)) if simple else None, "multi": not simple,
                "rec": t["rec"],
            })
        return {"account": account, "rows": out}

    TX_COLUMNS = """t.guid, t.post_date, t.enter_date, t.num, t.description, t.currency_guid, c.mnemonic, c.fraction"""
    SPLIT_COLUMNS = """s.tx_guid, s.guid, s.account_guid, s.memo, s.action, s.reconcile_state,
                       s.value_num, s.value_denom, s.quantity_num, s.quantity_denom,
                       a.commodity_guid, c.fraction as acc_fraction, s.lot_guid"""

    def transaction(self, tx):
        """取引 1 件の全 split。分割取引の展開表示・編集に使う。"""
        with self.connect() as con:
            t = con.execute(f"""
                select {self.TX_COLUMNS}
                from transactions t join commodities c on c.guid = t.currency_guid where t.guid = ?""",
                            (tx,)).fetchone()
            if t is None:
                raise ApiError(404, "取引が見つかりません")
            splits = con.execute(f"""
                select {self.SPLIT_COLUMNS}
                from splits s join accounts a on a.guid = s.account_guid
                left join commodities c on c.guid = a.commodity_guid
                where s.tx_guid = ? order by s.rowid""", (tx,)).fetchall()
            invoice = con.execute("select 1 from invoices where post_txn = ?", (tx,)).fetchone()
        return self._tx_payload(t, splits, invoice is not None)

    def recent(self, days):
        """入力日時（enter_date）が直近 days 日（今日を含む）の取引を、新しい順に返す。"""
        days = max(1, min(int(days), 366))
        since = datetime.combine(date.today() - timedelta(days=days - 1), dtime.min).astimezone()
        threshold = since.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
        where = f"{NORM_ENTER_DATE} >= ?"
        with self.connect() as con:
            txs = con.execute(f"""
                select {self.TX_COLUMNS}
                from transactions t join commodities c on c.guid = t.currency_guid
                where {where} order by {NORM_ENTER_DATE} desc limit 3000""", (threshold,)).fetchall()
            splits = {}
            for r in con.execute(f"""
                    select {self.SPLIT_COLUMNS}
                    from splits s join accounts a on a.guid = s.account_guid
                    left join commodities c on c.guid = a.commodity_guid
                    where s.tx_guid in (select t.guid from transactions t where {where})
                    order by s.rowid""", (threshold,)):
                splits.setdefault(r["tx_guid"], []).append(r)
            invoices = {r[0] for r in con.execute("select post_txn from invoices where post_txn is not null")}
        return {"days": days, "since": since.date().isoformat(),
                "rows": [self._tx_payload(t, splits.get(t["guid"], []), t["guid"] in invoices) for t in txs]}

    def income_statement(self, frm, to):
        """損益計算書：期間内（from〜to、両端含む・ローカル日付）の収益・費用科目ごとの
        自分自身の split 合計。非表示科目は集計対象から除く。親科目へのロールアップはフロント側で行う"""
        try:
            d_from, d_to = date.fromisoformat(frm), date.fromisoformat(to)
        except ValueError:
            raise ApiError(400, "期間が正しくありません")
        start, end = date_range_utc(d_from, d_to)
        with self.connect() as con:
            rows = con.execute(f"""
                select s.account_guid, s.quantity_num, s.quantity_denom, c.fraction
                from splits s
                join transactions t on t.guid = s.tx_guid
                join accounts a on a.guid = s.account_guid
                left join commodities c on c.guid = a.commodity_guid
                where {NORM_POST_DATE} between ? and ?
                  and a.account_type in ('INCOME', 'EXPENSE') and (a.hidden is null or a.hidden = 0)
            """, (start, end)).fetchall()
        own, fractions = {}, {}
        for r in rows:
            g = r["account_guid"]
            own[g] = own.get(g, Fraction(0)) + frac(r["quantity_num"], r["quantity_denom"])
            fractions[g] = r["fraction"] or 1
        balances = {g: to_number(v, fractions[g]) for g, v in own.items()}
        return {"from": frm, "to": to, "balances": balances}

    BALANCE_TYPES = {"ASSET", "BANK", "CASH", "STOCK", "MUTUAL", "RECEIVABLE", "CURRENCY",
                      "CREDIT", "LIABILITY", "PAYABLE"}

    def account_history(self, frm, to, account_guids):
        """統計・グラフ画面向け：指定した科目それぞれの月ごとの値（自分自身の split のみ）を返す。
        資産・負債系の科目は月末時点の残高（その科目の split の全履歴からの累計）、
        収益・費用系の科目（純資産などその他の種類も含む）はその月の split 合計（流量）。
        子科目のロールアップ・「純資産」の合算は、/api/book と同じ木構造・commodity 一致判定を
        使ってフロント側で行う"""
        months = month_range(frm, to)
        account_guids = list(dict.fromkeys(g for g in account_guids if g))   # 重複除去・順序維持
        if not account_guids:
            return {"months": months, "series": {}}
        with self.connect() as con:
            qmarks = ",".join("?" * len(account_guids))
            type_rows = con.execute(f"""
                select a.guid, a.account_type, c.fraction from accounts a
                left join commodities c on c.guid = a.commodity_guid
                where a.guid in ({qmarks})""", account_guids).fetchall()
            found = {r["guid"] for r in type_rows}
            if any(g not in found for g in account_guids):
                raise ApiError(404, "勘定科目が見つかりません")
            types = {r["guid"]: r["account_type"] for r in type_rows}
            fractions = {r["guid"]: r["fraction"] or 1 for r in type_rows}
            rows = con.execute(f"""
                select s.account_guid, s.quantity_num, s.quantity_denom, {NORM_POST_DATE} as pd
                from splits s join transactions t on t.guid = s.tx_guid
                where s.account_guid in ({qmarks})
                order by s.account_guid, pd""", account_guids).fetchall()
        by_acct = {}
        for r in rows:
            by_acct.setdefault(r["account_guid"], []).append(r)
        bounds = [month_bounds_utc(m) for m in months]
        series = {}
        for g in account_guids:
            splits, fraction, vals, idx = by_acct.get(g, []), fractions[g], [], 0
            if types[g] in self.BALANCE_TYPES:
                running = Fraction(0)
                for _, end in bounds:
                    while idx < len(splits) and splits[idx]["pd"] <= end:
                        running += frac(splits[idx]["quantity_num"], splits[idx]["quantity_denom"])
                        idx += 1
                    vals.append(to_number(running, fraction))
            else:
                for start, end in bounds:
                    while idx < len(splits) and splits[idx]["pd"] < start:
                        idx += 1
                    s = Fraction(0)
                    while idx < len(splits) and splits[idx]["pd"] <= end:
                        s += frac(splits[idx]["quantity_num"], splits[idx]["quantity_denom"])
                        idx += 1
                    vals.append(to_number(s, fraction))
            series[g] = vals
        return {"months": months, "series": series}

    def transactions_in_range(self, frm, to, limit=200):
        """AI分析（「取引の説明・メモを考慮する」設定がオンの場合向け）：指定期間の取引から、
        説明とメモだけを新しい順に返す。金額・科目・GUIDは含めない（AIに渡す情報を
        説明・メモのテキストだけに絞るため）"""
        try:
            d_from, d_to = date.fromisoformat(frm), date.fromisoformat(to)
        except ValueError:
            raise ApiError(400, "期間が正しくありません")
        limit = max(1, min(int(limit), 1000))
        start, end = date_range_utc(d_from, d_to)
        with self.connect() as con:
            tx_rows = con.execute(f"""
                select t.guid, t.description, t.post_date
                from transactions t
                where {NORM_POST_DATE} between ? and ?
                order by {NORM_POST_DATE} desc
                limit ?""", (start, end, limit)).fetchall()
            guids = [r["guid"] for r in tx_rows]
            memos = {}
            if guids:
                qmarks = ",".join("?" * len(guids))
                for r in con.execute(f"""
                        select tx_guid, memo from splits
                        where tx_guid in ({qmarks}) and memo is not null and memo != ''""", guids):
                    memos.setdefault(r["tx_guid"], set()).add(r["memo"])
        rows = [{"date": local_date(r["post_date"]), "desc": r["description"] or "",
                 "memos": sorted(memos.get(r["guid"], []))} for r in tx_rows]
        return {"from": frm, "to": to, "count": len(rows), "rows": rows}

    def search_transactions(self, q, limit=200):
        """取引検索：期間を問わず全取引から、説明またはいずれかの分割のメモに q を含むものを
        新しい順に返す（大文字小文字は区別しない。SQLite の LIKE の既定動作）。"""
        q = (q or "").strip()
        if not q:
            return {"q": q, "rows": []}
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.connect() as con:
            tx_rows = con.execute(f"""
                select t.guid
                from transactions t
                where t.description like ? escape '\\'
                   or exists (select 1 from splits s where s.tx_guid = t.guid and s.memo like ? escape '\\')
                order by {NORM_POST_DATE} desc, {NORM_ENTER_DATE} desc
                limit ?""", (like, like, limit)).fetchall()
            guids = [r["guid"] for r in tx_rows]
            if not guids:
                return {"q": q, "rows": []}
            qmarks = ",".join("?" * len(guids))
            tx_by_guid = {t["guid"]: t for t in con.execute(f"""
                select {self.TX_COLUMNS}
                from transactions t join commodities c on c.guid = t.currency_guid
                where t.guid in ({qmarks})""", guids)}
            splits = {}
            for r in con.execute(f"""
                    select {self.SPLIT_COLUMNS}
                    from splits s join accounts a on a.guid = s.account_guid
                    left join commodities c on c.guid = a.commodity_guid
                    where s.tx_guid in ({qmarks})
                    order by s.rowid""", guids):
                splits.setdefault(r["tx_guid"], []).append(r)
            invoices = {r[0] for r in con.execute(
                f"select post_txn from invoices where post_txn in ({qmarks})", guids)}
        ordered = [tx_by_guid[g] for g in guids if g in tx_by_guid]
        return {"q": q, "rows": [self._tx_payload(t, splits.get(t["guid"], []), t["guid"] in invoices)
                                  for t in ordered]}

    def _tx_payload(self, t, splits, has_invoice):
        fraction = t["fraction"] or 1
        enter = parse_ts(t["enter_date"])
        reason = None
        if any(s["reconcile_state"] == "y" for s in splits):
            reason = "照合済みの分割を含むため編集できません"
        elif any(s["lot_guid"] for s in splits) or has_invoice:
            reason = "請求書・ロットに関連付いた取引はまだ編集できません"
        elif any(s["commodity_guid"] != t["currency_guid"] for s in splits):
            reason = "通貨・銘柄の異なる科目を含む取引の分割編集にはまだ対応していません"
        return {
            "guid": t["guid"], "date": local_date(t["post_date"]), "num": t["num"], "desc": t["description"] or "",
            "enter": enter.astimezone().strftime("%Y-%m-%dT%H:%M") if enter else None,
            "currency": {"mnemonic": t["mnemonic"], "fraction": fraction},
            "editable": reason is None, "reason": reason,
            "splits": [{
                "guid": s["guid"], "account": s["account_guid"], "memo": s["memo"] or "", "action": s["action"] or "",
                "rec": s["reconcile_state"],
                "value": to_number(frac(s["value_num"], s["value_denom"]), fraction),
                "quantity": to_number(frac(s["quantity_num"], s["quantity_denom"]), s["acc_fraction"] or 1),
            } for s in splits],
        }

    def quickfill_pair(self, desc):
        """同じ説明の直近（入力日時順）の 2 科目の取引から、出金元（貸方）・入金先（借方）・金額を返す。"""
        with self.connect() as con:
            for (tx,) in con.execute(f"""
                    select t.guid from transactions t where t.description = ?
                    order by {NORM_ENTER_DATE} desc limit 20""", (desc,)):
                splits = con.execute("""
                    select s.account_guid, s.value_num, s.value_denom, c.fraction
                    from splits s join transactions t on t.guid = s.tx_guid
                    join commodities c on c.guid = t.currency_guid where s.tx_guid = ?""", (tx,)).fetchall()
                if len(splits) != 2 or splits[0][0] == splits[1][0]:
                    continue
                credit = next((sp for sp in splits if sp[1] < 0), None)
                if credit is None:
                    continue
                debit = next(sp for sp in splits if sp is not credit)
                return {"from": credit[0], "to": debit[0],
                        "amount": to_number(abs(frac(credit[1], credit[2])), credit[3] or 1)}
        return {}

    def quickfill(self, account, desc):
        """同じ説明の直近の取引から、相手科目と金額を返す（GnuCash の QuickFill 相当）。"""
        with self.connect() as con:
            for (tx,) in con.execute(f"""
                    select t.guid from transactions t
                    where t.description = ? and exists
                      (select 1 from splits where tx_guid = t.guid and account_guid = ?)
                    order by {NORM_POST_DATE} desc limit 20""", (desc, account)):
                splits = con.execute(
                    "select account_guid, quantity_num, quantity_denom from splits where tx_guid = ?",
                    (tx,)).fetchall()
                if len(splits) == 2 and splits[0][0] != splits[1][0]:
                    mine = next(s for s in splits if s[0] == account)
                    other = next(s for s in splits if s[0] != account)
                    fraction = self._account(con, account)["fraction"] or 1
                    return {"other": other[0], "amount": to_number(frac(mine[1], mine[2]), fraction)}
        return {}

    # ---- 書き込み

    def _account(self, con, guid):
        r = con.execute("""
            select a.guid, a.name, a.placeholder, a.commodity_guid, c.fraction, c.namespace
            from accounts a left join commodities c on c.guid = a.commodity_guid
            where a.guid = ?""", (guid,)).fetchone()
        if r is None:
            raise ApiError(404, "勘定科目が見つかりません")
        return r

    def _writable(self, con):
        if not self.writable:
            raise ApiError(403, "読み取り専用モードです。--write を付けて起動すると編集できます")
        locks = self.locked_by(con)
        if locks and not self.force:
            who = ", ".join(f"{l['Hostname']} (PID {l['PID']})" for l in locks)
            raise ApiError(409, f"GnuCash デスクトップで開かれています（{who}）。閉じてから操作するか --force で起動してください")

    def _descendant_guids(self, con, guid):
        """guid の子孫科目 guid の集合（guid 自身は含まない）。循環参照チェックに使う。"""
        children = {}
        for g, p in con.execute("select guid, parent_guid from accounts"):
            children.setdefault(p, []).append(g)
        out, stack = set(), [guid]
        while stack:
            for c in children.get(stack.pop(), []):
                if c not in out:
                    out.add(c)
                    stack.append(c)
        return out

    def _split_count(self, con, guid):
        return con.execute("select count(*) from splits where account_guid = ?", (guid,)).fetchone()[0]

    def _root_guid(self, con):
        return con.execute("select root_account_guid from books").fetchone()[0]

    def _validate_account_body(self, con, body, existing_guid=None):
        name = str(body.get("name", "")).strip()
        if not name:
            raise ApiError(400, "科目名を入力してください")
        type_ = body.get("type")
        if type_ not in ACCOUNT_TYPES:
            raise ApiError(400, "科目の種類が正しくありません")
        # 「最上位」（parent 未指定）は帳簿の ROOT 科目の子として作る
        parent = body.get("parent") or self._root_guid(con)
        if con.execute("select 1 from accounts where guid = ?", (parent,)).fetchone() is None:
            raise ApiError(400, "親科目が見つかりません")
        if existing_guid and (parent == existing_guid or parent in self._descendant_guids(con, existing_guid)):
            raise ApiError(400, "自分自身、またはその子科目を親には選べません")
        commodity = con.execute(
            "select guid, mnemonic, fraction, namespace from commodities where guid = ?",
            (body.get("commodity"),)).fetchone()
        if commodity is None or commodity["namespace"] not in ("CURRENCY", "ISO4217"):
            raise ApiError(400, "通貨が正しくありません")
        return {
            "name": name, "type": type_, "parent": parent, "commodity": commodity,
            "placeholder": bool(body.get("placeholder")), "hidden": bool(body.get("hidden")),
            "code": str(body.get("code", "")).strip(), "description": str(body.get("description", "")).strip(),
        }

    def create_account(self, body):
        with self.connect() as con:
            self._writable(con)
            v = self._validate_account_body(con, body)
            guid = new_guid()
            con.execute("""
                insert into accounts (guid, name, account_type, commodity_guid, commodity_scu, non_std_scu,
                                       parent_guid, code, description, hidden, placeholder)
                values (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)""",
                        (guid, v["name"], v["type"], v["commodity"]["guid"], v["commodity"]["fraction"] or 1,
                         v["parent"], v["code"], v["description"], int(v["hidden"]), int(v["placeholder"])))
        return {"guid": guid}

    def update_account(self, guid, body):
        with self.connect() as con:
            self._writable(con)
            acc = self._account(con, guid)
            v = self._validate_account_body(con, body, existing_guid=guid)
            has_tx = self._split_count(con, guid) > 0
            if has_tx and v["commodity"]["guid"] != acc["commodity_guid"]:
                raise ApiError(400, "取引がある科目は通貨を変更できません")
            if has_tx and v["placeholder"]:
                raise ApiError(400, "取引がある科目は集計用にできません")
            con.execute("""
                update accounts set name = ?, account_type = ?, commodity_guid = ?, commodity_scu = ?,
                                     non_std_scu = 0, parent_guid = ?, code = ?, description = ?,
                                     hidden = ?, placeholder = ? where guid = ?""",
                        (v["name"], v["type"], v["commodity"]["guid"], v["commodity"]["fraction"] or 1,
                         v["parent"], v["code"], v["description"], int(v["hidden"]), int(v["placeholder"]), guid))
        return {"guid": guid}

    def delete_account(self, guid):
        with self.connect() as con:
            self._writable(con)
            self._account(con, guid)
            if con.execute("select 1 from accounts where parent_guid = ? limit 1", (guid,)).fetchone():
                raise ApiError(400, "子科目があるため削除できません。先に子科目を削除するか、別の親に移動してください")
            if self._split_count(con, guid) > 0:
                raise ApiError(400, "取引が残っているため削除できません。先に取引を削除するか、別の科目に移動してください")
            con.execute("delete from slots where obj_guid = ?", (guid,))
            con.execute("delete from accounts where guid = ?", (guid,))
        return {"guid": guid}

    def _validate(self, con, body, need_amount):
        try:
            d = date.fromisoformat(body.get("date", ""))
        except ValueError:
            raise ApiError(400, "日付が正しくありません")
        desc = str(body.get("desc", "")).strip()
        if not need_amount:
            return d, desc, None, None, None
        mine = self._account(con, body.get("account"))
        other = self._account(con, body.get("other"))
        if mine["guid"] == other["guid"]:
            raise ApiError(400, "同じ勘定科目は選べません")
        if mine["placeholder"] or other["placeholder"]:
            raise ApiError(400, "集計用（プレースホルダ）の科目には記録できません")
        if mine["commodity_guid"] != other["commodity_guid"]:
            raise ApiError(400, "通貨・銘柄の異なる科目間の取引にはまだ対応していません")
        if mine["namespace"] not in ("CURRENCY", "ISO4217"):
            raise ApiError(400, "通貨以外（株式など）の科目への記帳にはまだ対応していません")
        fraction = mine["fraction"] or 1
        try:
            scaled = Decimal(str(body.get("amount"))) * fraction
        except InvalidOperation:
            raise ApiError(400, "金額が正しくありません")
        if scaled != scaled.to_integral_value():
            raise ApiError(400, f"金額の桁数が多すぎます（最小単位 1/{fraction}）")
        num = int(scaled)
        if num == 0:
            raise ApiError(400, "金額を入力してください")
        return d, desc, mine, other, (num, fraction)

    def _validate_splits(self, con, lines):
        """分割行 [{guid?, account, memo, amount}] を検証し (commodity_guid, fraction, [(line, num)]) を返す。"""
        if not isinstance(lines, list) or len(lines) < 2:
            raise ApiError(400, "分割は 2 行以上必要です")
        accts = [self._account(con, l.get("account")) for l in lines]
        commodity = accts[0]["commodity_guid"]
        for a in accts:
            if a["placeholder"]:
                raise ApiError(400, f"集計用（プレースホルダ）の科目「{a['name']}」には記録できません")
            if a["commodity_guid"] != commodity:
                raise ApiError(400, "通貨・銘柄の異なる科目を含む分割にはまだ対応していません")
        if accts[0]["namespace"] not in ("CURRENCY", "ISO4217"):
            raise ApiError(400, "通貨以外（株式など）の科目への記帳にはまだ対応していません")
        fraction = accts[0]["fraction"] or 1
        out = []
        for l in lines:
            try:
                scaled = Decimal(str(l.get("amount", "0") or "0")) * fraction
            except InvalidOperation:
                raise ApiError(400, "金額が正しくありません")
            if scaled != scaled.to_integral_value():
                raise ApiError(400, f"金額の桁数が多すぎます（最小単位 1/{fraction}）")
            out.append((l, int(scaled)))
        total = sum(n for _, n in out)
        if total != 0:
            raise ApiError(400, f"貸借が一致していません（差額 {Fraction(total, fraction)}）")
        if all(n == 0 for _, n in out):
            raise ApiError(400, "金額を入力してください")
        return commodity, fraction, out

    def _insert_split(self, con, tx, account, memo, num, fraction, iso):
        con.execute("""
            insert into splits (guid, tx_guid, account_guid, memo, action, reconcile_state,
                                reconcile_date, value_num, value_denom, quantity_num, quantity_denom, lot_guid)
            values (?, ?, ?, ?, '', 'n', ?, ?, ?, ?, ?, NULL)""",
                    (new_guid(), tx, account, memo, self.fmt_ts(EPOCH, iso), num, fraction, num, fraction))

    def _post_date(self, d, iso):
        # GnuCash 3.x 以降は取引日を「その日の 10:59:00 UTC」で保存する
        return self.fmt_ts(datetime(d.year, d.month, d.day, 10, 59, tzinfo=timezone.utc), iso)

    def _set_date_posted_slot(self, con, tx, d):
        cur = con.execute(
            "update slots set gdate_val = ? where obj_guid = ? and name = 'date-posted'",
            (d.strftime("%Y%m%d"), tx))
        if cur.rowcount == 0:
            con.execute("""
                insert into slots (obj_guid, name, slot_type, int64_val, string_val, double_val,
                                   timespec_val, guid_val, numeric_val_num, numeric_val_denom, gdate_val)
                values (?, 'date-posted', 10, 0, NULL, NULL, ?, NULL, 0, 1, ?)""",
                        (tx, self.fmt_ts(EPOCH, self.iso_dates(con)), d.strftime("%Y%m%d")))

    def _guard_tx(self, con, tx):
        if con.execute("select 1 from transactions where guid = ?", (tx,)).fetchone() is None:
            raise ApiError(404, "取引が見つかりません")
        if con.execute("select 1 from splits where tx_guid = ? and reconcile_state = 'y'", (tx,)).fetchone():
            raise ApiError(400, "照合済みの取引は編集・削除できません")
        if con.execute("select 1 from splits where tx_guid = ? and lot_guid is not null", (tx,)).fetchone() \
                or con.execute("select 1 from invoices where post_txn = ?", (tx,)).fetchone():
            raise ApiError(400, "請求書・ロットに関連付いた取引はまだ編集できません")

    def create(self, body):
        """body: {account, other, amount} の単純な取引、または {splits: [...]} の分割取引。"""
        with self.connect() as con:
            self._writable(con)
            if "splits" in body:
                d, desc, *_ = self._validate(con, body, False)
                commodity, fraction, lines = self._validate_splits(con, body["splits"])
                lines = [(l["account"], str(l.get("memo", "")), n) for l, n in lines]
            else:
                d, desc, mine, other, (num, fraction) = self._validate(con, body, True)
                commodity = mine["commodity_guid"]
                lines = [(mine["guid"], "", num), (other["guid"], "", -num)]
            iso = self.iso_dates(con)
            now = self.fmt_ts(datetime.now(timezone.utc), iso)
            tx = new_guid()
            con.execute("""insert into transactions (guid, currency_guid, num, post_date, enter_date, description)
                           values (?, ?, '', ?, ?, ?)""",
                        (tx, commodity, self._post_date(d, iso), now, desc))
            for acc, memo, n in lines:
                self._insert_split(con, tx, acc, memo, n, fraction, iso)
            self._set_date_posted_slot(con, tx, d)
        return {"guid": tx}

    def update(self, tx, body):
        with self.connect() as con:
            self._writable(con)
            self._guard_tx(con, tx)
            if "splits" in body:
                return self._update_splits(con, tx, body)
            splits = con.execute("select guid, account_guid from splits where tx_guid = ?", (tx,)).fetchall()
            account = body.get("account")
            simple = len(splits) == 2 and sum(s["account_guid"] == account for s in splits) == 1
            with_amount = simple and "amount" in body
            d, desc, mine, other, amt = self._validate(con, body, with_amount)
            iso = self.iso_dates(con)
            con.execute("update transactions set post_date = ?, description = ? where guid = ?",
                        (self._post_date(d, iso), desc, tx))
            self._set_date_posted_slot(con, tx, d)
            if with_amount:
                num, fraction = amt
                cur = con.execute("select currency_guid from transactions where guid = ?", (tx,)).fetchone()[0]
                if cur != mine["commodity_guid"]:
                    raise ApiError(400, "外貨建ての取引の金額変更にはまだ対応していません")
                my_split = next(s for s in splits if s["account_guid"] == account)
                other_split = next(s for s in splits if s["account_guid"] != account)
                con.execute("""update splits set value_num = ?, value_denom = ?, quantity_num = ?, quantity_denom = ?
                               where guid = ?""", (num, fraction, num, fraction, my_split["guid"]))
                con.execute("""update splits set account_guid = ?, value_num = ?, value_denom = ?,
                               quantity_num = ?, quantity_denom = ? where guid = ?""",
                            (other["guid"], -num, fraction, -num, fraction, other_split["guid"]))
        return {"guid": tx}

    def _update_splits(self, con, tx, body):
        """分割行で取引を置き換える。guid のある行は更新、ない行は追加、送られなかった既存行は削除。"""
        d, desc, *_ = self._validate(con, body, False)
        commodity, fraction, lines = self._validate_splits(con, body["splits"])
        cur = con.execute("select currency_guid from transactions where guid = ?", (tx,)).fetchone()[0]
        if cur != commodity:
            raise ApiError(400, "外貨建ての取引の分割編集にはまだ対応していません")
        existing = {r[0] for r in con.execute("select guid from splits where tx_guid = ?", (tx,))}
        keep = {l.get("guid") for l, _ in lines if l.get("guid")}
        if keep - existing:
            raise ApiError(400, "この取引に属さない分割が含まれています")
        iso = self.iso_dates(con)
        con.execute("update transactions set post_date = ?, description = ? where guid = ?",
                    (self._post_date(d, iso), desc, tx))
        self._set_date_posted_slot(con, tx, d)
        for g in existing - keep:
            for slot in self._slot_rows(con, g):
                con.execute("delete from slots where id = ?", (slot["id"],))
            con.execute("delete from splits where guid = ?", (g,))
        for l, n in lines:
            if l.get("guid"):
                con.execute("""update splits set account_guid = ?, memo = ?, value_num = ?, value_denom = ?,
                               quantity_num = ?, quantity_denom = ? where guid = ?""",
                            (l["account"], str(l.get("memo", "")), n, fraction, n, fraction, l["guid"]))
            else:
                self._insert_split(con, tx, l["account"], str(l.get("memo", "")), n, fraction, iso)
        return {"guid": tx}

    def copy(self, tx, body):
        """取引を複製する（GnuCash の「取引の複製」相当）。全 split をそのまま写し、日付だけ指定日にする。"""
        try:
            d = date.fromisoformat(body.get("date") or date.today().isoformat())
        except ValueError:
            raise ApiError(400, "日付が正しくありません")
        with self.connect() as con:
            self._writable(con)
            t = con.execute("select * from transactions where guid = ?", (tx,)).fetchone()
            if t is None:
                raise ApiError(404, "取引が見つかりません")
            splits = con.execute("select * from splits where tx_guid = ? order by rowid", (tx,)).fetchall()
            iso = self.iso_dates(con)
            new = new_guid()
            # 番号（num）は引き継がない。照合状態・ロットはリセットする
            con.execute("""insert into transactions (guid, currency_guid, num, post_date, enter_date, description)
                           values (?, ?, '', ?, ?, ?)""",
                        (new, t["currency_guid"], self._post_date(d, iso),
                         self.fmt_ts(datetime.now(timezone.utc), iso), t["description"]))
            for sp in splits:
                con.execute("""
                    insert into splits (guid, tx_guid, account_guid, memo, action, reconcile_state,
                                        reconcile_date, value_num, value_denom, quantity_num, quantity_denom, lot_guid)
                    values (?, ?, ?, ?, ?, 'n', ?, ?, ?, ?, ?, NULL)""",
                            (new_guid(), new, sp["account_guid"], sp["memo"], sp["action"], self.fmt_ts(EPOCH, iso),
                             sp["value_num"], sp["value_denom"], sp["quantity_num"], sp["quantity_denom"]))
            self._set_date_posted_slot(con, new, d)
            # 取引の備考（notes）も引き継ぐ
            for (note,) in con.execute(
                    "select string_val from slots where obj_guid = ? and name = 'notes' and slot_type = 4", (tx,)):
                con.execute("""insert into slots (obj_guid, name, slot_type, int64_val, string_val, double_val,
                                                timespec_val, guid_val, numeric_val_num, numeric_val_denom, gdate_val)
                               values (?, 'notes', 4, 0, ?, NULL, NULL, NULL, 0, 1, NULL)""", (new, note))
        return {"guid": new}

    def _slot_rows(self, con, obj_guid):
        """obj_guid の slot と、FRAME / GLIST でぶら下がる子 slot をすべて集める。"""
        rows = [dict(r) for r in con.execute("select * from slots where obj_guid = ?", (obj_guid,))]
        for r in list(rows):
            if r["slot_type"] in (8, 9) and r["guid_val"]:
                rows += self._slot_rows(con, r["guid_val"])
        return rows

    def delete(self, tx):
        with self.connect() as con:
            self._writable(con)
            self._guard_tx(con, tx)
            t = dict(con.execute("select * from transactions where guid = ?", (tx,)).fetchone())
            splits = [dict(r) for r in con.execute("select * from splits where tx_guid = ?", (tx,))]
            slots = self._slot_rows(con, tx)
            for s in splits:
                slots += self._slot_rows(con, s["guid"])
            con.executemany("delete from slots where id = ?", [(s["id"],) for s in slots])
            con.execute("delete from splits where tx_guid = ?", (tx,))
            con.execute("delete from transactions where guid = ?", (tx,))
        return {"guid": tx, "snapshot": {"transaction": t, "splits": splits, "slots": slots}}

    def restore(self, snap):
        """delete が返したスナップショットをそのまま書き戻す（元に戻す用）。"""
        with self.connect() as con:
            self._writable(con)
            for table, rows in (("transactions", [snap["transaction"]]),
                                ("splits", snap["splits"]), ("slots", snap["slots"])):
                cols = {r[1] for r in con.execute(f"pragma table_info({table})")}
                for row in rows:
                    keys = [k for k in row if k in cols and not (table == "slots" and k == "id")]
                    con.execute(f"insert into {table} ({', '.join(keys)}) values ({', '.join('?' * len(keys))})",
                                [row[k] for k in keys])
        return {"guid": snap["transaction"]["guid"]}

    # ---- マネーフォワード ME 取り込み：科目マッピング・取り込み済みIDの永続化
    # GnuCash の SQLite とは別に、同じフォルダの mf-import.json に保存する（auth.json と同じ置き場所の流儀）。
    # CSVの解析・振替の突き合わせ・マッピング適用はフロント側（JS）で行い、ここではマッピングの保存と、
    # フロントが組み立てた取引（2行split）の一括作成・取り込み済みID の記録だけを受け持つ

    def _mf_import_path(self):
        return Path(self.path).parent / "mf-import.json"

    def _load_mf_import(self):
        p = self._mf_import_path()
        if not p.exists():
            return {"inst": {}, "cat": {}, "desc": {}, "ids": []}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"inst": {}, "cat": {}, "desc": {}, "ids": []}
        return {"inst": data.get("inst") or {}, "cat": data.get("cat") or {}, "desc": data.get("desc") or {},
                "ids": data.get("ids") or []}

    def _save_mf_import(self, data):
        p = self._mf_import_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def mf_import_mappings(self):
        with self._mf_lock:
            d = self._load_mf_import()
        return {"inst": d["inst"], "cat": d["cat"], "desc": d["desc"]}

    def mf_import_ids(self):
        with self._mf_lock:
            d = self._load_mf_import()
        return {"ids": d["ids"]}

    def mf_set_mapping(self, kind, key, account_guid):
        """保有金融機関（inst）・カテゴリ（cat）・内容（desc、保有金融機関+内容の完全一致）
        1件分のマッピング先勘定科目を保存する。"""
        if kind not in ("inst", "cat", "desc"):
            raise ApiError(400, "種類が正しくありません")
        if not key or not isinstance(key, str):
            raise ApiError(400, "キーが正しくありません")
        with self.connect() as con:
            a = self._account(con, account_guid)
            if a["placeholder"]:
                raise ApiError(400, f"集計用（プレースホルダ）の科目「{a['name']}」は選べません")
        with self._mf_lock:
            d = self._load_mf_import()
            d[kind][key] = account_guid
            self._save_mf_import(d)
        return {"ok": True}

    def mf_delete_mapping(self, kind, key):
        """mf_set_mapping で保存したマッピングを取り消す（未設定に戻す）。"""
        if kind not in ("inst", "cat", "desc"):
            raise ApiError(400, "種類が正しくありません")
        if not key or not isinstance(key, str):
            raise ApiError(400, "キーが正しくありません")
        with self._mf_lock:
            d = self._load_mf_import()
            d[kind].pop(key, None)
            self._save_mf_import(d)
        return {"ok": True}

    def mf_import_commit(self, items):
        """フロントで組み立てた取引（{date, desc, splits:[...], ids:[...]}）を順に作成する。
        ids が既に取り込み済みの行はスキップする。1件失敗した時点でそれまでの分の ID は記録して中断する
        （次回の再取り込みで同じ行を二重に作成しないため）。"""
        if not isinstance(items, list) or not items:
            raise ApiError(400, "取り込む行がありません")
        with self._mf_lock:
            d = self._load_mf_import()
            imported = set(d["ids"])
            created = 0
            try:
                for it in items:
                    ids = it.get("ids") or []
                    if not ids:
                        raise ApiError(400, "IDのない行があります")
                    if all(i in imported for i in ids):
                        continue
                    self.create({"date": it.get("date"), "desc": it.get("desc", ""), "splits": it.get("splits")})
                    created += 1
                    imported.update(ids)
            finally:
                d["ids"] = sorted(imported)
                self._save_mf_import(d)
        return {"created": created}

    # ---- アプリ設定の永続化
    # GnuCash の SQLite とは別に、同じフォルダの app-settings.json に保存する
    # （mf-import.json・auth.json と同じ置き場所の流儀。会計データのスキーマには触れない）

    DEFAULT_SETTINGS = {"aiIncludeTxDetails": False, "mcpEnabled": False}

    def _settings_path(self):
        return Path(self.path).parent / "app-settings.json"

    def _load_settings(self):
        p = self._settings_path()
        if not p.exists():
            return dict(self.DEFAULT_SETTINGS)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return dict(self.DEFAULT_SETTINGS)
        return {"aiIncludeTxDetails": bool(data.get("aiIncludeTxDetails", False)),
                "mcpEnabled": bool(data.get("mcpEnabled", False))}

    def _save_settings(self, data):
        p = self._settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def get_settings(self):
        with self._settings_lock:
            d = self._load_settings()
        # aiAvailable は保存された設定ではなく、サーバーに API キーがあるかどうかをその場で返す
        # （フロント側で「分析を開始」前にAI機能が使えるか案内するため）
        d["aiAvailable"] = bool(os.environ.get("GNUCASH_AI_API_KEY"))
        return d

    def update_settings(self, patch):
        with self._settings_lock:
            d = self._load_settings()
            if "aiIncludeTxDetails" in patch:
                d["aiIncludeTxDetails"] = bool(patch["aiIncludeTxDetails"])
            if "mcpEnabled" in patch:
                d["mcpEnabled"] = bool(patch["mcpEnabled"])
            self._save_settings(d)
            return d

    # ---- AIアシスタント：会話履歴の永続化
    # 同じフォルダに、タブ（kind）ごとに会話を1本だけ保存する（複数保存はしない）。
    # 分析タブは ai-conversation.json、記帳タブは ai-ledger-conversation.json に分ける。
    # ツールの使い分け（記帳タブだけが取引を作れる）を会話ごとに明確にするため、あえて混ぜない。
    # 毎回のやり取りで会話全体をAnthropicに送り直す設計のため、無期限に保存すると1回あたりの
    # 料金・応答時間が伸び続けてしまう。そのため保存のたびに直近 AI_MAX_EXCHANGES 往復だけ残す

    AI_MAX_EXCHANGES = 20
    CONVERSATION_FILES = {"analysis": "ai-conversation.json", "ledger": "ai-ledger-conversation.json"}

    def _conversation_path(self, kind="analysis"):
        name = self.CONVERSATION_FILES.get(kind)
        if not name:
            raise ApiError(400, "kind が正しくありません")
        return Path(self.path).parent / name

    def _load_conversation(self, kind="analysis"):
        p = self._conversation_path(kind)
        if not p.exists():
            return {"period": None, "messages": [], "updatedAt": None}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"period": None, "messages": [], "updatedAt": None}
        messages = data.get("messages")
        if not isinstance(messages, list):
            messages = []
        return {"period": data.get("period"), "messages": messages, "updatedAt": data.get("updatedAt")}

    def _save_conversation_file(self, data, kind="analysis"):
        p = self._conversation_path(kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    @staticmethod
    def _trim_conversation(messages):
        # 可視のユーザー発言（content が文字列）を境界に往復単位でグループ化し、直近
        # AI_MAX_EXCHANGES 件だけ残す（ツール呼び出し用の非表示メッセージはその往復に含めて一緒に残す）
        groups = []
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                groups.append([m])
            elif groups:
                groups[-1].append(m)
        kept = groups[-Book.AI_MAX_EXCHANGES:]
        return [m for g in kept for m in g]

    def get_conversation(self, kind="analysis"):
        with self._conversation_lock:
            return self._load_conversation(kind)

    def save_conversation(self, payload, kind="analysis"):
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ApiError(400, "messages が必要です")
        data = {"period": payload.get("period"), "messages": self._trim_conversation(messages),
                "updatedAt": datetime.now(timezone.utc).isoformat()}
        with self._conversation_lock:
            self._save_conversation_file(data, kind)
        return data

    def clear_conversation(self, kind="analysis"):
        with self._conversation_lock:
            p = self._conversation_path(kind)
            if p.exists():
                p.unlink()
        return {"ok": True}

    # ---- MCP連携：科目名ヒント→勘定科目の解決、および確認結果の記憶
    # AIエージェント（MCPサーバー経由）が「食費」のような曖昧な文字列で科目を指定してきたとき、
    # 完全一致・一意な末尾一致ならそのまま解決する。それ以外は候補一覧を返して確認を求め、
    # 確認結果を mcp-mappings.json（mf-import.json 等と同じ置き場所）に保存して、
    # 次回から同じ文字列をそのまま解決できるようにする

    def _mcp_mappings_path(self):
        return Path(self.path).parent / "mcp-mappings.json"

    def _load_mcp_mappings(self):
        p = self._mcp_mappings_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_mcp_mappings(self, data):
        p = self._mcp_mappings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    # 残高を画面上「正の値」で見せるために符号を反転する科目種別（index.html の TYPES の credit と同じ）。
    # これらの科目を amount の起点（account）にして取引を作るときは、金額の符号も反転させる必要がある
    CREDIT_TYPES = {"CREDIT", "LIABILITY", "PAYABLE", "INCOME", "EQUITY"}

    def _account_candidates(self, con):
        """集計用（プレースホルダー）を除いた科目一覧を {guid, name（末端名）, full（フルパス）, type} で返す。
        book() の walk() と同じく ROOT 配下だけを辿る（テンプレート用の木は含めない）"""
        root = con.execute("select root_account_guid from books").fetchone()[0]
        rows = con.execute("select guid, name, parent_guid, placeholder, account_type from accounts").fetchall()
        ph_slots = {r[0] for r in con.execute(
            "select obj_guid from slots where name='placeholder' and string_val='true'")}
        by_parent = {}
        for r in rows:
            by_parent.setdefault(r["parent_guid"], []).append(r)
        out = []

        def walk(parent, path):
            for r in by_parent.get(parent, []):
                full = path + [r["name"]]
                if not (bool(r["placeholder"]) or r["guid"] in ph_slots):
                    out.append({"guid": r["guid"], "name": r["name"], "full": ":".join(full),
                                 "type": r["account_type"]})
                walk(r["guid"], full)

        walk(root, [])
        return out

    def resolve_account(self, query):
        """MCP向け：科目名のヒント文字列を勘定科目に解決する。
        学習済みマッピング → フルパス完全一致 → 末端名／末尾の一意一致 → 科目名の一部に一致、の順に試す
        （いずれも大文字・小文字は区別しない）。一部一致で候補が1件に絞れればそれも解決扱いにする
        （例：「paypayカード」→「負債:クレジットカード:PayPayカード(JCB)」）。
        それでも複数残る・見つからない場合だけ候補一覧（複数件 or 0件）を返し、確認を求める"""
        q = (query or "").strip()
        if not q:
            raise ApiError(400, "query が必要です")
        with self.connect() as con:
            candidates = self._account_candidates(con)
        with self._mcp_lock:
            learned = self._load_mcp_mappings().get(q)
        def resolved(c, source):
            return {"status": "resolved", "source": source, "guid": c["guid"], "name": c["full"],
                    "type": c["type"], "credit": c["type"] in self.CREDIT_TYPES}

        if learned:
            hit = next((c for c in candidates if c["guid"] == learned), None)
            if hit:
                return resolved(hit, "learned")
        ql = q.lower()
        exact = [c for c in candidates if c["full"].lower() == ql]
        if len(exact) == 1:
            return resolved(exact[0], "exact")
        matches = [c for c in candidates if c["name"].lower() == ql or c["full"].lower().endswith(":" + ql)]
        if len(matches) == 1:
            return resolved(matches[0], "unique")
        pool = matches or [c for c in candidates if ql in c["full"].lower()]
        if len(pool) == 1:
            return resolved(pool[0], "partial")
        return {"status": "ambiguous" if pool else "none",
                "candidates": [{"guid": c["guid"], "name": c["full"], "type": c["type"]} for c in pool[:20]]}

    def set_mcp_mapping(self, query, account_guid):
        q = (query or "").strip()
        if not q:
            raise ApiError(400, "query が必要です")
        with self.connect() as con:
            a = self._account(con, account_guid)
            if a["placeholder"]:
                raise ApiError(400, f"集計用（プレースホルダ）の科目「{a['name']}」は選べません")
            full = next((c["full"] for c in self._account_candidates(con) if c["guid"] == account_guid), a["name"])
        with self._mcp_lock:
            d = self._load_mcp_mappings()
            d[q] = account_guid
            self._save_mcp_mappings(d)
        return {"ok": True, "guid": account_guid, "name": full}

    def delete_mcp_mapping(self, query):
        q = (query or "").strip()
        if not q:
            raise ApiError(400, "query が必要です")
        with self._mcp_lock:
            d = self._load_mcp_mappings()
            d.pop(q, None)
            self._save_mcp_mappings(d)
        return {"ok": True}

    # ---- MCP連携：APIキーの発行・一覧・失効
    # 設定画面で発行する。生のキーは発行時のレスポンスで一度だけ返し、保存するのはソルト付きハッシュのみ
    # （mcp-keys.json、data/ 配下、Git には入らない）。万一このファイルが漏れてもキー自体は使えない

    def _mcp_keys_path(self):
        return Path(self.path).parent / "mcp-keys.json"

    def _load_mcp_keys(self):
        p = self._mcp_keys_path()
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save_mcp_keys(self, keys):
        p = self._mcp_keys_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    @staticmethod
    def _hash_mcp_key(token, salt):
        return hashlib.sha256((salt + token).encode()).hexdigest()

    def list_mcp_keys(self):
        with self._mcp_keys_lock:
            keys = self._load_mcp_keys()
        return {"keys": [{"id": k["id"], "label": k.get("label", ""),
                           "createdAt": k["createdAt"], "lastUsedAt": k.get("lastUsedAt")} for k in keys]}

    def create_mcp_key(self, label):
        token = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        rec = {"id": new_guid(), "label": str(label or "").strip()[:100], "salt": salt,
               "hash": self._hash_mcp_key(token, salt),
               "createdAt": datetime.now(timezone.utc).isoformat(), "lastUsedAt": None}
        with self._mcp_keys_lock:
            keys = self._load_mcp_keys()
            keys.append(rec)
            self._save_mcp_keys(keys)
        # key は発行直後のこのレスポンスでしか返らない（以降は保存されたハッシュとしか照合できない）
        return {"id": rec["id"], "label": rec["label"], "createdAt": rec["createdAt"], "key": token}

    def delete_mcp_key(self, key_id):
        with self._mcp_keys_lock:
            keys = self._load_mcp_keys()
            kept = [k for k in keys if k["id"] != key_id]
            self._save_mcp_keys(kept)
        return {"ok": True}

    def check_mcp_key(self, token):
        """Bearer トークンをハッシュ化して保存済みのキーと照合する。一致すれば lastUsedAt を更新する。
        設定で mcpEnabled がオフのときは（キーが残っていても）常に失敗させる"""
        with self._settings_lock:
            mcp_enabled = self._load_settings().get("mcpEnabled")
        if not token or not mcp_enabled:
            return False
        with self._mcp_keys_lock:
            keys = self._load_mcp_keys()
            for k in keys:
                if hmac.compare_digest(self._hash_mcp_key(token, k["salt"]), k["hash"]):
                    k["lastUsedAt"] = datetime.now(timezone.utc).isoformat()
                    self._save_mcp_keys(keys)
                    return True
        return False


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    book: Book = None
    auth = None          # パスキー認証（無効なら None）

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def _send(self, status, payload, ctype="application/json; charset=utf-8", cookie=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ApiError(400, "JSON が不正です")

    # ---- AI分析：Claude（Anthropic）への中継
    # 集計はすべてフロント側（JS）で組み立て、ここではその system プロンプトと会話履歴を
    # そのまま Anthropic の Messages API に転送するだけ（API キーをフロントに渡さないための中継）。
    # レスポンスは Anthropic 側で stream: true にして取得し、テキストが生成されるたびに
    # NDJSON（1行1イベントの JSON）としてそのままクライアントへ中継する（チャットらしく逐次表示するため）
    AI_API_URL = "https://api.anthropic.com/v1/messages"
    AI_API_VERSION = "2023-06-01"
    AI_MAX_TOKENS = 4096

    def ai_chat_stream(self, payload):
        api_key = os.environ.get("GNUCASH_AI_API_KEY")
        if not api_key:
            raise ApiError(400, "サーバーに GNUCASH_AI_API_KEY が設定されていません")
        messages = payload.get("messages") or []
        context = payload.get("context") or ""
        tools = payload.get("tools")
        if not isinstance(messages, list) or not messages:
            raise ApiError(400, "messages が必要です")
        # content はプレーンな文字列（通常の発言）か、ツール呼び出し・結果を表す
        # ブロックの配列（tool_use / tool_result）のどちらか。そのまま転送する
        sent = [{"role": m.get("role"), "content": m.get("content", "")}
                for m in messages if m.get("role") in ("user", "assistant")]
        if not sent:
            raise ApiError(400, "messages が必要です")
        model = os.environ.get("GNUCASH_AI_MODEL", "claude-sonnet-5-5")
        # 期間の切り替えや日付の経過で system プロンプト（集計データ）の内容は会話の途中でも
        # 変わりうる（保存済みの会話を別の日に再開した場合も含む）。これは以前のターンの
        # thinking ブロックの署名を無効にするため、prefix 不一致時はエラーにせず
        # そのブロックを落として続行するよう明示的に指定する
        body = {"model": model, "max_tokens": self.AI_MAX_TOKENS, "messages": sent, "stream": True,
                "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}}
        if context:
            # system プロンプトはプロンプトキャッシュの対象にする（同じ内容を毎回の往復で
            # 送り直すことになるこの設計での料金対策）
            body["system"] = [{"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = tools
        req = urllib.request.Request(self.AI_API_URL, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": api_key, "anthropic-version": self.AI_API_VERSION,
            "anthropic-beta": "thinking-binding-controls-2026-08-01",
            "content-type": "application/json"})
        try:
            upstream = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error", {}).get("message", detail)
            except ValueError:
                pass
            raise ApiError(502, f"AI APIの呼び出しに失敗しました（{e.code}）: {detail[:300]}")
        except urllib.error.URLError as e:
            raise ApiError(502, f"AI APIに接続できませんでした: {e.reason}")

        # ここから先はレスポンスヘッダをもう送ってしまうため、エラーも _handle の
        # ApiError 処理ではなく NDJSON の error イベントとしてクライアントへ伝える
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()

        def emit(obj):
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        blocks, stop_reason = [], None
        try:
            with upstream:
                for raw_line in upstream:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[len("data:"):].strip())
                    except ValueError:
                        continue
                    et = ev.get("type")
                    if et == "content_block_start":
                        idx = ev["index"]
                        while len(blocks) <= idx:
                            blocks.append(None)
                        blocks[idx] = dict(ev["content_block"])
                    elif et == "content_block_delta":
                        idx, delta = ev["index"], ev.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            blocks[idx]["text"] = blocks[idx].get("text", "") + text
                            emit({"type": "text_delta", "text": text})
                        elif delta.get("type") == "input_json_delta":
                            blocks[idx]["_json_buf"] = blocks[idx].get("_json_buf", "") + delta.get("partial_json", "")
                        elif delta.get("type") == "thinking_delta":
                            # 既定のモデルは thinking が有効（表示はされないが、往復時に元のまま
                            # 送り返す必要がある）。ここで拾わないと次のターンで空の thinking
                            # ブロックを送り返すことになり、Anthropic 側で 400 エラーになる
                            blocks[idx]["thinking"] = blocks[idx].get("thinking", "") + delta.get("thinking", "")
                        elif delta.get("type") == "signature_delta":
                            blocks[idx]["signature"] = blocks[idx].get("signature", "") + delta.get("signature", "")
                    elif et == "content_block_stop":
                        b = blocks[ev["index"]]
                        if "_json_buf" in b:
                            try:
                                b["input"] = json.loads(b.pop("_json_buf") or "{}")
                            except ValueError:
                                b["input"] = {}
                    elif et == "message_delta":
                        stop_reason = (ev.get("delta") or {}).get("stop_reason", stop_reason)
                    elif et == "error":
                        emit({"type": "error", "message": (ev.get("error") or {}).get("message") or "AI APIエラー"})
                        return
        except Exception as e:
            emit({"type": "error", "message": f"AI APIへの接続が切れました: {e}"})
            return

        content = [b for b in blocks if b]
        if not content:
            emit({"type": "error", "message": "AI APIから応答を取得できませんでした"})
            return
        if stop_reason == "max_tokens":
            note = "\n\n（※ 回答が長くなったため途中で切れています。続きを聞きたい場合はそのまま質問してください）"
            text_blocks = [b for b in content if b.get("type") == "text"]
            if text_blocks:
                text_blocks[-1]["text"] = text_blocks[-1].get("text", "") + note
            else:
                content.append({"type": "text", "text": note.strip()})
            emit({"type": "text_delta", "text": note})
        emit({"type": "done", "content": content, "stopReason": stop_reason})

    def _api_key_authed(self):
        """MCPサーバーなど、ブラウザを介さないクライアント向けの認証。
        設定画面で発行した Authorization: Bearer <key> が一致すればOK。
        パスキー認証（GNUCASH_AUTH）の有無に関わらず使える（キーが1つもない・mcpEnabledがオフなら常にFalse）。"""
        given = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not given.startswith(prefix):
            return False
        return self.book.check_mcp_key(given[len(prefix):])

    def _route(self, method):
        url = urlparse(self.path)
        p, qs = url.path, parse_qs(url.query)
        b = self.book
        if method == "GET" and not p.startswith("/api/"):
            # 画面は誰でも読める（ログイン画面を含むため）。データはすべて API 側で保護する。
            # /recent・/report・/<勘定科目の guid> など、フロント側のクライアントサイドルーティングが
            # 扱うパスはすべてここで index.html を返す（SPA のキャッチオール。直接アクセス・リロード対応）
            return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        api_keyed = p.startswith("/api/") and self._api_key_authed()
        if self.auth:
            authed = api_keyed or self.auth.is_authenticated(self.headers.get("Cookie"))
            # Bearer トークンはブラウザが自動付与するものではないので CSRF（Origin チェック）の対象外
            if not api_keyed and method != "GET" \
                    and not self.auth.check_origin(self.headers.get("Origin"), self.headers.get("Referer")):
                raise ApiError(403, "別のサイトからの操作は受け付けません")
            if p.startswith("/api/auth/"):
                return self._route_auth(method, p, authed)
            if not authed:
                return self._send(401, {"error": "ログインが必要です", "auth": True})
        elif p == "/api/auth/status":
            return self._send(200, {"enabled": False, "authenticated": True})
        if method == "GET" and p == "/api/book":
            return self._send(200, b.book())
        m = re.fullmatch(r"/api/accounts/([0-9a-f]{32})/register", p)
        if method == "GET" and m:
            return self._send(200, b.register(m[1]))
        if method == "POST" and p == "/api/accounts":
            return self._send(201, b.create_account(self._body()))
        m = re.fullmatch(r"/api/accounts/([0-9a-f]{32})", p)
        if m and method == "PUT":
            return self._send(200, b.update_account(m[1], self._body()))
        if m and method == "DELETE":
            return self._send(200, b.delete_account(m[1]))
        if method == "GET" and p == "/api/quickfill":
            account, desc = qs.get("account", [""])[0], qs.get("desc", [""])[0]
            # account なし = 最近の取引画面の「出金元 → 入金先」用
            return self._send(200, b.quickfill(account, desc) if account else b.quickfill_pair(desc))
        if method == "GET" and p == "/api/recent":
            try:
                days = int(qs.get("days", ["7"])[0])
            except ValueError:
                raise ApiError(400, "days が正しくありません")
            return self._send(200, b.recent(days))
        if method == "GET" and p == "/api/reports/income-statement":
            frm, to = qs.get("from", [""])[0], qs.get("to", [""])[0]
            if not frm or not to:
                raise ApiError(400, "from・to が必要です")
            return self._send(200, b.income_statement(frm, to))
        if method == "GET" and p == "/api/reports/account-history":
            frm, to = qs.get("from", [""])[0], qs.get("to", [""])[0]
            accounts = [g for g in qs.get("accounts", [""])[0].split(",") if g]
            if not frm or not to:
                raise ApiError(400, "from・to が必要です")
            return self._send(200, b.account_history(frm, to, accounts))
        if method == "GET" and p == "/api/search":
            return self._send(200, b.search_transactions(qs.get("q", [""])[0]))
        if method == "GET" and p == "/api/reports/transactions":
            frm, to = qs.get("from", [""])[0], qs.get("to", [""])[0]
            if not frm or not to:
                raise ApiError(400, "from・to が必要です")
            try:
                limit = int(qs.get("limit", ["200"])[0])
            except ValueError:
                raise ApiError(400, "limit が正しくありません")
            return self._send(200, b.transactions_in_range(frm, to, limit))
        if method == "GET" and p == "/api/settings":
            return self._send(200, b.get_settings())
        if method == "PUT" and p == "/api/settings":
            return self._send(200, b.update_settings(self._body()))
        if method == "POST" and p == "/api/ai/chat/stream":
            return self.ai_chat_stream(self._body())
        if method == "GET" and p == "/api/ai/conversation":
            return self._send(200, b.get_conversation())
        if method == "PUT" and p == "/api/ai/conversation":
            return self._send(200, b.save_conversation(self._body()))
        if method == "DELETE" and p == "/api/ai/conversation":
            return self._send(200, b.clear_conversation())
        if method == "GET" and p == "/api/ai/ledger-conversation":
            return self._send(200, b.get_conversation("ledger"))
        if method == "PUT" and p == "/api/ai/ledger-conversation":
            return self._send(200, b.save_conversation(self._body(), "ledger"))
        if method == "DELETE" and p == "/api/ai/ledger-conversation":
            return self._send(200, b.clear_conversation("ledger"))
        if method == "GET" and p == "/api/mf-import/mappings":
            return self._send(200, b.mf_import_mappings())
        if method == "POST" and p == "/api/mf-import/mappings":
            body = self._body()
            return self._send(200, b.mf_set_mapping(body.get("kind"), body.get("key"), body.get("account")))
        if method == "DELETE" and p == "/api/mf-import/mappings":
            body = self._body()
            return self._send(200, b.mf_delete_mapping(body.get("kind"), body.get("key")))
        if method == "GET" and p == "/api/mf-import/ids":
            return self._send(200, b.mf_import_ids())
        if method == "POST" and p == "/api/mf-import/commit":
            return self._send(201, b.mf_import_commit(self._body().get("items")))
        if method == "GET" and p == "/api/mcp/resolve-account":
            return self._send(200, b.resolve_account(qs.get("q", [""])[0]))
        if method == "POST" and p == "/api/mcp/mappings":
            body = self._body()
            return self._send(200, b.set_mcp_mapping(body.get("query"), body.get("account")))
        if method == "DELETE" and p == "/api/mcp/mappings":
            body = self._body()
            return self._send(200, b.delete_mcp_mapping(body.get("query")))
        if method == "GET" and p == "/api/mcp/keys":
            return self._send(200, b.list_mcp_keys())
        if method == "POST" and p == "/api/mcp/keys":
            return self._send(201, b.create_mcp_key(self._body().get("label")))
        m = re.fullmatch(r"/api/mcp/keys/([0-9a-f]{32})", p)
        if method == "DELETE" and m:
            return self._send(200, b.delete_mcp_key(m[1]))
        if method == "POST" and p == "/api/transactions":
            return self._send(201, b.create(self._body()))
        m = re.fullmatch(r"/api/transactions/([0-9a-f]{32})/copy", p)
        if method == "POST" and m:
            return self._send(201, b.copy(m[1], self._body()))
        if method == "POST" and p == "/api/transactions/restore":
            return self._send(201, b.restore(self._body()))
        m = re.fullmatch(r"/api/transactions/([0-9a-f]{32})", p)
        if m and method == "GET":
            return self._send(200, b.transaction(m[1]))
        if m and method == "PUT":
            return self._send(200, b.update(m[1], self._body()))
        if m and method == "DELETE":
            return self._send(200, b.delete(m[1]))
        raise ApiError(404, f"Not found: {unquote(p)}")

    def _route_auth(self, method, p, authed):
        a = self.auth
        if method == "GET" and p == "/api/auth/status":
            return self._send(200, a.status(authed))
        if method == "POST" and p == "/api/auth/login/options":
            return self._send(200, a.authentication_options())
        if method == "POST" and p == "/api/auth/login":
            res = a.authenticate(self._body())
            return self._send(200, res, cookie=a.make_cookie())
        if method == "POST" and p == "/api/auth/register/options":
            return self._send(200, a.registration_options(self._body(), authed))
        if method == "POST" and p == "/api/auth/register":
            res = a.register(self._body(), authed)
            return self._send(200, res, cookie=a.make_cookie())
        if method == "POST" and p == "/api/auth/logout":
            return self._send(200, {"ok": True}, cookie=a.clear_cookie())
        if not authed:
            return self._send(401, {"error": "ログインが必要です", "auth": True})
        if method == "GET" and p == "/api/auth/credentials":
            return self._send(200, a.list_credentials())
        m = re.fullmatch(r"/api/auth/credentials/([A-Za-z0-9_-]+)", p)
        if method == "DELETE" and m:
            return self._send(200, a.delete_credential(m[1]))
        raise ApiError(404, f"Not found: {unquote(p)}")

    def _handle(self, method):
        try:
            self._route(method)
        except ApiError as e:
            self._send(e.status, {"error": str(e)})
        except Exception as e:   # 認証まわりのエラー（AuthError）も JSON で返す
            if type(e).__name__ != "AuthError":
                raise
            self._send(e.status, {"error": str(e), "auth": e.status == 401})
        except sqlite3.Error as e:
            self._send(500, {"error": f"データベースエラー: {e}"})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")


def setup_auth(path, port):
    """パスキー認証の設定を環境変数から読み込む。"""
    try:
        from auth import Auth
    except ImportError:
        sys.exit("パスキー認証には py_webauthn が必要です: pip install -r requirements.txt")
    rp_id = os.environ.get("GNUCASH_AUTH_RP_ID", "localhost")
    origin = os.environ.get("GNUCASH_AUTH_ORIGIN") or f"http://localhost:{port}"
    host = urlparse(origin).hostname or ""
    if host != rp_id and not host.endswith("." + rp_id):
        sys.exit(f"GNUCASH_AUTH_ORIGIN（{origin}）のホスト名が GNUCASH_AUTH_RP_ID（{rp_id}）と一致しません")
    try:
        ipaddress.ip_address(rp_id.strip("[]"))
        sys.exit("パスキーは IP アドレスでは使えません。GNUCASH_AUTH_RP_ID にはドメイン名（localhost など）を指定してください")
    except ValueError:
        pass
    store = os.environ.get("GNUCASH_AUTH_FILE") or str(path.parent / "auth.json")
    days = float(os.environ.get("GNUCASH_AUTH_SESSION_DAYS", "30"))
    a = Auth(store, rp_id, origin, days)
    print(f"パスキー認証: 有効（RP ID {rp_id} / {origin} / ログイン有効期間 {days:g} 日 / {store}）", flush=True)
    return a


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main():
    ap = argparse.ArgumentParser(description="GnuCash SQLite ファイルを Web で閲覧・編集する")
    ap.add_argument("file", nargs="?", default=os.environ.get("GNUCASH_FILE") or str(DEFAULT_FILE),
                    help=f"GnuCash の SQLite ファイル（既定 {DEFAULT_FILE}）。環境変数 GNUCASH_FILE でも可")
    ap.add_argument("--host", default=os.environ.get("GNUCASH_HOST", "127.0.0.1"),
                    help="待ち受けアドレス（既定 127.0.0.1。Docker では 0.0.0.0）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("GNUCASH_PORT", "8000")))
    ap.add_argument("--write", action="store_true", default=env_flag("GNUCASH_WRITE"),
                    help="書き込みを許可する（起動時にバックアップを作成）")
    ap.add_argument("--force", action="store_true", default=env_flag("GNUCASH_FORCE"),
                    help="gnclock（デスクトップ版のロック）があっても書き込む")
    ap.add_argument("--backup-dir", default=os.environ.get("GNUCASH_BACKUP_DIR"),
                    help="バックアップの保存先（既定はファイルと同じフォルダの backup/）")
    ap.add_argument("--auth", action="store_true", default=env_flag("GNUCASH_AUTH"),
                    help="パスキー認証を有効にする（py_webauthn が必要）")
    args = ap.parse_args()

    # docker stop（SIGTERM）で速やかに終了する。PID 1 ではハンドラがないとシグナルが無視されるため
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    path = Path(args.file).resolve()
    if not path.is_file():
        sys.exit(f"{path} が見つかりません（GnuCash ファイルを data/ に置くか、パスを指定してください）")
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            sys.exit(f"{path} は SQLite 形式ではありません（XML 形式の場合は GnuCash で「名前を付けて保存」→ sqlite3 で保存してください）")

    if args.write:
        backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else path.parent / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{path.name}.{datetime.now():%Y%m%d%H%M%S}.bak"
        shutil.copy2(path, backup)
        print(f"バックアップ: {backup}")

    Handler.book = Book(str(path), args.write, args.force)
    Handler.book.ensure_indexes()
    if args.auth:
        Handler.auth = setup_auth(path, args.port)
    with Handler.book.connect() as con:
        locks = Handler.book.locked_by(con)
    if locks:
        print(f"注意: gnclock にロックがあります {locks}"
              + ("（--force 指定のため書き込みます）" if args.force else "（書き込みは拒否されます）"))

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"{'書き込み可' if args.write else '読み取り専用'}で {path.name} を開きました → http://{args.host}:{args.port}/",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

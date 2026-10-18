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

スキーマは docs/gnucash-db-schema.md を参照。
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import signal
import sqlite3
import sys
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

STATIC_DIR = Path(__file__).parent / "static"
# 既定のデータファイル（プロジェクト直下の data/。Git では管理しない）
DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "gnucash.gnucash"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

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


# ---------------------------------------------------------------- 帳簿アクセス

class Book:
    def __init__(self, path, writable, force):
        self.path = path
        self.writable = writable
        self.force = force

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
            descs = [r[0] for r in con.execute(f"""
                select t.description from transactions t where t.description <> ''
                group by t.description order by max({NORM_POST_DATE}) desc limit 1000""")]
            currency = con.execute("""
                select c.mnemonic from transactions t join commodities c on c.guid = t.currency_guid
                group by 1 order by count(*) desc limit 1""").fetchone()
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

    def _route(self, method):
        url = urlparse(self.path)
        p, qs = url.path, parse_qs(url.query)
        b = self.book
        if method == "GET" and p in ("/", "/index.html"):
            # 画面は誰でも読める（ログイン画面を含むため）。データはすべて API 側で保護する
            return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.auth:
            authed = self.auth.is_authenticated(self.headers.get("Cookie"))
            if method != "GET" and not self.auth.check_origin(self.headers.get("Origin"), self.headers.get("Referer")):
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

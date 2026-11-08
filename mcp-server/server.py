#!/usr/bin/env python3
"""GnuCash Web に、AIエージェントから「食費 500円」のような指示で取引を追加するための
MCPサーバー（標準ライブラリのみ）。stdio 経由で MCP クライアント（Claude Desktop・Claude Code など）
に接続する想定で、GnuCash Web 本体（web/server.py）とは別プロセスで動かす。

GnuCash Web 側の HTTP API を、Authorization: Bearer <GNUCASH_API_KEY> を付けて呼び出すだけで、
GnuCash の SQLite ファイルには直接触れない。

使い方（GnuCash Web の設定画面「MCP連携」で発行したAPIキーを使う。--write で起動しておくこと）:
    GNUCASH_WEB_URL=http://127.0.0.1:8000 GNUCASH_API_KEY=発行したキー python3 mcp-server/server.py

設定は環境変数で行う:
    GNUCASH_WEB_URL                      GnuCash Web のURL（必須）
    GNUCASH_API_KEY                      GnuCash Web の設定画面「MCP連携」で発行したAPIキー（必須）
    GNUCASH_MCP_DEFAULT_PAYMENT_ACCOUNT  payment_account を省略したときに使う科目のヒント（例: 現金）

科目名の解決：
    「食費」のような文字列から勘定科目を特定できない（該当なし・複数候補）場合は取引を作らず、
    候補一覧をエージェントに返す。エージェントがユーザーに確認したら、confirmed_category／
    confirmed_payment_account に選んだ科目名（候補の name をそのまま）を指定して再度呼び出す。
    確認結果は GnuCash Web 側に保存され（data/mcp-mappings.json）、次回から同じ文字列を
    そのまま自動解決できるようになる。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from os import environ

WEB_URL = environ.get("GNUCASH_WEB_URL", "").rstrip("/")
API_KEY = environ.get("GNUCASH_API_KEY", "")
DEFAULT_PAYMENT_HINT = environ.get("GNUCASH_MCP_DEFAULT_PAYMENT_ACCOUNT", "").strip()

PROTOCOL_VERSION_FALLBACK = "2025-06-18"
SERVER_INFO = {"name": "gnucash-web-mcp", "version": "1.0.0"}

ADD_TRANSACTION_TOOL = {
    "name": "add_transaction",
    "description": (
        "GnuCash Web に取引を1件記録する（例: 「食費 500円」「給与 300000円」）。"
        "category（勘定科目のヒント）が一意に特定できない場合は取引を作らず、"
        "候補一覧または「該当なし」を返すので、ユーザーに確認したうえで "
        "confirmed_category（候補の name をそのまま）を指定して再度呼び出すこと。"
        "payment_account も同様に曖昧な場合がある。一度確認した対応は記憶され、次回から確認不要になる。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "金額。支出・収入のどちらでも正の数で指定する（例: 500）",
            },
            "category": {
                "type": "string",
                "description": "勘定科目のヒント（例: '食費', '交際費', '給与'）。"
                                "費用科目なら支出、収益科目なら収入として記録される",
            },
            "payment_account": {
                "type": "string",
                "description": "支払い元・入金先のヒント（例: '現金', '普通預金'）。"
                                "省略時はサーバーに設定された既定の口座を使う",
            },
            "description": {"type": "string", "description": "取引の説明・メモ（省略可）"},
            "date": {"type": "string", "description": "日付（YYYY-MM-DD）。省略時は今日"},
            "confirmed_category": {
                "type": "string",
                "description": "category が曖昧だった場合に、ユーザーが選んだ正式な科目名（候補のnameをそのまま）。"
                                "指定すると category の表記を次回のために記憶する",
            },
            "confirmed_payment_account": {
                "type": "string",
                "description": "confirmed_category と同様。payment_account 用",
            },
        },
        "required": ["amount", "category"],
    },
}


class ToolError(Exception):
    """ツール呼び出し自体の失敗（設定ミス・API疎通エラーなど）。MCPのエラーレスポンスにする。"""


def api(method, path, body=None, params=None):
    if not WEB_URL or not API_KEY:
        raise ToolError("GNUCASH_WEB_URL / GNUCASH_API_KEY が設定されていません")
    url = WEB_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        raise ToolError(f"GnuCash Web API エラー（{e.code}）: {detail}")
    except urllib.error.URLError as e:
        raise ToolError(f"GnuCash Web に接続できませんでした（{WEB_URL}）: {e.reason}")


def resolve_account(query):
    return api("GET", "/api/mcp/resolve-account", params={"q": query})


def remember_mapping(query, guid):
    if query:
        api("POST", "/api/mcp/mappings", body={"query": query, "account": guid})


def candidates_text(r):
    if r["status"] == "none":
        return "該当する勘定科目が見つかりませんでした。正式な科目名を教えてください。"
    names = "\n".join(f"- {c['name']}" for c in r["candidates"])
    return f"候補が複数あり特定できません。どれか教えてください。\n{names}"


def resolve_field(hint, confirmed, label):
    """hint（元の文字列ヒント）を勘定科目に解決する。
    confirmed が指定されていれば、それを解決したうえで hint→guid の対応を記憶する（学習）。
    戻り値: (resolved_dict または None, 未解決時のユーザー向けメッセージ または None)"""
    text = (confirmed or hint or "").strip()
    if not text:
        return None, f"{label}が指定されていません。"
    r = resolve_account(text)
    if r["status"] != "resolved":
        return None, f"{label}「{text}」: {candidates_text(r)}"
    if confirmed and hint and confirmed.strip() != hint.strip():
        remember_mapping(hint.strip(), r["guid"])
    return r, None


def tool_add_transaction(args):
    try:
        amount = float(args.get("amount"))
    except (TypeError, ValueError):
        raise ToolError("amount には数値を指定してください")
    if amount == 0:
        raise ToolError("amount に 0 以外の数値を指定してください")
    category_hint = str(args.get("category") or "").strip()
    if not category_hint:
        raise ToolError("category を指定してください")
    payment_hint = str(args.get("payment_account") or "").strip() or DEFAULT_PAYMENT_HINT
    confirmed_category = args.get("confirmed_category")
    confirmed_payment = args.get("confirmed_payment_account")

    category, msg1 = resolve_field(category_hint, confirmed_category, "category")
    if not payment_hint and not confirmed_payment:
        msg2 = "payment_account が指定されておらず、既定の口座も設定されていません。"
        payment = None
    else:
        payment, msg2 = resolve_field(payment_hint, confirmed_payment, "payment_account")
    if msg1 or msg2:
        text = "\n".join(m for m in (msg1, msg2) if m)
        text += ("\n\n確認できたら、add_transaction を confirmed_category（および必要なら"
                 " confirmed_payment_account）を指定して同じ内容でもう一度呼び出してください。"
                 "次回から同じ表記では確認不要になります。")
        return {"content": [{"type": "text", "text": text}]}

    # category 科目側を基準に、符号反転が必要な科目種別（収益・負債など）なら金額の符号を反転する
    signed = -abs(amount) if category["credit"] else abs(amount)
    d = str(args.get("date") or "").strip() or date.today().isoformat()
    desc = str(args.get("description") or "").strip()
    api("POST", "/api/transactions", body={
        "account": category["guid"], "other": payment["guid"],
        "date": d, "desc": desc, "amount": str(signed),
    })
    text = f"記録しました：{d} {desc or '（説明なし）'} {category['name']} {amount:g}円（{payment['name']}）"
    return {"content": [{"type": "text", "text": text}]}


def handle_tool_call(name, args):
    if name != "add_transaction":
        raise ToolError(f"不明なツールです: {name}")
    return tool_add_transaction(args or {})


# ---------------------------------------------------------------- MCP (stdio / JSON-RPC 2.0)

def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def reply(id_, result):
    send({"jsonrpc": "2.0", "id": id_, "result": result})


def reply_error(id_, code, message):
    send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def handle(msg):
    method = msg.get("method")
    id_ = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        version = params.get("protocolVersion") or PROTOCOL_VERSION_FALLBACK
        return reply(id_, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "initialized"):
        return   # 通知。応答不要
    if method == "ping":
        return reply(id_, {})
    if method == "tools/list":
        return reply(id_, {"tools": [ADD_TRANSACTION_TOOL]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            result = handle_tool_call(name, args)
        except ToolError as e:
            return reply(id_, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        return reply(id_, result)
    if id_ is not None:
        reply_error(id_, -32601, f"Method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as e:   # サーバー自体は落とさず、エラーとしてクライアントへ返す
            id_ = msg.get("id") if isinstance(msg, dict) else None
            if id_ is not None:
                reply_error(id_, -32000, str(e))
            else:
                print(f"error handling {msg}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

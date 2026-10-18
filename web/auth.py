"""パスキー（WebAuthn）認証。GNUCASH_AUTH=1 のときだけ server.py から読み込まれる。

- 利用者は 1 人。ID・パスワードは使わず、端末に保存された「発見可能なパスキー」でログインする
- 最初の 1 本は、起動時にログへ出す登録用トークンを知っている人だけが登録できる
- 2 本目以降はログイン中に追加する
- ログイン状態は署名付き Cookie（HMAC）で保持する。サーバを再起動してもログアウトされない
- 認証情報は GnuCash のファイルとは別の JSON（既定は data/auth.json）に保存する。
  公開鍵のみで秘密鍵は持たないが、Cookie の署名鍵を含むので外部に出さないこと
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

COOKIE = "gcw_session"
CHALLENGE_TTL = 300          # 秒。パスキーの操作はこの時間内に完了させる
TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 見間違えやすい 0/O・1/I は除く


class AuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Auth:
    def __init__(self, store_path, rp_id, origin, session_days=30, rp_name="GnuCash Web"):
        self.path = Path(store_path)
        self.rp_id = rp_id
        self.origin = origin.rstrip("/")
        self.session_seconds = int(session_days * 86400)
        self.rp_name = rp_name
        self.secure_cookie = self.origin.startswith("https://")
        self.lock = threading.Lock()
        self.challenges = {}     # challenge(base64url) → (用途, 期限)
        self.setup_token = None
        self.data = self._load()
        if not self.data["credentials"]:
            self._new_setup_token()

    # ---------------------------------------------------------------- 保存

    def _load(self):
        if self.path.exists():
            data = json.loads(self.path.read_text())
        else:
            data = {}
        changed = not data
        data.setdefault("secret", secrets.token_hex(32))          # Cookie の署名鍵
        data.setdefault("user_id", bytes_to_base64url(secrets.token_bytes(32)))
        data.setdefault("epoch", 1)                                # 上げると全セッションが無効になる
        data.setdefault("credentials", [])
        if changed:
            self._write(data)
        return data

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def _save(self):
        self._write(self.data)

    def _new_setup_token(self):
        raw = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(12))
        self.setup_token = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
        print(f"パスキーが未登録です。初回登録用トークン: {self.setup_token}", flush=True)

    # ---------------------------------------------------------------- セッション

    def make_cookie(self):
        payload = _b64(json.dumps({"e": int(time.time()) + self.session_seconds, "v": self.data["epoch"]}).encode())
        sig = _b64(hmac.new(bytes.fromhex(self.data["secret"]), payload.encode(), hashlib.sha256).digest())
        attrs = f"Path=/; Max-Age={self.session_seconds}; HttpOnly; SameSite=Strict"
        return f"{COOKIE}={payload}.{sig}; {attrs}" + ("; Secure" if self.secure_cookie else "")

    def clear_cookie(self):
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" + ("; Secure" if self.secure_cookie else "")

    def is_authenticated(self, cookie_header):
        value = None
        for part in (cookie_header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                value = v
        if not value or "." not in value:
            return False
        payload, sig = value.rsplit(".", 1)
        expected = _b64(hmac.new(bytes.fromhex(self.data["secret"]), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            body = json.loads(_unb64(payload))
        except ValueError:
            return False
        return body.get("e", 0) > time.time() and body.get("v") == self.data["epoch"]

    def check_origin(self, origin_header, referer_header):
        """書き込み系のリクエストが自分の画面から来たか確認する（CSRF 対策）。"""
        if origin_header:
            return origin_header.rstrip("/") == self.origin
        return bool(referer_header) and referer_header.startswith(self.origin + "/")

    # ---------------------------------------------------------------- チャレンジ

    def _issue(self, purpose, challenge):
        now = time.time()
        with self.lock:
            for k in [k for k, (_, exp) in self.challenges.items() if exp < now]:
                del self.challenges[k]
            if len(self.challenges) > 100:
                raise AuthError(429, "リクエストが多すぎます。しばらくしてから試してください")
            self.challenges[bytes_to_base64url(challenge)] = (purpose, now + CHALLENGE_TTL)

    def _consume(self, purpose, credential):
        """クライアントが返した clientDataJSON からチャレンジを取り出し、発行済みのものか確認して消費する。"""
        try:
            client = json.loads(_unb64(credential["response"]["clientDataJSON"]))
            key = client["challenge"]
        except (KeyError, TypeError, ValueError):
            raise AuthError(400, "パスキーの応答が正しくありません")
        with self.lock:
            entry = self.challenges.pop(key, None)
        if not entry or entry[0] != purpose or entry[1] < time.time():
            raise AuthError(400, "認証の有効期限が切れました。もう一度試してください")
        return base64url_to_bytes(key)

    # ---------------------------------------------------------------- API

    def status(self, authenticated):
        return {"enabled": True, "authenticated": authenticated, "hasCredentials": bool(self.data["credentials"]),
                "rpId": self.rp_id, "origin": self.origin}

    def _check_token(self, token):
        norm = lambda t: "".join(ch for ch in str(t or "").upper() if ch.isalnum())
        if not self.setup_token or not hmac.compare_digest(norm(token), norm(self.setup_token)):
            raise AuthError(403, "登録用トークンが違います（サーバのログに表示されたものを入力してください）")

    def registration_options(self, body, authenticated):
        if self.data["credentials"]:
            if not authenticated:
                raise AuthError(401, "パスキーを追加するにはログインしてください")
        else:
            self._check_token(body.get("token"))
        options = webauthn.generate_registration_options(
            rp_id=self.rp_id, rp_name=self.rp_name,
            user_id=base64url_to_bytes(self.data["user_id"]), user_name="owner", user_display_name="GnuCash Web",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"]))
                                 for c in self.data["credentials"]])
        self._issue("reg", options.challenge)
        return json.loads(webauthn.options_to_json(options))

    def register(self, body, authenticated):
        credential = body.get("credential") or {}
        challenge = self._consume("reg", credential)
        first = not self.data["credentials"]
        if not first and not authenticated:
            raise AuthError(401, "パスキーを追加するにはログインしてください")
        if first:
            self._check_token(body.get("token"))
        try:
            v = webauthn.verify_registration_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id,
                expected_origin=self.origin, require_user_verification=True)
        except WebAuthnException as e:
            raise AuthError(400, f"パスキーを確認できませんでした: {e}")
        name = str(body.get("name") or "").strip()[:60] or f"パスキー {len(self.data['credentials']) + 1}"
        with self.lock:
            self.data["credentials"].append({
                "id": bytes_to_base64url(v.credential_id),
                "public_key": bytes_to_base64url(v.credential_public_key),
                "sign_count": v.sign_count,
                "name": name,
                "transports": (credential.get("response") or {}).get("transports") or [],
                "backed_up": bool(getattr(v, "credential_backed_up", False)),
                "created": _now_iso(),
                "last_used": None,
            })
            self._save()
        if first:
            self.setup_token = None
            print(f"パスキー「{name}」を登録しました。登録用トークンは無効になりました", flush=True)
        return {"ok": True, "name": name}

    def authentication_options(self):
        # allow_credentials を空にして、端末に保存されたパスキーから選んでもらう（ID 入力なし）
        options = webauthn.generate_authentication_options(
            rp_id=self.rp_id, user_verification=UserVerificationRequirement.REQUIRED)
        self._issue("auth", options.challenge)
        return json.loads(webauthn.options_to_json(options))

    def authenticate(self, body):
        credential = body.get("credential") or {}
        challenge = self._consume("auth", credential)
        stored = next((c for c in self.data["credentials"] if c["id"] == credential.get("id")), None)
        if stored is None:
            raise AuthError(401, "このパスキーは登録されていません")
        try:
            v = webauthn.verify_authentication_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id,
                expected_origin=self.origin, credential_public_key=base64url_to_bytes(stored["public_key"]),
                credential_current_sign_count=stored["sign_count"], require_user_verification=True)
        except WebAuthnException as e:
            raise AuthError(401, f"パスキーを確認できませんでした: {e}")
        with self.lock:
            stored["sign_count"] = v.new_sign_count
            stored["last_used"] = _now_iso()
            self._save()
        return {"ok": True, "name": stored["name"]}

    def list_credentials(self):
        return {"credentials": [{k: c[k] for k in ("id", "name", "created", "last_used", "backed_up")}
                                for c in self.data["credentials"]]}

    def delete_credential(self, cred_id):
        with self.lock:
            before = len(self.data["credentials"])
            self.data["credentials"] = [c for c in self.data["credentials"] if c["id"] != cred_id]
            if len(self.data["credentials"]) == before:
                raise AuthError(404, "パスキーが見つかりません")
            last = not self.data["credentials"]
            if last:
                self.data["epoch"] += 1     # 最後の 1 本を消したら全端末をログアウトさせる
            self._save()
        if last:
            print("パスキーがすべて削除されました。", file=sys.stdout, flush=True)
            self._new_setup_token()
        return {"ok": True, "remaining": len(self.data["credentials"])}

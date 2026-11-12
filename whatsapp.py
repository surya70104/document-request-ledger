"""WhatsApp transport for the Document Request Ledger.

Two modes, chosen by the environment:

  * SIMULATOR (default; WHATSAPP_PROVIDER empty): nothing leaves this machine. Every outbound message
    is stored and shown in the WhatsApp Simulator screen, where you can also play the client
    (send photos, PDFs, text, "I don't have this") and watch it land on the ledger.
  * LIVE (WHATSAPP_PROVIDER=meta | generic and WHATSAPP_TOKEN set):
      meta     WhatsApp Business Cloud API. WHATSAPP_PHONE_ID, WHATSAPP_TOKEN; optional
               WHATSAPP_API_URL (default https://graph.facebook.com/v20.0); webhook verification with
               WHATSAPP_VERIFY_TOKEN and signature checking with WHATSAPP_APP_SECRET.
      generic  Any gateway that accepts POST {"to": "+91...", "body": "..."} with a Bearer token at
               WHATSAPP_API_URL and answers {"id": "..."}.

Live mode needs an approved WhatsApp Business account; it has not been exercised against a real
account in this build (the Meta payload parsing and media download are covered by tests with the
network calls replaced).
"""
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Optional


class WhatsAppDeliveryError(Exception):
    """The provider did not accept the message."""


def provider() -> str:
    return os.getenv("WHATSAPP_PROVIDER", "").strip().lower()


def is_live() -> bool:
    return provider() in ("meta", "generic") and bool(os.getenv("WHATSAPP_TOKEN", "").strip())


def mode_label() -> str:
    return f"live ({provider()})" if is_live() else "simulator"


_PHONE_STRIP = re.compile(r"[\s\-().]")


def normalise_phone(raw: Optional[str], default_country: str = "91") -> Optional[str]:
    """'98765 43210' -> '+919876543210'; '+44 20 7946 0958' -> '+442079460958'; junk -> None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    cleaned = _PHONE_STRIP.sub("", raw)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)
    if not plus and len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]  # the national trunk prefix: 098765 43210
    if not plus and len(digits) == 10:
        digits = default_country + digits
    if not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - operator-configured URL
            return json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise WhatsAppDeliveryError(str(exc)) from exc


def send_text(to: str, body: str) -> dict:
    """Send one text message. Returns {'id': provider message id, 'mode': 'simulated' | 'sent'}.
    Raises WhatsAppDeliveryError when a live provider refuses it."""
    if not is_live():
        return {"id": f"sim-{uuid.uuid4().hex[:16]}", "mode": "simulated"}
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    auth = {"Authorization": f"Bearer {token}"}
    if provider() == "meta":
        base = os.getenv("WHATSAPP_API_URL", "https://graph.facebook.com/v20.0").rstrip("/")
        data = _post_json(
            f"{base}/{os.getenv('WHATSAPP_PHONE_ID', '').strip()}/messages",
            {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text", "text": {"body": body, "preview_url": True}},
            auth,
        )
        ids = [m.get("id") for m in data.get("messages", []) if m.get("id")]
        if not ids:
            raise WhatsAppDeliveryError(f"The WhatsApp API did not accept the message: {data}")
        return {"id": ids[0], "mode": "sent"}
    data = _post_json(os.getenv("WHATSAPP_API_URL", "").strip(), {"to": to, "body": body}, auth)
    return {"id": str(data.get("id") or uuid.uuid4().hex), "mode": "sent"}


# ---- inbound ------------------------------------------------------------------------------------
def verify_signature(raw_body: bytes, header: Optional[str]) -> bool:
    """Meta signs every webhook call: X-Hub-Signature-256 = 'sha256=' + HMAC(app secret, body)."""
    secret = os.getenv("WHATSAPP_APP_SECRET", "").strip()
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def parse_meta_payload(payload: dict) -> list[dict]:
    """Flatten a Meta Cloud API webhook into inbound messages:
    {id, phone, name, text, media: [{media_id, filename, content_type}], reply_to, timestamp}."""
    out = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts", []) or []}
            for m in value.get("messages", []) or []:
                kind = m.get("type")
                text, media = None, []
                if kind == "text":
                    text = (m.get("text") or {}).get("body")
                elif kind in ("image", "document", "video", "audio"):
                    obj = m.get(kind) or {}
                    text = obj.get("caption")
                    media.append({
                        "media_id": obj.get("id"),
                        "filename": obj.get("filename") or f"{kind}-{obj.get('id', 'file')}",
                        "content_type": obj.get("mime_type") or "application/octet-stream",
                    })
                elif kind == "button":
                    text = (m.get("button") or {}).get("text")
                else:
                    continue
                out.append({
                    "id": m.get("id"),
                    "phone": "+" + str(m.get("from", "")).lstrip("+"),
                    "name": names.get(m.get("from")),
                    "text": text,
                    "media": media,
                    "reply_to": (m.get("context") or {}).get("id"),
                    "timestamp": m.get("timestamp"),
                })
    return out


def fetch_media(media_id: str) -> tuple[bytes, str]:
    """Download an inbound Meta media file: look up its URL, then fetch it with the token."""
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    base = os.getenv("WHATSAPP_API_URL", "https://graph.facebook.com/v20.0").rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/{media_id}", headers=headers), timeout=20) as r:  # noqa: S310
            meta = json.loads(r.read().decode("utf-8"))
        with urllib.request.urlopen(urllib.request.Request(meta["url"], headers=headers), timeout=60) as r:  # noqa: S310
            return r.read(), meta.get("mime_type", "application/octet-stream")
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        raise WhatsAppDeliveryError(f"Could not download the WhatsApp media: {exc}") from exc

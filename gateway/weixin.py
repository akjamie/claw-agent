"""
WeChat (Weixin) iLink Bot adapter.

Connects to Tencent's iLink Bot API for personal WeChat accounts.
- QR login: scan to authenticate, credentials saved to config
- Long-poll: getupdates for inbound messages (no HTTP server needed)
- Send: text replies via sendmessage API

Design based on hermes-agent's iLink implementation.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import qrcode as _qrcode_lib
    QRCODE_AVAILABLE = True
except ImportError:
    _qrcode_lib = None  # type: ignore[assignment]
    QRCODE_AVAILABLE = False

from gateway.platform_base import (
    PlatformAdapter,
    IncomingMessage,
    OutgoingMessage,
    MessageHandler,
)
from gateway.config import PlatformConfig

logger = logging.getLogger(__name__)

# iLink API endpoints
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"

# Timeouts
LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000

# Retry config
MAX_CONSECUTIVE_FAILURES = 5
RETRY_DELAY_SECONDS = 2

# iLink app constants
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"

# Message types
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

# Item types
ITEM_TEXT = 1


def _make_ssl_connector() -> Optional[Any]:
    """Return a TCPConnector with certifi CA bundle if available."""
    try:
        import ssl
        import certifi
    except ImportError:
        return None
    if not AIOHTTP_AVAILABLE:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


async def _api_post(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: str,
    timeout_ms: int = API_TIMEOUT_MS,
) -> Dict[str, Any]:
    """POST to iLink API and return the JSON response."""
    url = f"{base_url}/{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "token": token,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    token: Optional[str] = None,
    timeout_ms: int = API_TIMEOUT_MS,
) -> Dict[str, Any]:
    """GET from iLink API and return the JSON response."""
    url = f"{base_url}/{endpoint}"
    headers = {}
    if token:
        headers["token"] = token
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        raw = await resp.text()
        if not resp.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
        return json.loads(raw)


async def qr_login(
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
) -> Optional[Dict[str, str]]:
    """
    Run the interactive iLink QR login flow.

    Displays a QR code in the terminal. User scans with WeChat to authenticate.
    Returns a credential dict on success: {account_id, token, base_url, user_id}
    Returns None if login fails or times out.
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for WeChat QR login. Install with: pip install aiohttp")

    connector = _make_ssl_connector()
    async with aiohttp.ClientSession(trust_env=True, connector=connector) as session:
        # Step 1: Get QR code
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.error("Failed to fetch QR code: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("QR response missing qrcode field")
            return None

        # The full URL is what WeChat needs to scan
        qr_scan_data = qrcode_url if qrcode_url else qrcode_value

        # Display QR code
        print("\n请使用微信扫描以下二维码 (Scan with WeChat):")
        if qrcode_url:
            print(f"  {qrcode_url}")
        _print_qr_terminal(qr_scan_data)

        # Step 2: Poll for scan status
        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                logger.warning("QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")

            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\n已扫码，请在微信里确认 (Scanned, please confirm in WeChat)...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期 (QR expired too many times)")
                    return None
                print(f"\n二维码已过期，正在刷新... (Refreshing QR {refresh_count}/3)")
                qr_resp, qrcode_value, qrcode_url, qr_scan_data = await _refresh_qr(
                    session, bot_type
                )
                if not qrcode_value:
                    return None
                _print_qr_terminal(qr_scan_data)
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")

                if not account_id or not token:
                    logger.error("QR confirmed but credentials incomplete")
                    return None

                print(f"\n✓ 登录成功 (Login successful)! Account: {account_id[:8]}...")
                return {
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "user_id": user_id,
                }

            await asyncio.sleep(2)

        print("\n登录超时 (Login timed out)")
        return None


async def _refresh_qr(
    session: "aiohttp.ClientSession",
    bot_type: str,
) -> tuple:
    """Refresh an expired QR code. Returns (resp, qrcode_value, qrcode_url, scan_data)."""
    try:
        qr_resp = await _api_get(
            session,
            base_url=ILINK_BASE_URL,
            endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
            timeout_ms=QR_TIMEOUT_MS,
        )
        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        qr_scan_data = qrcode_url if qrcode_url else qrcode_value
        if qrcode_url:
            print(f"  {qrcode_url}")
        return qr_resp, qrcode_value, qrcode_url, qr_scan_data
    except Exception as exc:
        logger.error("QR refresh failed: %s", exc)
        return {}, "", "", ""


def _print_qr_terminal(data: str) -> None:
    """Print QR code as ASCII art in the terminal."""
    if not data:
        return
    if not QRCODE_AVAILABLE:
        print("  (Install 'qrcode' package for terminal QR display)")
        return
    try:
        qr = _qrcode_lib.QRCode()
        qr.add_data(data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as exc:
        print(f"  (QR render failed: {exc})")


class WeixinAdapter(PlatformAdapter):
    """WeChat adapter using iLink Bot API (long-poll based)."""

    def __init__(self, config: PlatformConfig):
        self._config = config
        self._running = False
        self._handler: Optional[MessageHandler] = None
        self._session: Optional["aiohttp.ClientSession"] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._consecutive_failures = 0

    @property
    def platform_name(self) -> str:
        return "weixin"

    @property
    def token(self) -> str:
        """Read token from env var WEIXIN_TOKEN, fallback to config settings."""
        return os.environ.get("WEIXIN_TOKEN", "") or self._config.settings.get("token", "")

    @property
    def account_id(self) -> str:
        """Read account_id from env var WEIXIN_ACCOUNT_ID, fallback to config settings."""
        return os.environ.get("WEIXIN_ACCOUNT_ID", "") or self._config.settings.get("account_id", "")

    @property
    def base_url(self) -> str:
        """Read base_url from env var WEIXIN_BASE_URL, fallback to config settings."""
        url = os.environ.get("WEIXIN_BASE_URL", "") or self._config.settings.get("base_url", ILINK_BASE_URL)
        return url.rstrip("/")

    def is_running(self) -> bool:
        return self._running

    async def start(self, handler: MessageHandler) -> None:
        if self._running:
            return

        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp is required for WeChat iLink adapter")

        if not self.token:
            raise RuntimeError(
                "WeChat token not configured. Run 'claw gateway login' first."
            )

        self._handler = handler
        connector = _make_ssl_connector()
        self._session = aiohttp.ClientSession(trust_env=True, connector=connector)
        self._running = True
        self._consecutive_failures = 0

        # Start long-poll loop as a background task
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "[weixin] Started iLink adapter (account=%s, base=%s)",
            self.account_id[:8] if self.account_id else "?",
            self.base_url,
        )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._poll_task = None
        logger.info("[weixin] Stopped iLink adapter")

    async def send_message(self, message: OutgoingMessage) -> bool:
        if not self._session or not self.token:
            return False
        try:
            import uuid
            client_id = f"claw-weixin-{uuid.uuid4().hex}"
            await _api_post(
                self._session,
                base_url=self.base_url,
                endpoint=EP_SEND_MESSAGE,
                payload={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": message.user_id,
                        "client_id": client_id,
                        "message_type": MSG_TYPE_BOT,
                        "message_state": MSG_STATE_FINISH,
                        "item_list": [
                            {
                                "type": ITEM_TEXT,
                                "text_item": {"content": message.content},
                            }
                        ],
                    }
                },
                token=self.token,
            )
            return True
        except Exception as exc:
            logger.error("[weixin] send_message failed: %s", exc)
            return False

    async def _poll_loop(self) -> None:
        """Long-poll getupdates loop."""
        sync_buf = ""

        while self._running:
            try:
                payload: Dict[str, Any] = {
                    "app_id": ILINK_APP_ID,
                    "channel_version": CHANNEL_VERSION,
                    "timeout": LONG_POLL_TIMEOUT_MS,
                }
                if sync_buf:
                    payload["get_updates_buf"] = sync_buf

                resp = await _api_post(
                    self._session,
                    base_url=self.base_url,
                    endpoint=EP_GET_UPDATES,
                    payload=payload,
                    token=self.token,
                    timeout_ms=LONG_POLL_TIMEOUT_MS + 5000,
                )

                self._consecutive_failures = 0

                # Update sync buffer for next poll
                new_buf = resp.get("get_updates_buf")
                if new_buf:
                    sync_buf = str(new_buf)

                # Process messages
                messages = resp.get("msg_list") or []
                for msg_data in messages:
                    await self._handle_incoming(msg_data)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._consecutive_failures += 1
                logger.warning(
                    "[weixin] poll error (%d/%d): %s",
                    self._consecutive_failures,
                    MAX_CONSECUTIVE_FAILURES,
                    exc,
                )
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error("[weixin] Too many consecutive failures, stopping")
                    self._running = False
                    break
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    async def _handle_incoming(self, msg_data: Dict[str, Any]) -> None:
        """Process a single incoming message from getupdates."""
        msg = msg_data.get("msg") or msg_data
        msg_type = msg.get("message_type", 0)

        # Only process user messages
        if msg_type != MSG_TYPE_USER:
            return

        # Extract text content from item_list
        item_list = msg.get("item_list") or []
        text_parts: List[str] = []
        for item in item_list:
            if item.get("type") == ITEM_TEXT:
                text_item = item.get("text_item") or {}
                content = text_item.get("content", "")
                if content:
                    text_parts.append(content)

        if not text_parts:
            return

        content = "\n".join(text_parts)
        from_user = str(msg.get("from_user_id") or "")
        to_user = str(msg.get("to_user_id") or "")
        client_id = str(msg.get("client_id") or "")

        incoming = IncomingMessage(
            platform="weixin",
            user_id=from_user,
            username=from_user,
            content=content,
            message_id=client_id,
            timestamp=time.time(),
            raw=msg_data,
        )

        # Dispatch to handler
        if self._handler:
            try:
                reply = await self._handler(incoming)
                if reply:
                    outgoing = OutgoingMessage(
                        platform="weixin",
                        user_id=from_user,
                        content=reply,
                        reply_to=client_id,
                        extra={},
                    )
                    await self.send_message(outgoing)
            except Exception as exc:
                logger.error("[weixin] handler error for %s: %s", from_user[:8], exc)

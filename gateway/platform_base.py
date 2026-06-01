"""
Abstract base class for messaging platform adapters.

Each platform (WeChat, Telegram, etc.) implements this ABC to provide
a unified interface for the gateway to receive and send messages.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional
import time


@dataclass
class IncomingMessage:
    """Normalized incoming message from any platform."""

    platform: str
    user_id: str
    username: str
    content: str
    message_id: str
    timestamp: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)
    """Original platform-specific payload for advanced use."""


@dataclass
class OutgoingMessage:
    """Normalized outgoing message to any platform."""

    platform: str
    user_id: str
    content: str
    reply_to: Optional[str] = None
    """message_id this is replying to, if applicable."""
    extra: dict = field(default_factory=dict)
    """Platform-specific extras (e.g. media, buttons)."""


# Type alias for the message handler callback
MessageHandler = Callable[[IncomingMessage], Coroutine[Any, Any, Optional[str]]]


class PlatformAdapter(ABC):
    """Abstract base for all platform adapters."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Unique identifier for this platform (e.g. 'weixin')."""
        ...

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        """Start listening for messages. Call handler for each incoming message.

        Args:
            handler: async callback that receives IncomingMessage and returns
                     an optional reply string.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the adapter and release resources."""
        ...

    @abstractmethod
    async def send_message(self, message: OutgoingMessage) -> bool:
        """Send a message through this platform.

        Returns:
            True if sent successfully, False otherwise.
        """
        ...

    @abstractmethod
    def is_running(self) -> bool:
        """Return True if the adapter is actively listening."""
        ...

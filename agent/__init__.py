"""
Claw Agent — interactive AI agent runtime.

Provides the `claw chat` command with multi-turn conversation,
MCP tool integration, context compression, and SQLite persistence.
"""

from agent.config import AgentConfig, load_agent_config, save_agent_config
from agent.messages import Message, ToolCall, canonical_args, COMPRESSION_SUMMARY_TOOL_NAME
from agent.llm_client import LLMClient, StreamResult, StreamStatus, ProviderHTTPError
from agent.persistence import (
    SqlitePersistence,
    Session,
    PersistenceFailure,
    SessionNotFound,
    AmbiguousSession,
)
from agent.tool_registry import (
    ToolRegistry,
    ToolDef,
    _NEVER_PARALLEL,
    _PARALLEL_SAFE,
    _PATH_SCOPED,
)
from agent.guardrails import (
    GuardrailsController,
    GuardrailLedger,
    GuardrailHalt,
    tool_hash,
)
from agent.tool_dispatch import ToolDispatcher
from agent.compressor import ContextCompressor, CompressionResult
from agent.title_generator import TitleGenerator
from agent.loop import AgentLoop

__all__ = [
    # config
    "AgentConfig",
    "load_agent_config",
    "save_agent_config",
    # messages
    "Message",
    "ToolCall",
    "canonical_args",
    "COMPRESSION_SUMMARY_TOOL_NAME",
    # llm
    "LLMClient",
    "StreamResult",
    "StreamStatus",
    "ProviderHTTPError",
    # persistence
    "SqlitePersistence",
    "Session",
    "PersistenceFailure",
    "SessionNotFound",
    "AmbiguousSession",
    # tools
    "ToolRegistry",
    "ToolDef",
    "_NEVER_PARALLEL",
    "_PARALLEL_SAFE",
    "_PATH_SCOPED",
    "ToolDispatcher",
    # guardrails
    "GuardrailsController",
    "GuardrailLedger",
    "GuardrailHalt",
    "tool_hash",
    # compression
    "ContextCompressor",
    "CompressionResult",
    # title
    "TitleGenerator",
    # loop
    "AgentLoop",
]

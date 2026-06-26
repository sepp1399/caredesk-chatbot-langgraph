"""IvrToDigital agent — phone collection → lookup → SMS handoff."""

import logging
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from app.agents.llms import llm
from app.tools.ivr_to_digital import IVR_TO_DIGITAL_TOOLS
from app.tools.shared.transfer import transfer_to_flow
from app.i18n import apply_language, system_prompt_suffix

logger = logging.getLogger("caredesk_lg.ivr_to_digital")

_PROMPT_PATH = Path(__file__).parent / "ivr_to_digital_prompt.md"


def _load_prompt() -> str:
    return apply_language(_PROMPT_PATH.read_text(encoding="utf-8")) + system_prompt_suffix()


SYSTEM_PROMPT: str = _load_prompt()


def reload_prompt() -> int:
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = _load_prompt()
    return len(SYSTEM_PROMPT)


def _pre_model_hook(state: dict) -> dict:
    return {
        "llm_input_messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            *state["messages"],
        ],
    }


_memory = MemorySaver()

ivr_to_digital_agent = create_react_agent(
    model=llm,
    tools=[*IVR_TO_DIGITAL_TOOLS, transfer_to_flow],
    pre_model_hook=_pre_model_hook,
    checkpointer=_memory,
)
logger.info("IvrToDigital agent ready")


def extract_replies(messages: list, last_human_idx: int) -> list[str]:
    return [
        (m.content or "").strip()
        for m in messages[last_human_idx + 1:]
        if isinstance(m, AIMessage) and (m.content or "").strip()
    ]

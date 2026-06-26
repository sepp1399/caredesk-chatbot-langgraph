"""Patient-registration agent — new-patient enrolment.

Single-tool agent: collect → validate → register. No FSM snapshot needed
because the conversation IS the state — the LLM tracks the form fields
across turns and only invokes `register_patient` once everything is
confirmed by the patient.
"""

import logging
from pathlib import Path

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from app.agents.llms import llm
from app.tools.patient_registration import PATIENT_REGISTRATION_TOOLS
from app.tools.shared.transfer       import transfer_to_flow
from app.i18n import apply_language, system_prompt_suffix

logger = logging.getLogger("caredesk_lg.patient_registration")

_PROMPT_PATH = Path(__file__).parent / "patient_registration_prompt.md"


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

patient_registration_agent = create_react_agent(
    model=llm,
    tools=[*PATIENT_REGISTRATION_TOOLS, transfer_to_flow],
    pre_model_hook=_pre_model_hook,
    checkpointer=_memory,
)
logger.info("PatientRegistration agent ready")


def extract_replies(messages: list, last_human_idx: int) -> list[str]:
    return [
        (m.content or "").strip()
        for m in messages[last_human_idx + 1:]
        if isinstance(m, AIMessage) and (m.content or "").strip()
    ]

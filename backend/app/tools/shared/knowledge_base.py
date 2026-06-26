"""Shared knowledge-base tool — in-memory demo set.

Any agent can import and bind `search_knowledge_base`. Each chunk
carries one entry per supported language under `content_by_lang`.
`_retrieve()` picks the entry matching `settings.bot_lang` and falls
back to Italian when a translation is missing, so the inline FAQ
reply (and any agent that calls this tool directly) renders in the
configured language without an extra LLM translation step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.tools import tool

from app.i18n import current_lang

logger = logging.getLogger("caredesk_lg.kb")


@dataclass
class _Chunk:
    topic_by_lang:   dict[str, str]
    content_by_lang: dict[str, str]
    doc_type:        str = "faq"

    def topic(self, lang: str | None = None) -> str:
        return (
            self.topic_by_lang.get((lang or current_lang()))
            or self.topic_by_lang.get("it")
            or next(iter(self.topic_by_lang.values()), "")
        )

    def content(self, lang: str | None = None) -> str:
        return (
            self.content_by_lang.get((lang or current_lang()))
            or self.content_by_lang.get("it")
            or next(iter(self.content_by_lang.values()), "")
        )


_KB: list[_Chunk] = [
    _Chunk(
        topic_by_lang={
            "it": "Orari di apertura",
            "en": "Opening hours",
        },
        content_by_lang={
            "it": "L'Ospedale Salus è aperto dal lunedì al venerdì 08:00–20:00 "
                  "e il sabato 08:00–13:00. Festivi chiuso.",
            "en": "Ospedale Salus is open Monday–Friday 08:00–20:00 and "
                  "Saturday 08:00–13:00. Closed on public holidays.",
        },
    ),
    _Chunk(
        topic_by_lang={
            "it": "Indirizzo e parcheggio",
            "en": "Address and parking",
        },
        content_by_lang={
            "it": "C.so Galileo Ferraris 247, Torino. Metro Marconi, bus 4/14/18. "
                  "Parcheggio interno a pagamento.",
            "en": "C.so Galileo Ferraris 247, Turin. Marconi metro stop, buses "
                  "4/14/18. Paid on-site parking.",
        },
    ),
    _Chunk(
        topic_by_lang={
            "it": "Convenzioni",
            "en": "Insurance partnerships",
        },
        content_by_lang={
            "it": "Convenzionato con 250+ piani assicurativi (Allianz, Generali, "
                  "Unisalute, Faschim, Casagit Salute, Blue Assistance e altri).",
            "en": "We accept 250+ insurance plans (Allianz, Generali, Unisalute, "
                  "Faschim, Casagit Salute, Blue Assistance and others).",
        },
    ),
    _Chunk(
        topic_by_lang={
            "it": "Ritiro referti",
            "en": "Reports collection",
        },
        content_by_lang={
            "it": "Referti disponibili dopo 24–72 ore lavorative. Ritiro in "
                  "segreteria 9–18 o area riservata online.",
            "en": "Reports are ready within 24–72 working hours. Pick them up "
                  "at the front desk between 9:00 and 18:00, or download them "
                  "from your online patient area.",
        },
    ),
    _Chunk(
        topic_by_lang={
            "it": "Disdetta",
            "en": "Cancellation",
        },
        content_by_lang={
            "it": "Le disdette devono essere comunicate almeno 24 ore prima "
                  "dell'appuntamento; oltre quel termine può scattare la penale.",
            "en": "Cancellations must be requested at least 24 hours before the "
                  "appointment; later than that a penalty fee may apply.",
        },
    ),
    _Chunk(
        topic_by_lang={
            "it": "Preparazione elettrocardiogramma",
            "en": "Electrocardiogram preparation",
        },
        content_by_lang={
            "it": "ECG a riposo: nessuna preparazione, nessun digiuno. "
                  "ECG sotto sforzo: evitare pasti pesanti 2–3 ore prima.",
            "en": "Resting ECG: no preparation, no fasting required. Stress ECG: "
                  "avoid heavy meals in the 2–3 hours before the test.",
        },
        doc_type="preparation",
    ),
    _Chunk(
        topic_by_lang={
            "it": "Preparazione holter",
            "en": "Holter monitor preparation",
        },
        content_by_lang={
            "it": "Holter cardiaco 24–48 ore. Pelle pulita, no creme. Non bagnare "
                  "il dispositivo (no doccia/bagno).",
            "en": "Cardiac Holter monitoring lasts 24–48 hours. Wear it on clean "
                  "skin, no creams. Do not get the device wet (no shower/bath).",
        },
        doc_type="preparation",
    ),
]


@dataclass
class _RenderedChunk:
    """Lang-resolved view returned by `_retrieve()` — keeps the inline-info
    reply in router.py unchanged (it reads `.content` and `.topic`)."""
    topic:    str
    content:  str
    doc_type: str


def _retrieve(query: str, doc_type: str, top_k: int = 3) -> list[_RenderedChunk]:
    lang = current_lang()
    q_terms = {t.lower() for t in query.split() if len(t) > 3}
    scored: list[tuple[float, _Chunk]] = []
    for c in _KB:
        if c.doc_type != doc_type:
            continue
        # Score against BOTH languages so an English query still hits an
        # Italian chunk when the English translation is missing (and vice
        # versa). That way a multilingual install can search either way.
        text_lc = " ".join([
            c.topic("it"), c.topic("en"),
            c.content("it"), c.content("en"),
        ]).lower()
        overlap = sum(1 for t in q_terms if t in text_lc)
        score = overlap / max(len(q_terms), 1) if q_terms else 0.0
        scored.append((score, c))
    scored.sort(key=lambda p: p[0], reverse=True)
    picked = [c for s, c in scored[:top_k] if s > 0]
    if not picked:
        picked = [c for c in _KB if c.doc_type == doc_type][:top_k]
    return [
        _RenderedChunk(
            topic=c.topic(lang), content=c.content(lang), doc_type=c.doc_type,
        )
        for c in picked
    ]


@tool
def search_knowledge_base(
    query: str,
    doc_type: Literal["preparation", "faq", "operator_doc"],
) -> str:
    """
    Search the clinic knowledge base for grounded answers.

    Args:
        query:    Search query derived from the patient's question.
        doc_type: 'preparation' (exam prep), 'faq' (general questions),
                  'operator_doc' (internal staff docs).
    """
    chunks = _retrieve(query, doc_type)
    if not chunks:
        return "No relevant information found in the knowledge base."
    parts = [
        f"[{i}] topic={c.topic} | doc_type={c.doc_type}\n{c.content}"
        for i, c in enumerate(chunks, 1)
    ]
    logger.info("KB | doc_type=%s chunks=%d", doc_type, len(chunks))
    return "\n\n---\n\n".join(parts)

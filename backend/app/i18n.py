"""i18n bundles for static bot messages, keyed by `settings.bot_lang`.

The agent system prompts under `app/agents/*/` are loaded from disk in
their native (Italian) form; rather than translating ~40 KB of prompt
content per language, we append a small `language_directive` to the
system prompt at load time. The LLM follows the directive and answers
in the configured language regardless of what the prompt itself says.

Everything that the router emits directly to the patient (welcome menu,
"didn't understand" fallback, info menu intro / no-match / followup,
quick-reply chip labels, cross-flow kickoff strings) goes through the
bundles below — those are static (not LLM-generated) so they must be
translated up-front.

Add a new language by adding a new key to `_BUNDLES` with the same shape
as `"it"`. Anything left out falls back to Italian.
"""

from __future__ import annotations

from typing import Any

from app.config import settings


_BUNDLES: dict[str, dict[str, Any]] = {
    "it": {
        "welcome_lines": [
            "Ciao, sono l'assistente virtuale dell'ospedale Salus",
            (
                "Sono a tua disposizione per darti supporto nella prenotazione di un "
                "nuovo appuntamento, ripianificare o cancellare i tuoi appuntamenti. "
                "Per procedere, seleziona uno dei tasti qui sotto o digita ciò di cui "
                "hai bisogno in un unico messaggio."
            ),
        ],
        "welcome_quick_replies": [
            {"label": "1. Prenota",          "value": "Prenota"},
            {"label": "2. Disdici o sposta", "value": "Disdici o sposta"},
            {"label": "3. Informazioni",     "value": "Informazioni"},
        ],
        "fallback_didnt_understand": "Non sono sicuro di aver capito. ",
        "fallback_routing_error":   "Errore interno di routing. ",
        "info_menu_intro": (
            "Certo, su cosa vorresti informazioni? Posso aiutarti su orari, "
            "sedi, convenzioni, referti, disdette e preparazione esami."
        ),
        "info_no_match": (
            "Non ho informazioni puntuali su questo. Puoi contattare la "
            "segreteria della clinica per maggiori dettagli."
        ),
        "info_followup": "Vuoi sapere altro?",
        "info_topics": [
            {"label": "Orari e sedi",       "value": "orari sedi indirizzo"},
            {"label": "Convenzioni",        "value": "convenzioni assicurazioni"},
            {"label": "Referti",            "value": "ritiro referti"},
            {"label": "Disdetta",           "value": "disdetta penale"},
            {"label": "Preparazione esami", "value": "preparazione esame"},
        ],
        "kickoff": {
            "lab_booking":          "voglio prenotare",
            "manage_reservations":  "vorrei gestire un appuntamento",
            "patient_registration": "vorrei registrarmi",
            "lead_creation":        "vorrei essere richiamato",
            "ivr_to_digital":       "mi mandate il link?",
        },
        # Long form name of the language — substituted into each agent's
        # `## Lingua` section at load time via `apply_language()`. This is
        # the single source of truth: the MD prompts use {LANG_NAME} and
        # we fill it in here.
        "lang_name":             "italiano",

        # Phrases the bot must NOT use because they read as scripted /
        # robotic. Forms part of the tone suffix appended below.
        "robot_phrases": (
            "Gentile paziente / Gentilissimo / La ringrazio infinitamente / "
            "Sarà nostra premura / La informo che / In riferimento a quanto / "
            "Confermo quanto segue."
        ),
        # Phrases that sound natural — used as positive examples.
        "natural_examples": (
            "Ok! / Perfetto. / Allora, ti propongo… / Un attimo che "
            "controllo. / Ecco cosa ho trovato… / Vediamo. / Ricevuto."
        ),
        # Short "I'm checking on it" lines emitted BEFORE a slow tool call.
        "filler_examples": (
            "Un attimo, controllo le disponibilità… / Vediamo cosa c'è "
            "disponibile… / Sto verificando, un momento. / Un momento, "
            "guardo subito. / Attendi un istante, sto controllando."
        ),
    },

    "en": {
        "welcome_lines": [
            "Hi, I'm the virtual assistant of Ospedale Salus.",
            (
                "I can help you book a new appointment, reschedule or cancel "
                "an existing one. To get started, tap one of the buttons "
                "below or just type what you need in a single message."
            ),
        ],
        "welcome_quick_replies": [
            {"label": "1. Book",                 "value": "Book"},
            {"label": "2. Cancel or reschedule", "value": "Cancel or reschedule"},
            {"label": "3. Information",          "value": "Information"},
        ],
        "fallback_didnt_understand": "I'm not sure I understood. ",
        "fallback_routing_error":   "Internal routing error. ",
        "info_menu_intro": (
            "Sure — what would you like to know? I can help with opening "
            "hours, locations, insurance, reports, cancellations, and exam "
            "preparation."
        ),
        "info_no_match": (
            "I don't have specific information on this. You can contact "
            "the clinic's front desk for more details."
        ),
        "info_followup": "Anything else?",
        "info_topics": [
            {"label": "Hours & locations", "value": "hours locations address"},
            {"label": "Insurance",         "value": "insurance plans"},
            {"label": "Reports",           "value": "reports collection"},
            {"label": "Cancellation",      "value": "cancellation policy"},
            {"label": "Exam preparation",  "value": "exam preparation"},
        ],
        "kickoff": {
            "lab_booking":          "I'd like to book an appointment",
            "manage_reservations":  "I'd like to manage an appointment",
            "patient_registration": "I'd like to register",
            "lead_creation":        "I'd like a callback",
            "ivr_to_digital":       "can you send me the link?",
        },
        "lang_name":             "English",

        "robot_phrases": (
            "Dear patient / We kindly inform you / It will be our care to / "
            "I am pleased to inform you that / Please be advised that."
        ),
        "natural_examples": (
            "Ok! / Got it. / Let's see… / One moment while I check. / "
            "Here's what I found. / Sounds good. / Sure thing."
        ),
        "filler_examples": (
            "One moment, checking availability… / Let me take a look… / "
            "Hang on, I'm pulling up the slots… / Just a sec, looking "
            "that up. / Give me a moment, checking now."
        ),
    },
}


def _bundle() -> dict[str, Any]:
    lang = (settings.bot_lang or "it").lower()
    return _BUNDLES.get(lang) or _BUNDLES["it"]


def welcome_lines() -> list[str]:
    return list(_bundle()["welcome_lines"])


def welcome_quick_replies() -> list[dict]:
    return [dict(qr) for qr in _bundle()["welcome_quick_replies"]]


def info_topics() -> list[dict]:
    return [dict(t) for t in _bundle()["info_topics"]]


def info_menu_intro() -> str:
    return _bundle()["info_menu_intro"]


def info_no_match() -> str:
    return _bundle()["info_no_match"]


def info_followup() -> str:
    return _bundle()["info_followup"]


def fallback_didnt_understand() -> str:
    return _bundle()["fallback_didnt_understand"]


def fallback_routing_error() -> str:
    return _bundle()["fallback_routing_error"]


def kickoff(flow: str) -> str:
    return _bundle()["kickoff"].get(flow, "")


_TONE_TEMPLATE = (
    "\n\n--- TONE ---\n"
    "Sound human, not scripted. A few practical rules:\n"
    "- Vary your openings; do not start every reply with the same word.\n"
    "  Sometimes lead straight with the substance, no preamble.\n"
    "- Prefer short, direct sentences. Use longer ones only when there's "
    "real information density (slot proposals, final summaries).\n"
    "- Use light, conversational acknowledgements when the patient "
    "confirms or moves the booking forward — e.g. {natural_examples} — "
    "rather than restating the whole context.\n"
    "- Add small natural connectives where they fit (transitions, "
    "verbal nods) so the reply reads like a person typing on chat, "
    "not a form letter.\n"
    "- Warm but not saccharine. Never use scripted formal forms such as: "
    "{robot_phrases}\n"
    "- Stay concise: if one short sentence does the job, send one short "
    "sentence. Do not pad."
)

# Reactivity block — instructs the agent to emit a short "I'm on it"
# line BEFORE potentially slow tool calls (availability search, booking
# writes) so the patient sees activity while the backend call runs. The
# frontend streams tokens as they're produced, so this filler appears
# immediately while the tool result is still loading; we follow up with
# the real reply in the same turn.
#
# Skipped for instant in-memory lookups (insurance/doctor commits) and
# pre-fetched silent INIT calls.
_REACTIVITY_TEMPLATE = (
    "\n\n--- REACTIVITY ---\n"
    "Some tool calls can take a moment (a backend call). To avoid a "
    "silent gap that feels like a stall, emit ONE short, natural status "
    "line FIRST in the same response, THEN call the tool. The frontend "
    "streams your tokens live, so the patient sees the line immediately "
    "while the tool runs.\n"
    "Examples — vary the phrasing, do not repeat the same line:\n"
    "  {filler_examples}\n"
    "Apply this BEFORE calling these tools:\n"
    "  - search_dates / get_new_dates  (availability search)\n"
    "  - book_appointment / request_deferred_appointment  (write)\n"
    "  - list_my_reservations  (loading patient's bookings)\n"
    "  - cancel_reservation / reschedule_reservation  (write)\n"
    "Do NOT add a filler for fast/local operations:\n"
    "  - get_insurance_id_by_insurance_name, search_doctor_names "
    "(commit mode) — in-memory lookups, instant.\n"
    "  - Silent INIT tools the pre_model_hook already pre-fetched for you.\n"
    "Keep it to ONE short line — never two stacked fillers in the same "
    "turn. After the tool returns, continue the conversation normally; "
    "do not narrate that the operation succeeded ('Ho controllato!') — "
    "just present the result."
)


def lang_name() -> str:
    """Long form of the configured language ("italiano" / "English") —
    substituted into the `## Lingua` section of every agent prompt."""
    return _bundle()["lang_name"]


def apply_language(prompt_text: str) -> str:
    """Resolve `{LANG_NAME}` placeholders in an agent's MD prompt.

    Each prompt's `## Lingua` section is templated — substituting the
    placeholder here is how `BOT_LANG` becomes the single source of
    truth for the bot's output language. No override "tricks" at the
    edges of the prompt: the language pin lives in the canonical
    section the LLM reads first.
    """
    return prompt_text.replace("{LANG_NAME}", lang_name())


def system_prompt_suffix() -> str:
    """Text appended to every agent's system prompt at load time.

    Two functional blocks only — language is NOT touched here, that's
    handled by `apply_language()` on the prompt body:
      1. A tone block — natural, conversational replies.
      2. A reactivity block — emit a short status line before slow
         tool calls so the patient sees activity while a backend call
         runs.
    """
    bundle = _bundle()
    tone = _TONE_TEMPLATE.format(
        natural_examples=bundle["natural_examples"],
        robot_phrases=bundle["robot_phrases"],
    )
    reactivity = _REACTIVITY_TEMPLATE.format(
        filler_examples=bundle["filler_examples"],
    )
    return tone + reactivity


def current_lang() -> str:
    return (settings.bot_lang or "it").lower()

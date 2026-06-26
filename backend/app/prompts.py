"""Prompt dictionary loader for `Voicebot_Chatbot_Chiavi.tsv`.

The TSV is structured as: ``instance \\t lang \\t definition \\t translation``.
For every `definition` key (e.g. "Voicebot Lab Booking Welcome") there are
multiple translations, indexed by `(instance, lang)`.

Lookup precedence used by `get_prompt(definition)`:

  1. exact (`settings.prompts_instance`, `settings.prompts_lang`, definition)
  2. any instance with the same language
  3. any (instance, lang) for the same definition (last-resort fallback)

Templates in the TSV use ``[TRD0]`` ... ``[TRD11]`` placeholders. Use
`render(template, TRD0=..., TRD1=...)` to substitute them — missing slots
are left untouched so they're visible in the LLM context.

The loader is silent and tolerant: a missing TSV is logged as a warning,
not raised, so the agent still boots and falls back to its built-in copy.
"""

from __future__ import annotations

import csv
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("caredesk_lg.prompts")


_TRD_RE = re.compile(r"\[TRD(\d{1,2})\]")


@lru_cache(maxsize=1)
def _index() -> dict[str, dict[tuple[str, str], str]]:
    """Build: {definition: {(instance, lang): translation}}."""
    path: Path = settings.prompts_tsv_path
    if not path.exists():
        logger.warning("Prompts TSV not found at %s — get_prompt() will return empty", path)
        return {}

    out: dict[str, dict[tuple[str, str], str]] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration:
            return out
        # Be tolerant to header variations
        cols = {name.strip().lower(): i for i, name in enumerate(header)}
        i_inst = cols.get("instance", 0)
        i_lang = cols.get("lang", 1)
        i_def  = cols.get("definition", 2)
        i_trad = cols.get("translation", 3)

        for row in reader:
            if len(row) <= max(i_inst, i_lang, i_def, i_trad):
                continue
            inst = row[i_inst].strip()
            lang = row[i_lang].strip()
            defi = row[i_def].strip()
            trad = row[i_trad]
            if not defi or not trad:
                continue
            out.setdefault(defi, {})[(inst, lang)] = trad

    logger.info(
        "Loaded prompts dictionary: %d definitions, %d total rows",
        len(out), sum(len(v) for v in out.values()),
    )
    return out


def get_prompt(
    definition: str,
    *,
    instance: Optional[str] = None,
    lang: Optional[str] = None,
    default: str = "",
) -> str:
    """Resolve a translation with graceful fallback. Returns `default` if
    nothing matches.
    """
    inst = instance or settings.prompts_instance
    ln   = lang or settings.prompts_lang
    idx  = _index().get(definition)
    if not idx:
        return default

    if (inst, ln) in idx:
        return idx[(inst, ln)]
    same_lang = [v for (i, l), v in idx.items() if l == ln]
    if same_lang:
        return same_lang[0]
    return next(iter(idx.values()), default)


def render(template: str, **values) -> str:
    """Replace [TRD0]..[TRDn] placeholders with values keyed by TRD0..TRDn.

    Missing keys are left as-is in the output.
    """
    def _sub(m: re.Match) -> str:
        key = f"TRD{m.group(1)}"
        val = values.get(key)
        return str(val) if val is not None else m.group(0)
    return _TRD_RE.sub(_sub, template)


def reload() -> int:
    """Drop the cache so the next call re-reads the TSV. Returns row count."""
    _index.cache_clear()
    idx = _index()
    return sum(len(v) for v in idx.values())

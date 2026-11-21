"""The default-low text verbosity gate (T-C3-013).

Upstream ships this as a session plugin (core/plugin/verbosity.ts, 58
lines): on the context/compaction/generate/title hooks it fills
``options.textVerbosity = "low"`` whenever the resolved OpenAI model id
passes ``supportsVerbosity`` and neither model.settings nor variant.settings
already set it. py has no plugin host (the T-C3-011 cut precedent) and no
model-level settings face, so the whole policy is this one pure function,
applied at the two request-assembly points (``ContextBuilder.build`` and
the compaction summarizer -- session/llm_summarizer.py).

TS mapping (verbosity.ts):

- L45-50 ``supportsVerbosity`` verbatim, in order: ``gpt-6`` anywhere
  supports; a ``-chat``/``-image`` fragment rejects (the specialized faces
  outrank the GPT-5 regexes); else the two GPT-5 regexes -- minor 1-6, or
  the bare/-mini/-nano forms -- each with the ``^ | / | .`` left boundary
  and a ``[.:-] | $`` right boundary. New GPT-5 minors stay unset until
  their support is known (the L48 comment: gpt-5.7 rejects).
- L53 ``modelID.toLowerCase()`` -- the one surviving piece of the
  three-channel ``openAIModelID`` resolution (direct/bedrock-mantle/
  gateways prefix strips are not ported: py's single maas provider means
  ``ModelRef.model`` already is the wire id; plan T-C3-013 cut table).

The caller fills only the unset hole: an explicit request value is never
overwritten (the L28 early return when ``textVerbosity !== undefined``).
"""

from __future__ import annotations

import re
from typing import Literal

#: The upstream three-value enum (TextVerbositySchema, open-responses-options.ts).
_Verbosity = Literal["low", "medium", "high"]

#: verbosity.ts L46: ``gpt-6`` anywhere in the id.
_GPT_6 = "gpt-6"
#: verbosity.ts L47: the specialized chat/image faces never take it.
_SPECIALIZED = ("-chat", "-image")
#: verbosity.ts L49 first regex: minor 1-6 delimited on both sides.
_MINOR_RE = re.compile(r"(?:^|[/.])gpt-5\.[1-6](?:[.:-]|$)")
#: verbosity.ts L49 second regex: the bare/-mini/-nano forms.
_BARE_RE = re.compile(r"(?:^|[/.])gpt-5(?:-(?:mini|nano)(?:[.:-]|$)|$)")


def default_text_verbosity(model: str) -> _Verbosity | None:
    """The default-low gate: ``"low"`` when the model supports it, else None.

    ``model`` is the wire id (``ModelRef.model``); it is normalized with the
    upstream ``toLowerCase()`` before the gate so case never decides support.
    """
    lowered = model.lower()
    if _GPT_6 in lowered:
        return "low"
    if any(fragment in lowered for fragment in _SPECIALIZED):
        return None
    if _MINOR_RE.search(lowered) or _BARE_RE.search(lowered):
        return "low"
    return None

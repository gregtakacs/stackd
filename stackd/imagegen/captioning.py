"""
Masked-path prompt captioning — the ONE reduction both masked render paths share.

Flux.2 Klein (and every distilled inpaint pipeline in this stack) is a caption model,
not an instruction model: 'Replace the red Lamborghini with a blue Audi R8' leaves BOTH
subjects in the conditioning, and the original-object tokens drag the result back toward
the source photo. The masked paths therefore do not feed the user's words to the sampler
raw — they reduce an instruction frame to a plain caption of the WANTED result, and keep
that caption inside the model's effective attention window (CLIP-class text encoders
attend ~77 tokens; the resident Flux.2 graphs run no long-text encoder for the positive,
so the ~77-WORD cap below is an honest working proxy — words are what captions are made
of, and a caption longer than that was an instruction wearing a caption's clothes).

This is imagegen.tools' `_describe_edit_target` moved here verbatim (2026-09), so the MCP
edit_image masked path and the toolbox's /toolbox/jobs render apply EXACTLY the same
text behaviour: the same words must not edit differently in chat than in the editor.
toolbox/api._create calls caption_prompt() at job creation — BEFORE the spec is
persisted — so the job row, the create echo, and the graph all carry the caption that
was actually run, alongside the user's own words under prompt_raw (never silently
overwrite what the user typed; the editor shows both when they differ).

Deliberately dependency-free (stdlib only): toolbox imports this at module level, and
imagegen.tools imports it inside its own heavy MCP-server module — neither should drag
the other's runtime in.
"""

import re

# Instruction frames, lifted verbatim from imagegen/tools.py's masked path.
_EDIT_INSTR_RE = re.compile(
    r"\b(?:replace|swap(?:\s+out)?|change|turn|convert|transform)\b.*?"
    r"\b(?:with|to|into|for)\b\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
_MAKE_IT_RE = re.compile(
    r"^\s*make\s+(?:it|the\s+\S+(?:\s+\S+)?)\s+(?:into\s+|look\s+like\s+)?(.+)",
    re.IGNORECASE | re.DOTALL,
)

# ~77: the token budget every CLIP-class conditioning encoder in this stack shares.
# Words are a conservative proxy (subwords only make the real count tighter), and the
# cap keeps the FRONT of the caption — in a caption, the subject comes first, so a
# truncated head still conditions on the thing, while the tail of a long prompt is
# almost always the instruction scaffolding the reduction was trying to shed anyway.
PROMPT_TOKEN_CAP = 77


def describe_edit_target(prompt: str) -> str:
    """Reduce an instruction-phrased edit prompt to a description of the wanted
    result. 'replace/change/turn X with/to/into Y ...' (and 'make it Y ...') becomes
    'Y ...'. Left unchanged if it doesn't match — already a description, or an
    add/remove edit, whose wording carries no source-object poisoning. A candidate
    shorter than 8 characters is treated as noise and the prompt is left alone."""
    p = (prompt or "").strip()
    m = _EDIT_INSTR_RE.search(p) or _MAKE_IT_RE.match(p)
    if m:
        cand = m.group(1).strip().rstrip(". ").strip()
        if len(cand) >= 8:
            return cand
    return p


def caption_prompt(prompt: str) -> tuple[str, str]:
    """The full masked-path captioning: reduce, then cap. Returns
    (prompt_to_use, note); note is '' exactly when the text came through unchanged —
    callers should add NO extra keys to the spec in that case (the toolbox create
    contract pins the verbatim echo for unchanged prompts, and the editor only ever
    shows the 'sent as' line when this function says it changed something)."""
    p = (prompt or "").strip()
    if not p:
        return p, ""
    reduced = describe_edit_target(p)
    reduced_flag = reduced != p
    words = reduced.split()
    trimmed = len(words) > PROMPT_TOKEN_CAP
    if trimmed:
        reduced = " ".join(words[:PROMPT_TOKEN_CAP])
    if not (reduced_flag or trimmed):
        return p, ""
    if trimmed:
        note = (f"reduced to a plain caption of the wanted result, kept to its first "
                f"{PROMPT_TOKEN_CAP} words (the encoder attends ~{PROMPT_TOKEN_CAP} tokens)")
    else:
        note = "reduced to a plain caption of the wanted result"
    return reduced, note

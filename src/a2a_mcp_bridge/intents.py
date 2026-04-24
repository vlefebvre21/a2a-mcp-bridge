"""Wake-up intent enum for ``agent_send`` (ADR-002).

An intent annotates a message with its delivery semantics. Current v0.6 scope
(Option γ — 5 values declared, binary wake behaviour):

* ``triage`` (default) — "read, reply if relevant, done". Current behaviour.
* ``execute`` — task handoff; recipient continues autonomously until done.
* ``review`` — structured review request (LGTM / REQUEST_CHANGES).
* ``question`` — needs an answer, not a task.
* ``fyi`` — heads up, no action required, no reply expected. **Wake skipped.**

The bridge itself enforces only the wake policy (skip wake for no-wake
intents). Semantic dispatch to specialised skills (``a2a-task-execution``,
``a2a-review-request``) is a Hermes-side concern tracked separately — see
ADR-002 §4 Hermes-side work.
"""

from __future__ import annotations

# All recognised intent values.
# Keep alphabetised for diff stability; add new values here first when
# extending the enum, then wire up behaviour below.
VALID_INTENTS: frozenset[str] = frozenset(
    {"execute", "fyi", "question", "review", "triage"}
)

# Default intent applied when the caller omits the field or passes None.
# Must match the pre-ADR-002 behaviour (wake-up triggers the inbox-triage
# skill on the recipient) so existing callers are unaffected.
DEFAULT_INTENT: str = "triage"

# Intents that DO NOT trigger a webhook wake-up. The message is still
# persisted and still touches the signal file (so ``agent_subscribe`` and
# the next natural ``agent_inbox`` pick it up), but no HTTP wake is POSTed
# to the recipient's gateway — the whole point of the ``fyi`` intent is to
# avoid spawning an expensive LLM session for a notification nobody needs
# to action.
NO_WAKE_INTENTS: frozenset[str] = frozenset({"fyi"})


def normalize_intent(raw: str | None) -> tuple[str, bool]:
    """Normalise a caller-supplied ``intent`` value.

    Args:
        raw: the caller-supplied intent string, or ``None``.

    Returns:
        ``(normalised, downgraded)`` where:
          * ``normalised`` is the value to store (always in :data:`VALID_INTENTS`);
          * ``downgraded`` is ``True`` when the caller passed an unknown value
            that was silently downgraded to :data:`DEFAULT_INTENT`. Callers can
            use this flag to log a WARNING without re-parsing.

    Rules:
        * ``None`` → (``DEFAULT_INTENT``, ``False``). No warning — absence is
          legitimate backward-compat behaviour.
        * A value in :data:`VALID_INTENTS` → (value, ``False``). Pass-through.
        * Anything else → (``DEFAULT_INTENT``, ``True``). Forward-compat
          downgrade as prescribed by ADR-002 §5.3 ("unknown intent →
          downgrade to triage and log a warning rather than reject").
    """
    if raw is None:
        return DEFAULT_INTENT, False
    if raw in VALID_INTENTS:
        return raw, False
    return DEFAULT_INTENT, True


def wakes(intent: str) -> bool:
    """Return ``True`` if ``intent`` triggers a webhook wake-up.

    Assumes ``intent`` has been normalised (i.e. is in :data:`VALID_INTENTS`).
    For defensive callers: an unrecognised value is treated as wake-triggering,
    matching the forward-compat downgrade policy.
    """
    return intent not in NO_WAKE_INTENTS

"""Memory classification and LLM-based extraction (spec §20-26, §63, §69).

The classifier decides what should be remembered and of what type, with
rule-based signals plus optional LLM refinement. The extractor turns a
conversation turn into validated candidate memories. Trivial content never
becomes memory (spec §21): greetings, filler, one-off questions and raw tool
output are ignored by rule, and every candidate carries importance and
confidence estimates (§22/§23) that are kept strictly separate.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import MemoryType
from ..logging.setup import log_debug, log_error

# --------------------------------------------------------------- classifier


class MemoryDecisionType(str, enum.Enum):
    """Classifier verdict for a candidate memory (spec §20)."""

    IGNORE = "ignore"
    FACT = "fact"
    PREFERENCE = "preference"
    STATE = "state"
    GOAL = "goal"
    TASK = "task"
    EPISODIC = "episodic"
    CONVERSATION = "conversation"
    KNOWLEDGE = "knowledge"
    PROJECT_CONTEXT = "project_context"


_DECISION_TO_TYPE: dict[MemoryDecisionType, MemoryType | None] = {
    MemoryDecisionType.IGNORE: None,
    MemoryDecisionType.FACT: MemoryType.FACT,
    MemoryDecisionType.PREFERENCE: MemoryType.PREFERENCE,
    MemoryDecisionType.STATE: MemoryType.STATE,
    MemoryDecisionType.GOAL: MemoryType.GOAL,
    MemoryDecisionType.TASK: MemoryType.TASK,
    MemoryDecisionType.EPISODIC: MemoryType.EPISODIC,
    MemoryDecisionType.CONVERSATION: MemoryType.CONVERSATION,
    MemoryDecisionType.KNOWLEDGE: MemoryType.KNOWLEDGE,
    MemoryDecisionType.PROJECT_CONTEXT: MemoryType.PROJECT_CONTEXT,
}


@dataclass
class MemoryDecision:
    """The classifier's verdict plus scores (spec §20/§22/§23)."""

    decision: MemoryDecisionType
    importance: float = 0.5
    confidence: float = 0.5
    topic: str = ""
    explicitly_confirmed: bool = False
    reason: str = ""


# Explicit user statements make memories confirmed with full confidence (§24).
_EXPLICIT_RE = re.compile(
    r"^(remember(?: that)?|note that|keep in mind|i (?:prefer|use|switched to|am working on)|"
    r"my (?:name|editor|language|project|goal|preference)|from now on)\b",
    re.IGNORECASE,
)

# Correction statements (§67) are high priority and explicit.
_CORRECTION_RE = re.compile(
    r"\b(that'?s wrong|not anymore|no longer|i don'?t use|i actually|correction)\b",
    re.IGNORECASE,
)

# Trivial content that must never be stored (spec §21).
_TRIVIAL_RE = re.compile(
    r"^(hello|hi|hey|thanks|thank you|thx|okay|ok|sure|yes|no|bye|goodbye|good morning|"
    r"good night|what time is it|what'?s the time|what is \d+ *[+*/-] *\d+)\b[\s!.?]*$",
    re.IGNORECASE,
)

# Raw tool output and transient chatter (spec §63).
_TOOL_OUTPUT_RE = re.compile(r"^(ls|cat|grep|pip |npm |docker |git (status|log|diff)|traceback)", re.IGNORECASE)

# Preference signals.
_PREFERENCE_RE = re.compile(
    r"\b(i (?:prefer|like|love|hate|dislike|favor|use|am using)|my (?:favourite|favorite|preference)|"
    r"always (?:use|answer|reply)|never (?:use|answer)|keep (?:it|answers?) (?:short|concise|minimal)|"
    r"i'?ve switched (?:from|to)|i switched (?:from|to))\b",
    re.IGNORECASE,
)

# State/project signals.
_STATE_RE = re.compile(
    r"\b(i'?m (?:currently )?(?:working on|building)|current(?:ly)? project|my project|"
    r"the (?:project|stage|milestone) is)\b",
    re.IGNORECASE,
)

_GOAL_RE = re.compile(r"\b(i want to|i'?m trying to|my goal|goal is|planning to|aim to)\b", re.IGNORECASE)
_TASK_RE = re.compile(r"\b(i need to|i have to|todo|to-do|task:|remind me to)\b", re.IGNORECASE)
_FACT_RE = re.compile(r"\b(i (?:am|own|have|work|live|study)|my (?:name|job|machine|gpu|laptop|os))\b", re.IGNORECASE)

# Topic extraction: quoted names, capitalized phrases, or declared project names.
_TOPIC_RE = re.compile(r"[\"']([\w .-]{2,40})[\"']|\b((?:LifeOS|ARIA|[A-Z][a-zA-Z0-9]{2,}(?:OS|App|Bot|Assistant)))\b")

_EPISODIC_RE = re.compile(
    r"\b(i tried|i attempted|failed|didn'?t work|rejected|decided|chose|gave up|rolled back|"
    r"it turned out|we ended up)\b",
    re.IGNORECASE,
)

_IMPORTANT_RE = re.compile(
    r"\b(critical|important|never forget|always remember|long.?term|architecture|decision|production)\b",
    re.IGNORECASE,
)


class MemoryClassifier:
    """Rule-based classification with importance/confidence estimation (§20).

    Deliberately deterministic: rules handle the common cases cheaply and
    audibly. The extractor can override individual fields with LLM judgment;
    rules remain the safety net that keeps trivial content out (§21).
    """

    def classify(self, content: str, *, source: str = "conversation") -> MemoryDecision:
        text = content.strip()
        if not text:
            return MemoryDecision(MemoryDecisionType.IGNORE, reason="empty content")
        if _TRIVIAL_RE.match(text):
            return MemoryDecision(MemoryDecisionType.IGNORE, reason="trivial content (§21)")
        if _TOOL_OUTPUT_RE.match(text) and len(text) > 200:
            return MemoryDecision(MemoryDecisionType.IGNORE, reason="raw tool output (§63)")

        explicit = bool(_EXPLICIT_RE.match(text)) or source == "user_explicit"
        correction = bool(_CORRECTION_RE.search(text))
        topic_match = _TOPIC_RE.search(text)
        topic = (topic_match.group(1) or topic_match.group(2) or "") if topic_match else ""

        decision = MemoryDecisionType.CONVERSATION
        if _PREFERENCE_RE.search(text):
            decision = MemoryDecisionType.PREFERENCE
        elif _STATE_RE.search(text):
            decision = MemoryDecisionType.STATE
        elif _GOAL_RE.search(text):
            decision = MemoryDecisionType.GOAL
        elif _TASK_RE.search(text):
            decision = MemoryDecisionType.TASK
        elif correction or _EPISODIC_RE.search(text):
            decision = MemoryDecisionType.EPISODIC
        elif _FACT_RE.search(text):
            decision = MemoryDecisionType.FACT
        elif source == "document":
            decision = MemoryDecisionType.KNOWLEDGE

        importance = 0.5
        if decision in {MemoryDecisionType.PREFERENCE, MemoryDecisionType.STATE, MemoryDecisionType.GOAL}:
            importance = 0.7
        elif decision == MemoryDecisionType.TASK:
            importance = 0.6
        elif decision == MemoryDecisionType.EPISODIC:
            importance = 0.6
        if _IMPORTANT_RE.search(text):
            importance = min(importance + 0.2, 1.0)
        if explicit:
            importance = min(importance + 0.1, 1.0)
        if len(text) < 25:
            importance = max(importance - 0.15, 0.1)

        confidence = 1.0 if explicit else (0.7 if decision != MemoryDecisionType.CONVERSATION else 0.5)
        if correction:
            confidence = 1.0
            importance = max(importance, 0.8)

        return MemoryDecision(
            decision=decision,
            importance=round(min(max(importance, 0.0), 1.0), 2),
            confidence=round(confidence, 2),
            topic=topic,
            explicitly_confirmed=explicit,
            reason="rule-based classification",
        )


# --------------------------------------------------------------- extractor


class ExtractionProvider(Protocol):
    """The LLM surface the extractor needs (any aria Provider works)."""

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], on_text: Any = None
    ) -> Any: ...


@dataclass
class CandidateMemory:
    """One validated candidate from the extraction pipeline (spec §25)."""

    type: MemoryDecisionType
    content: str
    importance: float
    confidence: float
    explicitly_confirmed: bool
    topic: str = ""
    source: str = "conversation"
    source_reference: str = ""
    key: str = ""
    value: str = ""


from ..prompts import MEMORY_EXTRACTION_PROMPT

_EXTRACTION_PROMPT = MEMORY_EXTRACTION_PROMPT


def _clamp(value: Any, default: float) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(score, 0.0), 1.0)


class MemoryExtractor:
    """Turns a conversation turn into candidate memories via LLM (§25/§26)."""

    def __init__(self, provider: ExtractionProvider, classifier: MemoryClassifier) -> None:
        self._provider = provider
        self._classifier = classifier

    def extract(self, user_text: str, assistant_text: str) -> list[CandidateMemory]:
        """Run LLM extraction, validate the JSON, fall back to rules (§26)."""
        payload = json.dumps({"user": user_text, "assistant": assistant_text}, ensure_ascii=True)
        prompt = _EXTRACTION_PROMPT + payload
        try:
            response = self._provider.complete([{"role": "user", "content": prompt}], [], on_text=None)
        except Exception as exc:  # noqa: BLE001 - provider failure must not crash
            log_error(f"Memory extractor: provider failed: {exc}")
            return self._rule_fallback(user_text, assistant_text)
        content = str(getattr(response, "content", response) or "")
        candidates = self._parse(content)
        if candidates is None:
            log_error("Memory extractor: malformed JSON from extraction; using rule fallback")
            return self._rule_fallback(user_text, assistant_text)
        return candidates

    def _parse(self, content: str) -> list[CandidateMemory] | None:
        """Parse and validate the LLM JSON (spec §26: validate before use)."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Models often wrap JSON in prose or Markdown fences. Decode the
            # first balanced object rather than using a greedy regex, which
            # consumed multiple objects and caused avoidable fallback errors.
            data = self._decode_embedded_object(content)
            if data is None:
                return None
        if not isinstance(data, dict):
            return None
        memories = data.get("memories")
        if not isinstance(memories, list):
            return None
        candidates: list[CandidateMemory] = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            raw_type = str(item.get("type", "")).strip().lower()
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            try:
                decision_type = MemoryDecisionType(raw_type)
            except ValueError:
                decision_type = MemoryDecisionType.CONVERSATION
            # Never trust LLM scores blindly; clamp and floor trivial items.
            importance = _clamp(item.get("importance"), 0.5)
            confidence = _clamp(item.get("confidence"), 0.5)
            if importance < 0.2:
                continue
            candidates.append(
                CandidateMemory(
                    type=decision_type,
                    content=text,
                    importance=importance,
                    confidence=confidence,
                    explicitly_confirmed=bool(item.get("explicitly_confirmed")),
                    topic=str(item.get("topic", "")).strip(),
                    key=str(item.get("key", "")).strip(),
                    value=str(item.get("value", "")).strip(),
                )
            )
        return candidates

    @staticmethod
    def _decode_embedded_object(content: str) -> Any | None:
        """Extract one balanced JSON object while respecting quoted braces."""
        start = content.find("{")
        while start >= 0:
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(content)):
                character = content[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == '"':
                        in_string = False
                    continue
                if character == '"':
                    in_string = True
                elif character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(content[start : index + 1])
                        except json.JSONDecodeError:
                            break
            start = content.find("{", start + 1)
        return None

    def _rule_fallback(self, user_text: str, assistant_text: str) -> list[CandidateMemory]:
        """Classify the user's message with rules when the LLM path fails."""
        candidates: list[CandidateMemory] = []
        for text in (user_text,):
            decision = self._classifier.classify(text)
            if decision.decision is MemoryDecisionType.IGNORE:
                continue
            candidates.append(
                CandidateMemory(
                    type=decision.decision,
                    content=text,
                    importance=decision.importance,
                    confidence=decision.confidence if not decision.explicitly_confirmed else 1.0,
                    explicitly_confirmed=decision.explicitly_confirmed,
                    topic=decision.topic,
                )
            )
        return candidates

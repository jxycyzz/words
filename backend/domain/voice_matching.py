from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable


_TOKEN_ALIASES = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "won": "1",
    "two": "2",
    "too": "2",
    "to": "2",
    "three": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
    "see": "c",
    "sea": "c",
    "be": "b",
    "bee": "b",
    "you": "u",
    "why": "y",
    "are": "r",
}


@dataclass(frozen=True)
class VoiceChoice:
    key: str
    text: str
    priority: float = 0.0


@dataclass(frozen=True)
class VoiceAlternative:
    key: str
    text: str
    score: float
    lexical_score: float = 0.0
    pronunciation_score: float = 0.0
    acoustic_score: float = 0.0
    qualified: bool = False
    reason: str = ""


@dataclass(frozen=True)
class VoiceMatchResult:
    key: str | None
    score: float = 0.0
    ambiguous: bool = False
    reason: str = "no_match"
    normalized_transcript: str = ""
    alternatives: tuple[VoiceAlternative, ...] = ()


@dataclass(frozen=True)
class _VoiceFeatures:
    normalized: str
    compact: str
    lexical: frozenset[str]
    pronunciation: frozenset[str]
    acoustic: frozenset[str]


@dataclass(frozen=True)
class _VoiceEvidence:
    score: float
    lexical_score: float
    pronunciation_score: float
    acoustic_score: float
    exact: bool
    compact_exact: bool
    qualified: bool
    reason: str


def normalize_voice_text(value: str) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _sound_key(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "", value.casefold())
    text = re.sub(r"c(?=[eiy])", "s", text)
    text = re.sub(r"g(?=[eiy])", "j", text)
    replacements = (
        ("ph", "f"),
        ("ck", "k"),
        ("qu", "kw"),
        ("x", "ks"),
        ("c", "k"),
        ("z", "s"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return re.sub(r"(.)\1+", r"\1", text)


def _pronunciation_keys(value: str) -> set[str]:
    text = re.sub(r"[^a-z0-9]+", "", value.casefold())
    if not text:
        return set()

    text = re.sub(r"c(?=[eiy])", "s", text)
    text = re.sub(r"g(?=[eiy])", "j", text)

    replacements = (
        ("eigh", "a"),
        ("igh", "i"),
        ("ee", "i"),
        ("ea", "i"),
        ("ei", "i"),
        ("ie", "i"),
        ("ey", "i"),
        ("y", "i"),
        ("ph", "f"),
        ("ck", "k"),
        ("qu", "kw"),
        ("q", "k"),
        ("c", "k"),
        ("x", "ks"),
        ("z", "s"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    if len(text) > 3 and text.endswith("e") and not text.endswith(("ie", "ee")):
        text = text[:-1]
    text = re.sub(r"([ei])\1+", r"\1", text)

    variants = {text}
    if len(text) > 3 and text.endswith("ng"):
        variants.add(text[:-1])
    return {variant for variant in variants if variant}


def _acoustic_key(value: str) -> str:
    """Return a broad English sound shape for noisy short-word ASR output."""

    text = re.sub(r"[^a-z]+", "", value.casefold())
    if not text:
        return ""

    text = re.sub(r"c(?=[eiy])", "s", text)
    text = re.sub(r"g(?=[eiy])", "j", text)

    digraphs = (
        ("tion", "X"),
        ("tch", "J"),
        ("dge", "J"),
        ("ch", "J"),
        ("sh", "S"),
        ("th", "T"),
        ("ph", "F"),
        ("ng", "N"),
        ("qu", "K"),
        ("ck", "K"),
        ("wh", "W"),
        ("wr", "R"),
        ("kn", "N"),
    )
    for old, new in digraphs:
        text = text.replace(old, new)

    groups = {
        "a": "V",
        "e": "V",
        "i": "V",
        "o": "V",
        "u": "V",
        "y": "V",
        "b": "P",
        "p": "P",
        "d": "T",
        "t": "T",
        "g": "K",
        "k": "K",
        "q": "K",
        "c": "K",
        "f": "F",
        "v": "F",
        "s": "S",
        "z": "S",
        "x": "S",
        "j": "J",
        "l": "R",
        "r": "R",
        "w": "R",
        "m": "N",
        "n": "N",
        "h": "H",
    }
    key = "".join(groups.get(char, char) for char in text)
    return re.sub(r"(.)\1+", r"\1", key)


def _features(value: str) -> _VoiceFeatures:
    normalized = normalize_voice_text(value)
    if not normalized:
        return _VoiceFeatures("", "", frozenset(), frozenset(), frozenset())

    tokens = normalized.split()
    compact = "".join(tokens)
    canonical_tokens = [_TOKEN_ALIASES.get(token, token) for token in tokens]
    canonical = " ".join(canonical_tokens)
    canonical_compact = "".join(canonical_tokens)

    lexical = {normalized, compact, canonical, canonical_compact}
    pronunciation = {
        _sound_key(compact),
        _sound_key(canonical_compact),
        *_pronunciation_keys(compact),
        *_pronunciation_keys(canonical_compact),
    }
    acoustic = {
        _acoustic_key(compact),
        _acoustic_key(canonical_compact),
    }
    return _VoiceFeatures(
        normalized=normalized,
        compact=compact,
        lexical=frozenset(item for item in lexical if item),
        pronunciation=frozenset(item for item in pronunciation if item),
        acoustic=frozenset(item for item in acoustic if item),
    )


def _best_ratio(left: Iterable[str], right: Iterable[str]) -> float:
    best = 0.0
    for first in left:
        for second in right:
            if first == second:
                return 1.0
            best = max(best, SequenceMatcher(None, first, second).ratio())
    return best


def _voice_evidence(transcript: str, answer: str) -> _VoiceEvidence:
    heard = _features(transcript)
    expected = _features(answer)
    if not heard.compact or not expected.compact:
        return _VoiceEvidence(0.0, 0.0, 0.0, 0.0, False, False, False, "empty")

    if heard.normalized == expected.normalized:
        return _VoiceEvidence(1.0, 1.0, 1.0, 1.0, True, True, True, "exact")
    if heard.compact == expected.compact:
        return _VoiceEvidence(0.995, 1.0, 1.0, 1.0, False, True, True, "compact_exact")

    score = 0.0
    if heard.lexical & expected.lexical:
        score = max(score, 0.97)
    if heard.pronunciation & expected.pronunciation:
        score = max(score, 0.91)
    if heard.acoustic & expected.acoustic:
        score = max(score, 0.88)

    lexical_ratio = _best_ratio(heard.lexical, expected.lexical)
    pronunciation_ratio = _best_ratio(heard.pronunciation, expected.pronunciation)
    score = max(score, 0.92 * lexical_ratio)
    score = max(score, 0.88 * pronunciation_ratio)
    acoustic_ratio = _best_ratio(heard.acoustic, expected.acoustic)
    score = max(score, 0.82 * acoustic_ratio)

    answer_length = len(expected.compact)
    heard_initial = next(iter(heard.acoustic), "")[:1]
    expected_initial = next(iter(expected.acoustic), "")[:1]
    if (
        answer_length <= 5
        and heard_initial
        and heard_initial == expected_initial
        and heard_initial != "V"
    ):
        # Short words are often transcribed as another real word. A matching
        # consonant onset plus a similar sound shape is intentionally lenient.
        score = max(score, 0.58 + 0.24 * acoustic_ratio)

    score = min(score, 1.0)
    if answer_length <= 5:
        qualified = score >= _acceptance_threshold(answer)
        reason = "short_supported" if qualified else "low_score"
    else:
        # Broad acoustic classes are useful for short words, but on longer
        # words they can make unrelated utterances look deceptively similar.
        # Require lexical or pronunciation structure in addition to sound shape.
        structural_score = max(lexical_ratio, pronunciation_ratio)
        qualified = (
            lexical_ratio >= 0.70
            or pronunciation_ratio >= 0.70
            or (acoustic_ratio >= 0.86 and structural_score >= 0.55)
        )
        reason = "long_supported" if qualified else "weak_structure"
    return _VoiceEvidence(
        score,
        lexical_ratio,
        pronunciation_ratio,
        acoustic_ratio,
        False,
        False,
        qualified,
        reason,
    )


def voice_similarity(transcript: str, answer: str) -> float:
    return _voice_evidence(transcript, answer).score


def _acceptance_threshold(answer: str) -> float:
    compact_length = len(normalize_voice_text(answer).replace(" ", ""))
    if compact_length <= 0:
        return 1.0
    if compact_length <= 5:
        return 0.54
    return 0.68


@lru_cache(maxsize=4096)
def voice_answers_confusable(left: str, right: str) -> bool:
    """Return whether two answers should be kept off screen at the same time."""

    left_normalized = normalize_voice_text(left)
    right_normalized = normalize_voice_text(right)
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    if max(
        voice_similarity(left_normalized, right_normalized),
        voice_similarity(right_normalized, left_normalized),
    ) >= 0.80:
        return True

    left_compact = left_normalized.replace(" ", "")
    right_compact = right_normalized.replace(" ", "")
    if not left_compact.isalpha() or not right_compact.isalpha():
        return False

    left_shapes = _confusable_acoustic_shapes(left_compact)
    right_shapes = _confusable_acoustic_shapes(right_compact)
    if _best_ratio(left_shapes, right_shapes) >= 0.84:
        return True

    left_full = _acoustic_key(left_compact)
    right_full = _acoustic_key(right_compact)
    prefix_length = 0
    for left_unit, right_unit in zip(left_full, right_full):
        if left_unit != right_unit:
            break
        prefix_length += 1
    shorter_length = min(len(left_full), len(right_full))
    return (
        prefix_length >= 3
        and shorter_length > 0
        and prefix_length / shorter_length >= 0.60
    )


def _confusable_acoustic_shapes(value: str) -> frozenset[str]:
    full = _acoustic_key(value)
    shapes = {full} if full else set()
    if (
        len(full) >= 5
        and full[0] != "V"
        and full[1] == "V"
        and full[2] != "V"
    ):
        # ASR frequently drops an unstressed initial syllable, as in
        # destroy -> "stroy". Compare both the full and stressed shapes.
        shapes.add(full[2:])
    return frozenset(shapes)


def match_voice_choice(transcript: str, choices: Iterable[VoiceChoice]) -> VoiceMatchResult:
    normalized = normalize_voice_text(transcript)
    choice_list = [choice for choice in choices if normalize_voice_text(choice.text)]
    if not normalized:
        return VoiceMatchResult(None, reason="empty", normalized_transcript="")
    if not choice_list:
        return VoiceMatchResult(None, reason="no_candidates", normalized_transcript=normalized)

    alternatives: list[VoiceAlternative] = []
    for choice in choice_list:
        evidence = _voice_evidence(normalized, choice.text)
        alternatives.append(
            VoiceAlternative(
                choice.key,
                choice.text,
                evidence.score,
                evidence.lexical_score,
                evidence.pronunciation_score,
                evidence.acoustic_score,
                evidence.qualified,
                evidence.reason,
            )
        )
    priority_by_key = {choice.key: choice.priority for choice in choice_list}
    alternatives.sort(
        key=lambda item: (item.score, priority_by_key.get(item.key, 0.0)),
        reverse=True,
    )
    best = alternatives[0]
    if not best.qualified:
        return VoiceMatchResult(
            None,
            score=best.score,
            reason=best.reason or "weak_structure",
            normalized_transcript=normalized,
            alternatives=tuple(alternatives[:5]),
        )
    if best.score < _acceptance_threshold(best.text):
        return VoiceMatchResult(
            None,
            score=best.score,
            reason="low_score",
            normalized_transcript=normalized,
            alternatives=tuple(alternatives[:5]),
        )

    best_text = normalize_voice_text(best.text)
    second_distinct = next(
        (item for item in alternatives[1:] if normalize_voice_text(item.text) != best_text),
        None,
    )
    best_length = len(best_text.replace(" ", ""))
    ambiguity_margin = 0.10 if best_length <= 5 else (0.16 if best.score >= 0.98 else 0.14)
    confusable_pair = bool(
        second_distinct is not None
        and voice_answers_confusable(best.text, second_distinct.text)
    )
    close_scores = bool(
        second_distinct is not None
        and second_distinct.qualified
        and second_distinct.score >= _acceptance_threshold(second_distinct.text)
        and best.score - second_distinct.score < ambiguity_margin
    )
    if confusable_pair or close_scores:
        return VoiceMatchResult(
            None,
            score=best.score,
            ambiguous=True,
            reason="ambiguous",
            normalized_transcript=normalized,
            alternatives=tuple(alternatives[:5]),
        )

    return VoiceMatchResult(
        best.key,
        score=best.score,
        reason="matched",
        normalized_transcript=normalized,
        alternatives=tuple(alternatives[:5]),
    )

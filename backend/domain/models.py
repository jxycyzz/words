from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Word:
    id: int
    word: str
    translation: str
    phonetic: str
    created_on: str
    updated_at: str
    practice_count: int = 0
    correct_count: int = 0
    last_practiced_at: Optional[str] = None
    easiness: float = 2.5
    interval_days: int = 0
    due_on: Optional[str] = None
    repetitions: int = 0
    lapses: int = 0

    @property
    def accuracy_percent(self) -> int:
        if self.practice_count <= 0:
            return 0
        return round((self.correct_count / self.practice_count) * 100)


@dataclass(frozen=True)
class ReviewHistory:
    id: int
    word_id: int
    word: str
    translation: str
    practiced_at: str
    correct: bool
    quality: int
    interval_days: int
    easiness: float
    due_on: str


@dataclass(frozen=True)
class LetterMistake:
    id: int
    word_id: int
    word: str
    position: int
    expected_char: str
    wrong_char: str
    wrong_chars: tuple[str, ...]
    answer_snapshot: str
    practiced_at: str
    source: str


@dataclass(frozen=True)
class PracticeWord:
    id: int
    prompt: str
    answer: str

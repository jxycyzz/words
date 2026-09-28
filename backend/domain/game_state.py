from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .models import PracticeWord
from .voice_matching import VoiceChoice, match_voice_choice, voice_answers_confusable


@dataclass
class FallingWord:
    runtime_id: str
    word_id: int
    prompt: str
    answer: str
    x_ratio: float
    y: float
    progress: int = 0
    hint_positions: set[int] = field(default_factory=set)
    mistake_counts: dict[int, int] = field(default_factory=dict)
    mistake_chars: dict[int, list[str]] = field(default_factory=dict)
    manual_hint_used: bool = False

    @property
    def next_char(self) -> Optional[str]:
        if self.progress >= len(self.answer):
            return None
        return self.answer[self.progress].casefold()

    @property
    def masked_answer(self) -> str:
        parts: list[str] = []
        for index, char in enumerate(self.answer):
            if index < self.progress:
                parts.append(char)
            elif index in self.hint_positions:
                parts.append("[space]" if char == " " else f"[{char}]")
            else:
                parts.append("·")
        return "".join(parts)


@dataclass(frozen=True)
class HitEvent:
    runtime_id: str
    word_id: int
    char: str
    completed: bool
    score_delta: int
    prompt: str
    answer: str
    x_ratio: float
    y: float
    free_hint: bool = False
    manual_hint_used: bool = False


@dataclass(frozen=True)
class MistakeEvent:
    runtime_id: str
    word_id: int
    wrong_char: str
    expected_char: str
    position: int
    prompt: str
    answer: str
    x_ratio: float
    y: float
    requeued: bool
    wrong_chars: tuple[str, ...] = field(default_factory=tuple)
    free_hint: bool = False
    manual_hint_used: bool = False


@dataclass(frozen=True)
class MissedWordEvent:
    runtime_id: str
    word_id: int
    answer: str
    manual_hint_used: bool = False


@dataclass(frozen=True)
class VoiceCandidateSnapshot:
    runtime_id: str
    word_id: int
    answer: str
    priority: float


GameEvent = HitEvent | MistakeEvent


class GameState:
    SPEED_LEVELS = (0.2, 0.5, 0.7)
    ROUND_SPEED_MULTIPLIERS = (0.2, 0.2, 0.2)
    DEFAULT_SPAWN_LANES = (0.18, 0.50, 0.82)

    def __init__(
        self,
        words: Iterable[PracticeWord],
        max_active: int = 5,
        spawn_interval: float = 2.0,
        min_spawn_gap: float = 0.0,
        min_spawn_x_gap: float = 0.0,
        spawn_profiles: dict[float, tuple[float, float, float]] | None = None,
        spawn_lanes: Sequence[float] | None = DEFAULT_SPAWN_LANES,
        avoid_voice_conflicts: bool = False,
        seed: int | None = None,
    ) -> None:
        self.words = [word for word in words if word.answer.strip()]
        if not self.words:
            raise ValueError("at least one practice word is required")
        self.max_active = max_active
        self._base_spawn_interval = max(float(spawn_interval), 0.1)
        self._base_min_spawn_gap = max(float(min_spawn_gap), 0.0)
        self._base_min_spawn_x_gap = max(float(min_spawn_x_gap), 0.0)
        self.spawn_profiles = spawn_profiles or {}
        self.spawn_lanes = tuple(
            min(max(float(lane), 0.08), 0.92)
            for lane in (spawn_lanes or ())
        )
        self.avoid_voice_conflicts = bool(avoid_voice_conflicts)
        self.spawn_interval = self._base_spawn_interval
        self.min_spawn_gap = self._base_min_spawn_gap
        self.min_spawn_x_gap = self._base_min_spawn_x_gap
        self._next_spawn_lane_index = 0
        self.random = random.Random(seed)
        self.base_speed = 44.0
        self.round_speed_multipliers = self.ROUND_SPEED_MULTIPLIERS
        self.initial_speed_multiplier = self.round_speed_multipliers[0]
        self._speed_multiplier = self.initial_speed_multiplier
        self._manual_speed_multiplier: float | None = None
        self.voice_match_message = ""
        self.voice_match_details: dict[str, object] = {}
        self.reset(0.0)

    @property
    def active_limit(self) -> int:
        return min(self.max_active, len(self.words))

    @property
    def speed_multiplier(self) -> float:
        return self._speed_multiplier

    def _round_speed_multiplier(self) -> float:
        index = min(max(self.current_round, 1), len(self.round_speed_multipliers)) - 1
        return self.round_speed_multipliers[index]

    def reset(self, now: float) -> None:
        self.current_round = 1
        self.score = 0
        self.lives = 3
        self.game_active = True
        self.active: list[FallingWord] = []
        self.pending_round: list[PracticeWord] = []
        self._spawn_counter = 0
        self._last_update = now
        self._missed_word_ids: list[int] = []
        self._missed_word_events: list[MissedWordEvent] = []
        self.voice_locked_runtime_id: str | None = None
        self.voice_match_details = {}
        self.round_processed_words = 0
        self.round_total_words = len(self.words)
        self.next_spawn_at = now
        self._start_round(now, first_round=True)

    def retry_current_round(self, now: float) -> None:
        self.lives = 3
        self.game_active = True
        self.active = []
        self.pending_round = []
        self._last_update = now
        self._missed_word_ids.clear()
        self._missed_word_events.clear()
        self.clear_voice_lock()
        self._apply_speed_for_round()
        self.pending_round = self.words[:]
        self.random.shuffle(self.pending_round)
        self.round_processed_words = 0
        self.round_total_words = len(self.pending_round)
        self.next_spawn_at = now

    def set_speed_multiplier(self, value: float) -> None:
        self._manual_speed_multiplier = self._nearest_speed_level(value)
        self._speed_multiplier = self._manual_speed_multiplier
        self._apply_spawn_profile()

    def clear_manual_speed_multiplier(self) -> None:
        self._manual_speed_multiplier = None
        self._apply_speed_for_round()

    def _nearest_speed_level(self, value: float) -> float:
        clean = float(value)
        return min(self.SPEED_LEVELS, key=lambda speed: abs(speed - clean))

    def _apply_speed_for_round(self) -> None:
        self._speed_multiplier = (
            self._manual_speed_multiplier
            if self._manual_speed_multiplier is not None
            else self._round_speed_multiplier()
        )
        self._apply_spawn_profile()

    def _apply_spawn_profile(self) -> None:
        if not self.spawn_profiles:
            self.spawn_interval = self._base_spawn_interval
            self.min_spawn_gap = self._base_min_spawn_gap
            self.min_spawn_x_gap = self._base_min_spawn_x_gap
            return
        profile_speed = min(self.spawn_profiles, key=lambda speed: abs(speed - self._speed_multiplier))
        interval, y_gap, x_gap = self.spawn_profiles[profile_speed]
        self.spawn_interval = max(float(interval), 0.1)
        self.min_spawn_gap = max(float(y_gap), 0.0)
        self.min_spawn_x_gap = max(float(x_gap), 0.0)

    def snapshot(self, now: float) -> dict[str, object]:
        return {
            "current_round": self.current_round,
            "score": self.score,
            "lives": self.lives,
            "game_active": self.game_active,
            "spawn_counter": self._spawn_counter,
            "next_spawn_delay": max(self.next_spawn_at - now, 0.0),
            "voice_locked_runtime_id": self.voice_locked_runtime_id,
            "speed_multiplier": self._speed_multiplier,
            "manual_speed_multiplier": self._manual_speed_multiplier,
            "next_spawn_lane_index": self._next_spawn_lane_index,
            "round_processed_words": self.round_processed_words,
            "round_total_words": self.round_total_words,
            "word_ids": [word.id for word in self.words],
            "pending_word_ids": [word.id for word in self.pending_round],
            "active": [
                {
                    "runtime_id": word.runtime_id,
                    "word_id": word.word_id,
                    "x_ratio": word.x_ratio,
                    "y": word.y,
                    "progress": word.progress,
                    "hint_positions": sorted(word.hint_positions),
                    "mistake_counts": word.mistake_counts,
                    "mistake_chars": word.mistake_chars,
                    "manual_hint_used": word.manual_hint_used,
                }
                for word in self.active
            ],
        }

    def load_snapshot(self, payload: dict[str, object], now: float) -> None:
        words_by_id = {word.id: word for word in self.words}
        self.current_round = max(int(payload.get("current_round", 1) or 1), 1)
        self.score = max(int(payload.get("score", 0) or 0), 0)
        self.lives = max(int(payload.get("lives", 3) or 0), 0)
        self.game_active = bool(payload.get("game_active", True)) and self.lives > 0
        self._spawn_counter = max(int(payload.get("spawn_counter", 0) or 0), 0)
        self.voice_locked_runtime_id = str(payload.get("voice_locked_runtime_id") or "") or None
        manual_speed = payload.get("manual_speed_multiplier")
        self._manual_speed_multiplier = None if manual_speed in (None, "") else self._nearest_speed_level(float(manual_speed))
        self._next_spawn_lane_index = max(int(payload.get("next_spawn_lane_index", 0) or 0), 0)
        self._last_update = now
        self._missed_word_ids.clear()
        self._missed_word_events.clear()
        self.active = []

        active_ids: set[int] = set()
        for item in payload.get("active", []) or []:
            if not isinstance(item, dict):
                continue
            word_id = int(item.get("word_id", 0) or 0)
            source = words_by_id.get(word_id)
            if source is None:
                continue
            answer = source.answer.strip()
            progress = min(max(int(item.get("progress", 0) or 0), 0), len(answer))
            hint_positions = {
                int(index)
                for index in item.get("hint_positions", []) or []
                if 0 <= int(index) < len(answer)
            }
            mistake_counts = {
                int(index): int(count)
                for index, count in (item.get("mistake_counts", {}) or {}).items()
                if str(index).lstrip("-").isdigit()
            }
            mistake_chars = {
                int(index): [str(char) for char in chars]
                for index, chars in (item.get("mistake_chars", {}) or {}).items()
                if str(index).lstrip("-").isdigit() and isinstance(chars, list)
            }
            self.active.append(
                FallingWord(
                    runtime_id=str(item.get("runtime_id") or f"{self.current_round}-{self._spawn_counter}"),
                    word_id=word_id,
                    prompt=source.prompt,
                    answer=answer,
                    x_ratio=min(max(float(item.get("x_ratio", 0.5) or 0.5), 0.08), 0.92),
                    y=max(float(item.get("y", 44.0) or 44.0), 44.0),
                    progress=progress,
                    hint_positions=hint_positions,
                    mistake_counts=mistake_counts,
                    mistake_chars=mistake_chars,
                    manual_hint_used=bool(item.get("manual_hint_used", False)),
                )
            )
            active_ids.add(word_id)

        pending_ids: list[int] = []
        for word_id_value in payload.get("pending_word_ids", []) or []:
            word_id = int(word_id_value)
            if word_id in words_by_id:
                pending_ids.append(word_id)

        known_ids = {
            int(word_id)
            for word_id in payload.get("word_ids", []) or []
            if int(word_id) in words_by_id
        }
        represented_ids = set(pending_ids) | active_ids | known_ids
        for word in self.words:
            if word.id not in represented_ids:
                pending_ids.append(word.id)

        self.pending_round = [words_by_id[word_id] for word_id in pending_ids if word_id in words_by_id]
        self._defer_restored_voice_conflicts()
        remaining_count = len(self.pending_round) + len(self.active)
        fallback_total = max(len(self.words), remaining_count)
        self.round_total_words = max(int(payload.get("round_total_words", fallback_total) or fallback_total), remaining_count)
        self.round_processed_words = min(
            max(int(payload.get("round_processed_words", self.round_total_words - remaining_count) or 0), 0),
            self.round_total_words,
        )
        if self.voice_locked_runtime_id and not any(
            word.runtime_id == self.voice_locked_runtime_id for word in self.active
        ):
            self.clear_voice_lock()
        self._apply_speed_for_round()
        self.next_spawn_at = now + max(float(payload.get("next_spawn_delay", 0.0) or 0.0), 0.0)

    def _defer_restored_voice_conflicts(self) -> None:
        if not self.avoid_voice_conflicts or len(self.active) < 2:
            return

        kept: list[FallingWord] = []
        deferred: list[FallingWord] = []
        for word in sorted(self.active, key=lambda item: item.y, reverse=True):
            conflicts = [
                active
                for active in kept
                if voice_answers_confusable(word.answer, active.answer)
            ]
            if not conflicts:
                kept.append(word)
                continue
            if word.progress == 0 and not word.manual_hint_used:
                deferred.append(word)
                continue

            replaceable = next(
                (
                    active
                    for active in conflicts
                    if active.progress == 0 and not active.manual_hint_used
                ),
                None,
            )
            if replaceable is None:
                kept.append(word)
                continue
            kept.remove(replaceable)
            deferred.append(replaceable)
            kept.append(word)

        kept_runtime_ids = {word.runtime_id for word in kept}
        self.active = [
            word for word in self.active if word.runtime_id in kept_runtime_ids
        ]
        for word in deferred:
            item = PracticeWord(id=word.word_id, prompt=word.prompt, answer=word.answer)
            index = self.random.randint(0, len(self.pending_round))
            self.pending_round.insert(index, item)
            if word.runtime_id == self.voice_locked_runtime_id:
                self.clear_voice_lock()

    def update(self, now: float, baseline_y: float) -> None:
        if not self.game_active:
            return

        dt = min(max(now - self._last_update, 0.0), 0.05)
        self._last_update = now

        fall_speed = self.base_speed * self.speed_multiplier
        survivors: list[FallingWord] = []
        for word in self.active:
            word.y += fall_speed * dt
            if word.y >= baseline_y:
                self.lives -= 1
                self.round_processed_words = min(self.round_processed_words + 1, self.round_total_words)
                self._missed_word_ids.append(word.word_id)
                self._missed_word_events.append(
                    MissedWordEvent(
                        runtime_id=word.runtime_id,
                        word_id=word.word_id,
                        answer=word.answer,
                        manual_hint_used=word.manual_hint_used,
                    )
                )
                if word.runtime_id == self.voice_locked_runtime_id:
                    self.clear_voice_lock()
            else:
                survivors.append(word)
        self.active = survivors

        if self.lives <= 0:
            self.game_active = False
            return

        if (
            self.pending_round
            and len(self.active) < self.active_limit
            and now >= self.next_spawn_at
        ):
            if self._spawn_word():
                self.next_spawn_at = now + self.spawn_interval
            else:
                self.next_spawn_at = now + 0.1

        if not self.pending_round and not self.active:
            self._start_round(now, first_round=False)

    def pop_missed_word_ids(self) -> list[int]:
        missed = self._missed_word_ids[:]
        self._missed_word_ids.clear()
        return missed

    def pop_missed_word_events(self) -> list[MissedWordEvent]:
        missed = self._missed_word_events[:]
        self._missed_word_events.clear()
        self._missed_word_ids.clear()
        return missed

    def handle_text(
        self,
        text: str,
        turret_x_ratio: float = 0.5,
        baseline_y: float = 1.0,
    ) -> list[GameEvent]:
        if not self.game_active:
            return []

        events: list[GameEvent] = []
        for char in text.casefold():
            if char in "\r\n\t":
                continue

            target = self._find_target(char, baseline_y)
            if target is None:
                mistake_target = self._find_mistake_target(baseline_y)
                if mistake_target is not None:
                    mistake = self._register_mistake(mistake_target, char)
                    if mistake:
                        events.append(mistake)
                continue

            target.progress += 1
            target.mistake_counts.pop(target.progress - 1, None)
            target.mistake_chars.pop(target.progress - 1, None)
            completed = target.progress >= len(target.answer)
            score_delta = 10 if completed else 0
            if completed:
                self.score += score_delta
                self.round_processed_words = min(self.round_processed_words + 1, self.round_total_words)
                self.active = [word for word in self.active if word.runtime_id != target.runtime_id]
                if target.runtime_id == self.voice_locked_runtime_id:
                    self.clear_voice_lock()

            events.append(
                HitEvent(
                    runtime_id=target.runtime_id,
                    word_id=target.word_id,
                    char=char,
                    completed=completed,
                    score_delta=score_delta,
                    prompt=target.prompt,
                    answer=target.answer,
                    x_ratio=target.x_ratio,
                    y=target.y,
                    free_hint=target.manual_hint_used,
                    manual_hint_used=target.manual_hint_used,
                )
            )
        return events

    def _register_mistake(self, word: FallingWord, wrong_char: str) -> Optional[MistakeEvent]:
        position = word.progress
        expected = word.next_char
        if expected is None:
            return None
        word.mistake_counts[position] = word.mistake_counts.get(position, 0) + 1
        word.mistake_chars.setdefault(position, []).append(wrong_char)
        if word.mistake_counts[position] < 3 or position in word.hint_positions:
            return None

        word.hint_positions.add(position)
        requeued = self._requeue_word(word)
        return MistakeEvent(
            runtime_id=word.runtime_id,
            word_id=word.word_id,
            wrong_char=wrong_char,
            expected_char=expected,
            position=position,
            prompt=word.prompt,
            answer=word.answer,
            x_ratio=word.x_ratio,
            y=word.y,
            requeued=requeued,
            wrong_chars=tuple(word.mistake_chars.get(position, ())),
            free_hint=word.manual_hint_used,
            manual_hint_used=word.manual_hint_used,
        )

    def _requeue_word(self, word: FallingWord) -> bool:
        if any(item.id == word.word_id and item.answer == word.answer for item in self.pending_round):
            return False
        self._insert_requeued_word(word)
        self.round_total_words += 1
        return True

    def _requeue_word_copies(self, word: FallingWord, count: int) -> int:
        copies = max(int(count), 0)
        for _ in range(copies):
            self._insert_requeued_word(word)
        self.round_total_words += copies
        return copies

    def _insert_requeued_word(self, word: FallingWord) -> None:
        item = PracticeWord(id=word.word_id, prompt=word.prompt, answer=word.answer)
        index = self.random.randint(0, len(self.pending_round))
        self.pending_round.insert(index, item)

    def _start_round(self, now: float, first_round: bool) -> None:
        if not first_round:
            self.current_round += 1
        self.clear_voice_lock()
        self._apply_speed_for_round()
        self.pending_round = self.words[:]
        self.random.shuffle(self.pending_round)
        self.round_processed_words = 0
        self.round_total_words = len(self.pending_round)
        self.next_spawn_at = now if first_round else now + 1.0

    def _spawn_word(self) -> bool:
        if not self.pending_round:
            return False
        pending_index = self._next_voice_distinct_word_index()
        if pending_index is None:
            return False
        x_ratio = self._choose_spawn_x_ratio()
        if x_ratio is None:
            return False
        word = self.pending_round.pop(pending_index)
        self._spawn_counter += 1
        self.active.append(
            FallingWord(
                runtime_id=f"{self.current_round}-{self._spawn_counter}",
                word_id=word.id,
                prompt=word.prompt,
                answer=word.answer.strip(),
                x_ratio=x_ratio,
                y=44.0,
            )
        )
        return True

    def _next_voice_distinct_word_index(self) -> int | None:
        if not self.avoid_voice_conflicts or not self.active:
            return 0
        for index, pending in enumerate(self.pending_round):
            if not any(
                voice_answers_confusable(pending.answer, active.answer)
                for active in self.active
            ):
                return index
        return None

    def _has_spawn_room(self, x_ratio: float | None = None) -> bool:
        if self.min_spawn_gap <= 0 or not self.active:
            return True
        if x_ratio is None or not self.spawn_lanes:
            return min(word.y for word in self.active) >= 44.0 + self.min_spawn_gap
        return all(
            word.y >= 44.0 + self.min_spawn_gap
            or abs(word.x_ratio - x_ratio) > self.min_spawn_x_gap
            for word in self.active
        )

    def _choose_spawn_x_ratio(self) -> Optional[float]:
        if self.spawn_lanes:
            lane_count = len(self.spawn_lanes)
            start = self._next_spawn_lane_index % lane_count
            for offset in range(lane_count):
                index = (start + offset) % lane_count
                candidate = self.spawn_lanes[index]
                if self._has_spawn_room(candidate):
                    self._next_spawn_lane_index = (index + 1) % lane_count
                    return candidate
            return None
        if not self._has_spawn_room():
            return None
        if self.min_spawn_x_gap <= 0 or not self.active:
            return self.random.uniform(0.12, 0.88)
        near_top_limit = 44.0 + max(self.min_spawn_gap, 80.0)
        nearby = [word for word in self.active if word.y <= near_top_limit]
        if not nearby:
            return self.random.uniform(0.12, 0.88)

        candidates = [self.random.uniform(0.12, 0.88) for _ in range(16)]
        candidates.extend([0.12, 0.24, 0.36, 0.50, 0.64, 0.76, 0.88])

        def nearest_distance(candidate: float) -> float:
            return min(abs(candidate - word.x_ratio) for word in nearby)

        best = max(candidates, key=nearest_distance)
        return min(max(best, 0.12), 0.88)

    def _find_target(
        self,
        char: str,
        baseline_y: float,
    ) -> Optional[FallingWord]:
        locked = self.voice_locked_word(baseline_y)
        if locked is not None:
            return locked if locked.next_char == char else None
        candidates = [
            word
            for word in self.active
            if word.next_char == char and word.y < baseline_y
        ]
        if not candidates:
            return None
        hinted = [
            word
            for word in candidates
            if word.progress in word.hint_positions
        ]
        return max(hinted or candidates, key=lambda word: word.y)

    def _find_mistake_target(
        self,
        baseline_y: float,
    ) -> Optional[FallingWord]:
        locked = self.voice_locked_word(baseline_y)
        if locked is not None:
            return locked
        candidates = [
            word
            for word in self.active
            if word.next_char is not None and word.y < baseline_y
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda word: word.y)

    def active_word_by_badge(self, badge_number: int, baseline_y: float | None = None) -> Optional[FallingWord]:
        if badge_number < 1:
            return None
        candidates = [
            word
            for word in self.active
            if word.next_char is not None and (baseline_y is None or word.y < baseline_y)
        ]
        index = badge_number - 1
        if index >= len(candidates):
            return None
        return candidates[index]

    def add_repeats_for_runtime_id(self, runtime_id: str, repeat_count: int = 3) -> Optional[FallingWord]:
        target = next((word for word in self.active if word.runtime_id == runtime_id), None)
        if target is None:
            return None
        target.manual_hint_used = True
        self._requeue_word_copies(target, repeat_count)
        return target

    def clear_voice_lock(self) -> None:
        self.voice_locked_runtime_id = None

    def capture_voice_candidates(self, baseline_y: float) -> tuple[VoiceCandidateSnapshot, ...]:
        return tuple(
            VoiceCandidateSnapshot(
                runtime_id=word.runtime_id,
                word_id=word.word_id,
                answer=word.answer,
                priority=word.y,
            )
            for word in self.active
            if word.y < baseline_y and word.next_char is not None
        )

    def voice_locked_word(self, baseline_y: float | None = None) -> Optional[FallingWord]:
        if not self.voice_locked_runtime_id:
            return None
        for word in self.active:
            if word.runtime_id == self.voice_locked_runtime_id:
                if baseline_y is not None and word.y >= baseline_y:
                    self.clear_voice_lock()
                    return None
                return word
        self.clear_voice_lock()
        return None

    def lock_voice_target(
        self,
        transcript: str,
        baseline_y: float,
        candidate_snapshots: Sequence[VoiceCandidateSnapshot] | None = None,
    ) -> Optional[FallingWord]:
        self.voice_match_message = ""
        candidates = [
            word
            for word in self.active
            if word.y < baseline_y and word.next_char is not None
        ]
        snapshots = (
            self.capture_voice_candidates(baseline_y)
            if candidate_snapshots is None
            else tuple(candidate_snapshots)
        )
        if not snapshots:
            self.clear_voice_lock()
            self.voice_match_message = "当前屏幕没有可锁定的单词"
            self.voice_match_details = {
                "reason": "no_candidates",
                "score": 0.0,
                "transcript": "",
                "selected_runtime_id": None,
                "selected_word_id": None,
                "alternatives": [],
            }
            return None

        snapshot_by_runtime = {item.runtime_id: item for item in snapshots}

        result = match_voice_choice(
            transcript,
            (
                VoiceChoice(
                    key=item.runtime_id,
                    text=item.answer,
                    priority=item.priority,
                )
                for item in snapshots
            ),
        )
        self.voice_match_details = {
            "reason": result.reason,
            "score": round(result.score, 4),
            "transcript": result.normalized_transcript,
            "selected_runtime_id": result.key,
            "selected_word_id": (
                snapshot_by_runtime[result.key].word_id
                if result.key in snapshot_by_runtime
                else None
            ),
            "alternatives": [
                {
                    "runtime_id": item.key,
                    "word_id": (
                        snapshot_by_runtime[item.key].word_id
                        if item.key in snapshot_by_runtime
                        else None
                    ),
                    "score": round(item.score, 4),
                    "lexical_score": round(item.lexical_score, 4),
                    "pronunciation_score": round(item.pronunciation_score, 4),
                    "acoustic_score": round(item.acoustic_score, 4),
                    "qualified": item.qualified,
                    "reason": item.reason,
                }
                for item in result.alternatives
            ],
        }
        if result.reason == "empty":
            self.clear_voice_lock()
            self.voice_match_message = "没有识别到有效读音，请再读一次"
            return None
        if result.ambiguous:
            self.clear_voice_lock()
            self.voice_match_message = "读音接近多个词，请再读一次"
            return None
        if result.key is None:
            self.clear_voice_lock()
            self.voice_match_message = "暂未匹配到屏幕单词，请再读一次"
            return None

        target = next(
            (
                word
                for word in candidates
                if word.runtime_id == result.key
                and result.key in snapshot_by_runtime
                and word.word_id == snapshot_by_runtime[result.key].word_id
                and word.answer.casefold() == snapshot_by_runtime[result.key].answer.casefold()
            ),
            None,
        )
        if target is None:
            self.clear_voice_lock()
            self.voice_match_details["match_reason"] = result.reason
            self.voice_match_details["reason"] = "target_gone"
            self.voice_match_message = "目标已离开屏幕，请再读一次"
            return None

        self.voice_locked_runtime_id = target.runtime_id
        self.voice_match_message = "语音锁定成功，请输入"
        return target

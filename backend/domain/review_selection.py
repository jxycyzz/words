from __future__ import annotations
import random
from datetime import date, timedelta
from math import exp
from typing import Optional, Sequence, Iterable
from .models import Word

def _parse_date(value):
    return date.fromisoformat(value[:10])

def _parse_datetime_date(value):
    return _parse_date(value)

def _clean_date(value):
    return value[:10]

def normalize_word_key(value):
    return " ".join(value.strip().casefold().split())

class ReviewSelector:
    """Desktop selection methods, statically copied with an injected calendar day."""
    def __init__(self, words, target_day):
        self.words = words
        self.target_day = target_day

    def list_words(self):
        return self.words

    def roll_forward_missed_reviews(self, today):
        # The Store performs this in its surrounding transaction.
        pass

    def select(self, existing_ids, limit):
        review_date = self.target_day
        required_date_set = {review_date.isoformat(), (review_date-timedelta(days=1)).isoformat()}
        words = self.words
        words_by_id = {word.id:word for word in words}
        required_ids = {word.id for word in words if word.created_on in required_date_set}
        selected_ids = self._unique_word_ids([i for i in existing_ids if i in words_by_id])
        if not selected_ids:
            selected_ids = [word.id for word in self.list_review_words(limit=max(limit,len(required_ids)),randomize=True,required_created_dates=sorted(required_date_set))]
        for word_id in self._ordered_required_ids(words,required_ids,review_date):
            if word_id not in selected_ids:
                selected_ids.append(word_id)
        if len(required_ids)>limit:
            selected_ids = [i for i in selected_ids if i in required_ids]
            for word_id in self._ordered_required_ids(words,required_ids,review_date):
                if word_id not in selected_ids:
                    selected_ids.append(word_id)
        else:
            selected_ids = self._trim_daily_review_ids(selected_ids,required_ids,limit,review_date,words_by_id)
            selected_ids = self._fill_daily_review_ids(selected_ids,required_date_set,limit,limit,review_date,words_by_id)
        return self._unique_word_ids([i for i in selected_ids if i in words_by_id])

    def retention_probability(self, word: Word, target_date: Optional[date] = None) -> float:
        current_date = target_date or self.target_day
        if word.practice_count <= 0 or not word.last_practiced_at:
            age = max((current_date - _parse_date(word.created_on)).days, 0)
            return max(0.05, exp(-age / 1.5))
        last_date = _parse_datetime_date(word.last_practiced_at)
        days_since = max((current_date - last_date).days, 0)
        stability = max(float(word.interval_days), 1.0)
        return max(0.0, min(1.0, exp(-days_since / stability)))

    def mastery_percent(self, word: Word, target_date: Optional[date] = None) -> int:
        retention = self.retention_probability(word, target_date)
        if word.practice_count <= 0:
            accuracy = 0.0
        else:
            accuracy = word.correct_count / word.practice_count
        return round((retention * 0.65 + accuracy * 0.35) * 100)

    def review_score(self, word: Word, today: Optional[date] = None) -> float:
        current_date = today or self.target_day
        due_date = _parse_date(word.due_on or word.created_on)
        overdue_days = max((current_date - due_date).days, 0)
        if word.practice_count <= 0:
            return 3.0 + min(overdue_days / 3.0, 2.0)
        accuracy = word.correct_count / word.practice_count
        retention = self.retention_probability(word, current_date)
        due_boost = 1.0 if due_date <= current_date else 0.0
        lapse_boost = min(word.lapses * 0.25, 1.0)
        return due_boost + overdue_days * 0.08 + (1.0 - retention) * 1.5 + (1.0 - accuracy) + lapse_boost

    def list_review_words(
        self,
        limit: int = 10,
        randomize: bool = False,
        required_created_dates: Optional[Sequence[str]] = None,
    ) -> list[Word]:
        today = self.target_day
        self.roll_forward_missed_reviews(today)
        words = self.list_words()
        limit = max(int(limit), 1)
        required_dates = {
            _clean_date(value)
            for value in required_created_dates or []
            if value
        }
        required_words = [
            word for word in words if word.created_on in required_dates
        ]
        required_ids = {word.id for word in required_words}
        candidate_words = [
            word for word in words if word.id not in required_ids
        ]
        due_words: list[Word] = []
        future_words: list[Word] = []
        for word in candidate_words:
            if _parse_date(word.due_on or word.created_on) <= today:
                due_words.append(word)
            else:
                future_words.append(word)
        ranked_required = sorted(
            required_words,
            key=lambda word: (
                _parse_date(word.created_on),
                self.review_score(word),
                -self.mastery_percent(word),
                normalize_word_key(word.word),
            ),
            reverse=True,
        )
        ranked_due = sorted(
            due_words,
            key=lambda word: (
                self.review_score(word),
                -self.mastery_percent(word),
                word.created_on,
            ),
            reverse=True,
        )
        ranked_future = sorted(
            future_words,
            key=lambda word: (
                self.mastery_percent(word) * -1,
                self.review_score(word),
                word.created_on,
            ),
            reverse=True,
        )
        ranked = ranked_due + ranked_future
        remaining_limit = max(limit - len(ranked_required), 0)
        if remaining_limit == 0:
            return ranked_required
        if not randomize or len(ranked) <= remaining_limit:
            return ranked_required + ranked[:remaining_limit]
        return ranked_required + self._weighted_review_sample(ranked, remaining_limit, today)

    def _weighted_review_sample(self, ranked: list[Word], limit: int, today: date) -> list[Word]:
        pool_size = min(len(ranked), max(limit * 4, limit))
        pool = ranked[:pool_size]
        selected: list[Word] = []
        while pool and len(selected) < limit:
            pool_length = len(pool)
            weights = [
                max(self.review_score(word, today), 0.1)
                + (pool_length - index) / pool_length * 0.2
                for index, word in enumerate(pool)
            ]
            choice = random.choices(pool, weights=weights, k=1)[0]
            selected.append(choice)
            pool.remove(choice)
        return selected

    def _unique_word_ids(self, word_ids: Iterable[int]) -> list[int]:
        seen: set[int] = set()
        ordered: list[int] = []
        for word_id in word_ids:
            clean_id = int(word_id)
            if clean_id in seen:
                continue
            seen.add(clean_id)
            ordered.append(clean_id)
        return ordered

    def _ordered_required_ids(
        self,
        words: Sequence[Word],
        required_ids: set[int],
        review_date: date,
    ) -> list[int]:
        required_words = [word for word in words if word.id in required_ids]
        required_words.sort(
            key=lambda word: (
                _parse_date(word.created_on),
                self.review_score(word, review_date),
                normalize_word_key(word.word),
            ),
            reverse=True,
        )
        return [word.id for word in required_words]

    def _trim_daily_review_ids(
        self,
        selected_ids: list[int],
        required_ids: set[int],
        max_count: int,
        review_date: date,
        words_by_id: dict[int, Word],
    ) -> list[int]:
        selected_ids = self._unique_word_ids(selected_ids)
        overflow = len(selected_ids) - max_count
        if overflow <= 0:
            return selected_ids
        removable = [
            word_id
            for word_id in selected_ids
            if word_id not in required_ids and word_id in words_by_id
        ]
        removable.sort(
            key=lambda word_id: (
                self.review_score(words_by_id[word_id], review_date),
                -self.mastery_percent(words_by_id[word_id], review_date),
                _parse_date(words_by_id[word_id].created_on),
                normalize_word_key(words_by_id[word_id].word),
            )
        )
        remove_ids = set(removable[:overflow])
        return [word_id for word_id in selected_ids if word_id not in remove_ids]

    def _fill_daily_review_ids(
        self,
        selected_ids: list[int],
        required_date_set: set[str],
        min_count: int,
        max_count: int,
        review_date: date,
        words_by_id: dict[int, Word],
    ) -> list[int]:
        selected_ids = self._unique_word_ids(selected_ids)
        if len(selected_ids) >= min_count or len(selected_ids) >= len(words_by_id):
            return selected_ids

        candidate_words = self.list_review_words(
            limit=max_count,
            randomize=True,
            required_created_dates=sorted(required_date_set),
        )
        for word in candidate_words:
            if len(selected_ids) >= min_count or len(selected_ids) >= max_count:
                break
            if word.id not in selected_ids:
                selected_ids.append(word.id)

        if len(selected_ids) >= min_count or len(selected_ids) >= len(words_by_id):
            return selected_ids

        fallback = [
            word
            for word in words_by_id.values()
            if word.id not in selected_ids
        ]
        fallback.sort(
            key=lambda word: (
                self.review_score(word, review_date),
                -self.mastery_percent(word, review_date),
                word.created_on,
            ),
            reverse=True,
        )
        for word in fallback:
            if len(selected_ids) >= min_count or len(selected_ids) >= max_count:
                break
            selected_ids.append(word.id)
        return selected_ids

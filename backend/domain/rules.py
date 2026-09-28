from __future__ import annotations
from datetime import date, timedelta
from typing import Optional
MIN_EASINESS = 1.3
MAX_EASINESS = 2.8
MAX_REVIEW_INTERVAL_DAYS = 365

def clamp_easiness(value: object) -> float:
    try:
        clean = float(value)
    except (TypeError, ValueError):
        clean = 2.5
    return min(max(clean, MIN_EASINESS), MAX_EASINESS)

def clamp_review_interval_days(value: object, max_days: int = MAX_REVIEW_INTERVAL_DAYS) -> int:
    try:
        clean = int(round(float(value)))
    except (TypeError, ValueError):
        clean = 0
    return min(max(clean, 0), max_days)

def calculate_reward_round(
    correct_chars: int,
    total_chars: int,
    duration_seconds: float,
    max_score: int,
    progress_ratio: float = 1.0,
) -> dict[str, object]:
    correct_chars = max(int(correct_chars), 0)
    total_chars = max(int(total_chars), 0)
    duration_seconds = max(float(duration_seconds), 0.0)
    max_score = max(int(max_score), 0)
    progress_ratio = max(min(float(progress_ratio), 1.0), 0.0)

    accuracy_ratio = 0.0 if total_chars <= 0 else min(correct_chars / total_chars, 1.0)
    cpm = 0.0 if duration_seconds <= 0 else correct_chars / duration_seconds * 60.0
    reward_points = min(max_score, max(0, int(round(max_score * accuracy_ratio * progress_ratio))))
    return {
        "correct_chars": correct_chars,
        "total_chars": total_chars,
        "duration_seconds": round(duration_seconds, 3),
        "accuracy_percent": round(accuracy_ratio * 100, 2),
        "cpm": round(cpm, 2),
        "speed_percent": 0.0,
        "max_score": max_score,
        "reward_points": reward_points,
    }

def sm2_next_state(
    quality: int,
    easiness: float,
    repetitions: int,
    interval_days: int,
    practiced_on: Optional[date] = None,
) -> tuple[float, int, int, str]:
    """Return next easiness, repetitions, interval, and due date using SM-2."""
    quality = min(max(int(quality), 0), 5)
    easiness = clamp_easiness(easiness)
    interval_days = clamp_review_interval_days(interval_days)
    practiced_on = practiced_on or date.today()

    if quality < 3:
        repetitions = 0
        interval_days = 1
    else:
        repetitions += 1
        if repetitions == 1:
            interval_days = 1
        elif repetitions == 2:
            interval_days = 6
        else:
            interval_days = max(round(interval_days * easiness), 1)
    interval_days = max(clamp_review_interval_days(interval_days), 1)

    easiness = easiness + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    easiness = clamp_easiness(easiness)
    due_on = (practiced_on + timedelta(days=max(interval_days, 1))).isoformat()
    return easiness, repetitions, interval_days, due_on

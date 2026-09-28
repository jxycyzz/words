from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


POLICY_VERSION = 1
DEFAULT_WORD_COUNT = 75
DEFAULT_ROUND_COUNT = 2
DEFAULT_PERFECT_REWARD_MONEY = Decimal("4.00")
REWARD_POINT_CEILING = 400
MIN_WORD_COUNT = 10
MAX_WORD_COUNT = 200
MIN_ROUND_COUNT = 1
MAX_ROUND_COUNT = 3
MIN_REWARD_MONEY = Decimal("0.00")
MAX_REWARD_MONEY = Decimal("100.00")
PASSWORD_ITERATIONS = 240_000

PARENT_PASSWORD_KEY = "parent_password_hash"
PARENT_POLICY_KEY = "parent_review_policy"
DAILY_POLICY_PREFIX = "daily_review_policy:"


def _money(value: object) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("全对奖励金额必须是有效数字") from exc
    if amount < MIN_REWARD_MONEY or amount > MAX_REWARD_MONEY:
        raise ValueError("全对奖励金额必须在 0.00 到 100.00 元之间")
    return amount


def reward_round_max_scores(round_count: int) -> tuple[int, ...]:
    rounds = min(max(int(round_count), MIN_ROUND_COUNT), MAX_ROUND_COUNT)
    quotient, remainder = divmod(REWARD_POINT_CEILING, rounds)
    return tuple(
        quotient + (1 if index < remainder else 0)
        for index in range(rounds)
    )


def normalize_review_policy(payload: object | None = None) -> dict[str, object]:
    raw = payload if isinstance(payload, dict) else {}
    try:
        word_count = int(raw.get("word_count", DEFAULT_WORD_COUNT))
        round_count = int(raw.get("round_count", DEFAULT_ROUND_COUNT))
    except (TypeError, ValueError) as exc:
        raise ValueError("单词数和轮数必须是整数") from exc
    if word_count < MIN_WORD_COUNT or word_count > MAX_WORD_COUNT:
        raise ValueError(f"单词数必须在 {MIN_WORD_COUNT} 到 {MAX_WORD_COUNT} 之间")
    if round_count < MIN_ROUND_COUNT or round_count > MAX_ROUND_COUNT:
        raise ValueError(f"轮数必须在 {MIN_ROUND_COUNT} 到 {MAX_ROUND_COUNT} 之间")
    amount = _money(raw.get("perfect_reward_money", DEFAULT_PERFECT_REWARD_MONEY))
    return {
        "version": POLICY_VERSION,
        "word_count": word_count,
        "round_count": round_count,
        "perfect_reward_money": float(amount),
        "reward_point_ceiling": REWARD_POINT_CEILING,
        "round_max_scores": list(reward_round_max_scores(round_count)),
    }


def policy_json(policy: object | None) -> str:
    return json.dumps(
        normalize_review_policy(policy),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_policy_json(value: str | None) -> dict[str, object]:
    if not value:
        return normalize_review_policy()
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("家长复习规则配置已损坏") from exc
    return normalize_review_policy(payload)


def hash_parent_password(password: str) -> str:
    clean = str(password)
    if len(clean) < 6:
        raise ValueError("家长密码至少需要 6 个字符")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        clean.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_parent_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = str(encoded or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.b64decode(salt_text, validate=True)
        expected = base64.b64decode(digest_text, validate=True)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


@dataclass
class ParentSettingsManager:
    repository: object

    def has_password(self) -> bool:
        return bool(self.repository.get_setting(PARENT_PASSWORD_KEY))

    def initialize_password(self, password: str, confirmation: str) -> None:
        if self.has_password():
            raise ValueError("家长密码已经设置")
        if password != confirmation:
            raise ValueError("两次输入的家长密码不一致")
        self.repository.set_setting(PARENT_PASSWORD_KEY, hash_parent_password(password))
        self.repository.log_operation(
            "parent_password_initialized",
            "parent password initialized",
            subject_type="settings",
            subject_id="parent_review_policy",
        )

    def verify_password(self, password: str) -> bool:
        return verify_parent_password(
            password,
            self.repository.get_setting(PARENT_PASSWORD_KEY),
        )

    def current_policy(self) -> dict[str, object]:
        return parse_policy_json(self.repository.get_setting(PARENT_POLICY_KEY))

    def save_policy(self, password: str, policy: object) -> dict[str, object]:
        if not self.has_password():
            raise ValueError("请先设置家长密码")
        if not self.verify_password(password):
            raise ValueError("家长密码错误")
        normalized = normalize_review_policy(policy)
        self.repository.set_setting(PARENT_POLICY_KEY, policy_json(normalized))
        self.repository.log_operation(
            "parent_review_policy_changed",
            "parent review policy changed",
            subject_type="settings",
            subject_id="parent_review_policy",
            details=normalized,
        )
        clear_future = getattr(
            self.repository,
            "clear_future_daily_review_preparation",
            None,
        )
        if callable(clear_future):
            clear_future(date.today().isoformat())
        return normalized

    def daily_policy(self, day: str) -> dict[str, object] | None:
        value = self.repository.get_setting(f"{DAILY_POLICY_PREFIX}{day}")
        return parse_policy_json(value) if value else None

    def get_or_create_daily_policy(self, day: str) -> dict[str, object]:
        existing = self.daily_policy(day)
        if existing is not None:
            return existing
        policy = self.current_policy()
        self.repository.set_setting(f"{DAILY_POLICY_PREFIX}{day}", policy_json(policy))
        self.repository.log_operation(
            "daily_review_policy_frozen",
            f"daily review policy frozen: {day}",
            subject_type="daily_review",
            subject_id=day,
            details=policy,
        )
        return policy

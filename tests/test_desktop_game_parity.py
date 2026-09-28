from __future__ import annotations

import unittest

from backend.domain.game_state import GameState, MistakeEvent, VoiceCandidateSnapshot
from backend.domain.models import PracticeWord


def practice_words(count: int) -> list[PracticeWord]:
    return [PracticeWord(id=index, prompt=f"meaning {index}", answer=f"word{index}") for index in range(count)]


class GameStateTests(unittest.TestCase):
    def test_active_limit_respects_selected_word_count(self) -> None:
        state = GameState(practice_words(3), seed=1)
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        state.update(4.2, baseline_y=500)
        state.update(6.3, baseline_y=500)
        self.assertLessEqual(len(state.active), 3)
        self.assertEqual(state.active_limit, 3)

    def test_active_limit_caps_at_five_for_large_sets(self) -> None:
        state = GameState(practice_words(8), seed=1)
        for step in range(20):
            state.update(step * 2.1, baseline_y=500)
        self.assertLessEqual(len(state.active), 5)
        self.assertEqual(state.active_limit, 5)

    def test_spawn_gap_prevents_top_overlap(self) -> None:
        state = GameState(
            practice_words(3),
            spawn_interval=2.0,
            min_spawn_gap=90.0,
            spawn_lanes=(),
            seed=1,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)

        self.assertEqual(len(state.active), 1)

        state.active[0].y = 134.0
        state.update(2.2, baseline_y=500)

        self.assertEqual(len(state.active), 2)

    def test_speed_profiles_adjust_spawn_timing_and_safety_gaps(self) -> None:
        state = GameState(
            practice_words(3),
            spawn_profiles={
                0.2: (3.0, 70.0, 0.14),
                0.5: (2.0, 70.0, 0.14),
                0.7: (1.0, 70.0, 0.14),
            },
            seed=1,
        )

        self.assertAlmostEqual(state.spawn_interval, 3.0)
        self.assertAlmostEqual(state.min_spawn_gap, 70.0)
        self.assertAlmostEqual(state.min_spawn_x_gap, 0.14)

        state.set_speed_multiplier(0.5)
        self.assertAlmostEqual(state.spawn_interval, 2.0)
        self.assertAlmostEqual(state.min_spawn_gap, 70.0)
        self.assertAlmostEqual(state.min_spawn_x_gap, 0.14)

        state.set_speed_multiplier(0.7)
        self.assertAlmostEqual(state.spawn_interval, 1.0)
        self.assertAlmostEqual(state.min_spawn_gap, 70.0)
        self.assertAlmostEqual(state.min_spawn_x_gap, 0.14)

    def test_spawn_lanes_cycle_left_middle_right(self) -> None:
        state = GameState(practice_words(3), spawn_interval=3.0, min_spawn_gap=70.0, min_spawn_x_gap=0.14, seed=2)
        state.update(0.0, baseline_y=500)
        state.update(3.1, baseline_y=500)
        state.update(6.2, baseline_y=500)

        self.assertEqual([round(word.x_ratio, 2) for word in state.active], [0.18, 0.5, 0.82])

    def test_spawn_chooses_next_lane_when_current_lane_is_too_close(self) -> None:
        state = GameState(
            practice_words(3),
            spawn_interval=1.0,
            min_spawn_gap=40.0,
            min_spawn_x_gap=0.28,
            seed=2,
        )
        state.update(0.0, baseline_y=500)
        state.active[0].x_ratio = 0.5
        state.active[0].y = 80.0
        state.update(1.1, baseline_y=500)

        self.assertEqual(len(state.active), 2)
        self.assertGreaterEqual(abs(state.active[1].x_ratio - 0.5), 0.28)

    def test_next_round_waits_until_current_round_is_finished(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="abc"),
                PracticeWord(id=2, prompt="two", answer="xyz"),
            ],
            seed=2,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        self.assertEqual(state.current_round, 1)
        self.assertEqual(len(state.pending_round), 0)
        self.assertEqual(len(state.active), 2)

        for word in list(state.active):
            for char in word.answer:
                state.handle_text(char, baseline_y=500)
        state.update(4.2, baseline_y=500)
        self.assertEqual(state.current_round, 2)

    def test_correct_and_wrong_input(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="apple", answer="apple")], seed=3)
        state.update(0.0, baseline_y=500)
        self.assertEqual(state.active[0].masked_answer, "·····")
        self.assertEqual(state.handle_text("z", baseline_y=500), [])

        events = []
        for char in "apple":
            events.extend(state.handle_text(char, baseline_y=500))

        self.assertTrue(events[-1].completed)
        self.assertEqual(state.score, 10)
        self.assertEqual(len(state.active), 0)

    def test_phrase_with_space_can_be_completed(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="again", answer="over again")], seed=4)
        state.update(0.0, baseline_y=500)

        events = []
        for char in "over again":
            events.extend(state.handle_text(char, baseline_y=500))

        self.assertTrue(events[-1].completed)
        self.assertEqual(state.score, 10)
        self.assertEqual(len(state.active), 0)

    def test_three_wrong_letters_show_hint_and_requeue_word(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="apple", answer="apple")], seed=3)
        state.update(0.0, baseline_y=500)

        self.assertEqual(state.handle_text("z", baseline_y=500), [])
        self.assertEqual(state.handle_text("x", baseline_y=500), [])
        events = state.handle_text("q", baseline_y=500)

        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], MistakeEvent)
        mistake = events[0]
        self.assertEqual(mistake.word_id, 1)
        self.assertEqual(mistake.position, 0)
        self.assertEqual(mistake.expected_char, "a")
        self.assertEqual(mistake.wrong_chars, ("z", "x", "q"))
        self.assertTrue(mistake.requeued)
        self.assertIn("[a]", state.active[0].masked_answer)
        self.assertEqual([word.id for word in state.pending_round], [1])

    def test_wrong_letters_after_partial_input_hint_current_position(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="apple", answer="apple")], seed=3)
        state.update(0.0, baseline_y=500)
        state.handle_text("a", baseline_y=500)

        state.handle_text("z", baseline_y=500)
        state.handle_text("x", baseline_y=500)
        events = state.handle_text("q", baseline_y=500)

        self.assertIsInstance(events[0], MistakeEvent)
        self.assertEqual(events[0].position, 1)
        self.assertEqual(events[0].expected_char, "p")
        self.assertIn("a[p]", state.active[0].masked_answer)

    def test_followup_input_can_hit_another_lower_word_without_locking(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ab"),
                PracticeWord(id=2, prompt="two", answer="ac"),
            ],
            seed=5,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        state.active[0].y = 450
        state.active[1].y = 300
        events = state.handle_text("a", baseline_y=500)
        first_id = events[0].word_id

        remaining = state.active[0] if state.active[0].word_id != first_id else state.active[1]
        remaining.y = 470
        events = state.handle_text("a", baseline_y=500)

        self.assertEqual(events[0].word_id, remaining.word_id)

    def test_unlocked_target_prefers_largest_y_word(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ax"),
                PracticeWord(id=2, prompt="two", answer="ay"),
            ],
            seed=6,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        state.active[0].y = 120
        state.active[1].y = 455

        events = state.handle_text("a", baseline_y=500)

        self.assertEqual(events[0].word_id, state.active[1].word_id)

    def test_hint_letter_candidate_wins_for_current_key(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ax"),
                PracticeWord(id=2, prompt="two", answer="ay"),
            ],
            seed=7,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        unhinted = state.active[0]
        hinted = state.active[1]
        unhinted.y = 455
        hinted.y = 250
        hinted.hint_positions.add(0)

        events = state.handle_text("a", baseline_y=500)

        self.assertEqual(events[0].word_id, hinted.word_id)

    def test_multiple_hint_candidates_use_largest_y(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ax"),
                PracticeWord(id=2, prompt="two", answer="ay"),
            ],
            seed=8,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        higher_hint = state.active[0]
        lower_hint = state.active[1]
        higher_hint.y = 410
        lower_hint.y = 465
        higher_hint.hint_positions.add(0)
        lower_hint.hint_positions.add(0)

        events = state.handle_text("a", baseline_y=500)

        self.assertEqual(events[0].word_id, lower_hint.word_id)

    def test_badge_target_selects_visible_word_by_number(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ax"),
                PracticeWord(id=2, prompt="two", answer="by"),
                PracticeWord(id=3, prompt="three", answer="cz"),
            ],
            seed=13,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        state.update(4.2, baseline_y=500)

        first = state.active_word_by_badge(1, baseline_y=500)
        second = state.active_word_by_badge(2, baseline_y=500)
        missing = state.active_word_by_badge(4, baseline_y=500)

        self.assertEqual(first, state.active[0])
        self.assertEqual(second, state.active[1])
        self.assertIsNone(missing)

    def test_badge_hint_requeues_three_copies_without_changing_mask_or_score(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=14)
        state.update(0.0, baseline_y=500)
        target = state.active_word_by_badge(1, baseline_y=500)
        assert target is not None
        original_mask = target.masked_answer

        first_events = state.handle_text("a", baseline_y=500)
        second_events = state.handle_text("b", baseline_y=500)

        self.assertFalse(first_events[0].free_hint)
        self.assertTrue(second_events[0].completed)
        self.assertEqual(second_events[0].score_delta, 10)
        self.assertEqual(state.score, 10)

        state = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=14)
        state.update(0.0, baseline_y=500)
        target = state.active_word_by_badge(1, baseline_y=500)
        assert target is not None
        state.add_repeats_for_runtime_id(target.runtime_id, repeat_count=3)

        self.assertEqual(target.masked_answer, original_mask)
        self.assertEqual([word.id for word in state.pending_round].count(target.word_id), 3)
        self.assertEqual(state.round_total_words, 4)
        self.assertEqual(state.round_processed_words, 0)

    def test_badge_hint_requeue_copies_are_mixed_into_pending_words(self) -> None:
        state = GameState(
            [PracticeWord(id=index, prompt=str(index), answer=str(index)) for index in range(1, 6)],
            seed=2,
        )
        state.update(0.0, baseline_y=500)
        target = state.active_word_by_badge(1, baseline_y=500)
        assert target is not None

        state.add_repeats_for_runtime_id(target.runtime_id, repeat_count=3)
        pending_ids = [word.id for word in state.pending_round]

        self.assertEqual(pending_ids.count(target.word_id), 3)
        self.assertNotEqual(pending_ids[-3:], [target.word_id, target.word_id, target.word_id])
        self.assertEqual(state.round_total_words, 8)

    def test_snapshot_restore_preserves_badge_hint_requeue_copies(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=15)
        state.update(0.0, baseline_y=500)
        target = state.active_word_by_badge(1, baseline_y=500)
        assert target is not None
        state.add_repeats_for_runtime_id(target.runtime_id, repeat_count=3)
        snapshot = state.snapshot(0.0)

        restored = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=15)
        restored.load_snapshot(snapshot, 10.0)

        self.assertEqual(len(restored.pending_round), 3)
        self.assertEqual(restored.round_total_words, 4)

    def test_round_progress_counts_completed_and_missed_words(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="a"),
                PracticeWord(id=2, prompt="two", answer="b"),
                PracticeWord(id=3, prompt="three", answer="c"),
            ],
            seed=16,
        )
        state.update(0.0, baseline_y=500)
        self.assertEqual((state.round_processed_words, state.round_total_words), (0, 3))

        state.handle_text(state.active[0].answer, baseline_y=500)
        self.assertEqual((state.round_processed_words, state.round_total_words), (1, 3))

        state.update(2.1, baseline_y=500)
        state.update(4.2, baseline_y=500)
        state.active[0].y = 500
        state.update(4.3, baseline_y=500)
        self.assertEqual((state.round_processed_words, state.round_total_words), (2, 3))

    def test_wrong_input_is_recorded_on_largest_y_word(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ax"),
                PracticeWord(id=2, prompt="two", answer="by"),
            ],
            seed=9,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        state.active[0].y = 180
        state.active[1].y = 460

        state.handle_text("z", baseline_y=500)
        state.handle_text("z", baseline_y=500)
        events = state.handle_text("z", baseline_y=500)

        self.assertIsInstance(events[0], MistakeEvent)
        self.assertEqual(events[0].word_id, state.active[1].word_id)

    def test_voice_lock_prefers_largest_y_exact_match(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="apple"),
                PracticeWord(id=2, prompt="two", answer="apple"),
            ],
            seed=10,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        state.active[0].y = 200
        state.active[1].y = 460

        target = state.lock_voice_target("Apple.", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.word_id, state.active[1].word_id)
        self.assertEqual(state.voice_locked_runtime_id, target.runtime_id)
        self.assertNotIn("apple", state.voice_match_message.casefold())

    def test_voice_lock_accepts_minor_asr_spelling_drift(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="one", answer="apple")], seed=10)
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("aple", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.answer, "apple")

    def test_voice_lock_accepts_deliver_asr_variants(self) -> None:
        for transcript in ("liver", "delive"):
            with self.subTest(transcript=transcript):
                state = GameState(
                    [PracticeWord(id=1, prompt="交付", answer="deliver")],
                    seed=10,
                )
                state.update(0.0, baseline_y=500)

                target = state.lock_voice_target(transcript, baseline_y=500)

                self.assertIsNotNone(target)
                self.assertEqual(target.answer, "deliver")

    def test_voice_lock_rejects_long_match_based_only_on_broad_acoustics(self) -> None:
        state = GameState(
            [PracticeWord(id=1, prompt="损害", answer="damage")],
            seed=10,
        )
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("thank you", baseline_y=500)

        self.assertIsNone(target)
        self.assertEqual(state.voice_match_details["reason"], "weak_structure")

    def test_voice_lock_uses_candidates_present_when_recording_started(self) -> None:
        deliver = PracticeWord(id=1, prompt="交付", answer="deliver")
        delivery = PracticeWord(id=2, prompt="递送", answer="delivery")
        state = GameState([deliver, delivery], seed=10)
        state.active = []
        state.pending_round = [deliver, delivery]
        state.next_spawn_at = 0.0
        state.update(0.0, baseline_y=500)
        recorded_candidate = state.active[0]
        self.assertEqual(recorded_candidate.answer, "deliver")
        recorded_snapshots = state.capture_voice_candidates(500)
        state.update(2.1, baseline_y=500)
        self.assertEqual(len(state.active), 2)

        target = state.lock_voice_target(
            "delivery",
            baseline_y=500,
            candidate_snapshots=recorded_snapshots,
        )

        self.assertIsNotNone(target)
        self.assertEqual(target.runtime_id, recorded_candidate.runtime_id)
        self.assertEqual(target.answer, recorded_candidate.answer)

    def test_voice_lock_rejects_reused_runtime_id_for_different_word(self) -> None:
        state = GameState(
            [PracticeWord(id=1, prompt="交付", answer="deliver")],
            seed=10,
        )
        state.update(0.0, baseline_y=500)
        runtime_id = state.active[0].runtime_id

        target = state.lock_voice_target(
            "deliver",
            baseline_y=500,
            candidate_snapshots=(
                VoiceCandidateSnapshot(runtime_id, 999, "deliver", state.active[0].y),
            ),
        )

        self.assertIsNone(target)
        self.assertEqual(state.voice_match_details["reason"], "target_gone")

    def test_voice_lock_does_not_rerank_when_recorded_target_has_dropped(self) -> None:
        deliver = PracticeWord(id=1, prompt="交付", answer="deliver")
        damage = PracticeWord(id=2, prompt="损害", answer="damage")
        state = GameState([deliver, damage], seed=10)
        state.active = []
        state.pending_round = [deliver, damage]
        state.next_spawn_at = 0.0
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        snapshots = state.capture_voice_candidates(500)
        state.active = [word for word in state.active if word.answer == "damage"]

        target = state.lock_voice_target(
            "deliver",
            baseline_y=500,
            candidate_snapshots=snapshots,
        )

        self.assertIsNone(target)
        self.assertEqual(state.voice_match_details["reason"], "target_gone")
        self.assertIsNone(state.voice_locked_runtime_id)

    def test_voice_lock_rejects_close_deliver_delivery_candidates(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="交付", answer="deliver"),
                PracticeWord(id=2, prompt="递送", answer="delivery"),
            ],
            seed=10,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)

        target = state.lock_voice_target("delivery", baseline_y=500)

        self.assertIsNone(target)
        self.assertTrue(state.voice_match_details["reason"] == "ambiguous")

    def test_voice_aware_spawn_defers_confusable_active_word(self) -> None:
        deliver = PracticeWord(id=1, prompt="交付", answer="deliver")
        delivery = PracticeWord(id=2, prompt="递送", answer="delivery")
        apple = PracticeWord(id=3, prompt="苹果", answer="apple")
        state = GameState(
            [deliver, delivery, apple],
            spawn_interval=1.0,
            avoid_voice_conflicts=True,
            seed=10,
        )
        state.active = []
        state.pending_round = [deliver, delivery, apple]
        state.next_spawn_at = 0.0

        state.update(0.0, baseline_y=500)
        state.update(1.1, baseline_y=500)

        self.assertEqual([word.answer for word in state.active], ["deliver", "apple"])
        self.assertEqual([word.answer for word in state.pending_round], ["delivery"])

    def test_voice_aware_spawn_defers_shared_syllable_pairs(self) -> None:
        for first, second in (("destroy", "style"), ("circle", "surf")):
            with self.subTest(first=first, second=second):
                first_word = PracticeWord(id=1, prompt="first", answer=first)
                second_word = PracticeWord(id=2, prompt="second", answer=second)
                apple = PracticeWord(id=3, prompt="苹果", answer="apple")
                state = GameState(
                    [first_word, second_word, apple],
                    spawn_interval=1.0,
                    avoid_voice_conflicts=True,
                    seed=10,
                )
                state.active = []
                state.pending_round = [first_word, second_word, apple]
                state.next_spawn_at = 0.0

                state.update(0.0, baseline_y=500)
                state.update(1.1, baseline_y=500)

                self.assertEqual(
                    [word.answer for word in state.active],
                    [first, "apple"],
                )
                self.assertEqual(
                    [word.answer for word in state.pending_round],
                    [second],
                )

    def test_snapshot_restore_defers_confusable_active_pair(self) -> None:
        destroy = PracticeWord(id=1, prompt="破坏", answer="destroy")
        style = PracticeWord(id=2, prompt="风格", answer="style")
        source = GameState([destroy, style], seed=10)
        source.active = []
        source.pending_round = [destroy, style]
        source.next_spawn_at = 0.0
        source.update(0.0, baseline_y=500)
        source.update(2.1, baseline_y=500)
        payload = source.snapshot(2.1)

        restored = GameState(
            [destroy, style],
            avoid_voice_conflicts=True,
            seed=10,
        )
        restored.load_snapshot(payload, now=3.0)

        self.assertEqual(len(restored.active), 1)
        self.assertEqual(len(restored.pending_round), 1)
        self.assertEqual(
            {restored.active[0].answer, restored.pending_round[0].answer},
            {"destroy", "style"},
        )

    def test_shared_syllable_pair_is_ambiguous_if_already_visible(self) -> None:
        for first, second in (("destroy", "style"), ("circle", "surf")):
            with self.subTest(first=first, second=second):
                state = GameState(
                    [
                        PracticeWord(id=1, prompt="first", answer=first),
                        PracticeWord(id=2, prompt="second", answer=second),
                    ],
                    seed=10,
                )
                state.update(0.0, baseline_y=500)
                state.update(2.1, baseline_y=500)

                target = state.lock_voice_target(first, baseline_y=500)

                self.assertIsNone(target)
                self.assertEqual(state.voice_match_details["reason"], "ambiguous")

    def test_voice_lock_accepts_phrase_spacing_and_minor_drift(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="again", answer="over again")], seed=10)
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("over agen", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.answer, "over again")

    def test_voice_lock_accepts_general_asr_word_splitting(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="toward", answer="onto")], seed=10)
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("on two", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.answer, "onto")

    def test_voice_lock_accepts_pronunciation_near_miss_for_short_words(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="eager", answer="keen")], seed=10)
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("king", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.answer, "keen")

    def test_voice_lock_accepts_dull_when_asr_returns_door(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="not bright", answer="dull")], seed=10)
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("door", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.answer, "dull")

    def test_voice_lock_accepts_edge_when_asr_returns_each(self) -> None:
        state = GameState(
            [PracticeWord(id=1, prompt="边缘", answer="edge")],
            seed=10,
        )
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("each", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.answer, "edge")

    def test_voice_lock_accepts_clear_short_word_sound_shape(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="not bright", answer="dull"),
                PracticeWord(id=2, prompt="quick", answer="fast"),
            ],
            seed=10,
        )
        state.update(0.0, baseline_y=500)
        state.update(3.1, baseline_y=500)

        target = state.lock_voice_target("down", baseline_y=500)

        self.assertIsNotNone(target)
        self.assertEqual(target.answer, "dull")

    def test_voice_lock_rejects_pronunciation_near_miss_when_visible_word_is_ambiguous(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="eager", answer="keen"),
                PracticeWord(id=2, prompt="ruler", answer="king"),
            ],
            seed=10,
        )
        state.update(0.0, baseline_y=500)
        state.update(3.1, baseline_y=500)

        target = state.lock_voice_target("king", baseline_y=500)

        self.assertIsNone(target)
        self.assertIsNone(state.voice_locked_runtime_id)
        self.assertIn("多个词", state.voice_match_message)

    def test_voice_lock_keeps_short_words_strict(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="inside", answer="in")], seed=10)
        state.update(0.0, baseline_y=500)

        target = state.lock_voice_target("it", baseline_y=500)

        self.assertIsNone(target)
        self.assertIsNone(state.voice_locked_runtime_id)

    def test_voice_lock_rejects_ambiguous_visible_candidates(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="apple"),
                PracticeWord(id=2, prompt="two", answer="apply"),
            ],
            seed=10,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)

        target = state.lock_voice_target("appl", baseline_y=500)

        self.assertIsNone(target)
        self.assertIsNone(state.voice_locked_runtime_id)
        self.assertIn("多个词", state.voice_match_message)

    def test_voice_lock_routes_correct_and_wrong_input_to_locked_word(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ax"),
                PracticeWord(id=2, prompt="two", answer="ay"),
            ],
            seed=11,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        locked = state.active[0]
        other = state.active[1]
        locked.y = 120
        other.y = 470
        state.lock_voice_target(locked.answer, baseline_y=500)

        events = state.handle_text("a", baseline_y=500)
        self.assertEqual(events[0].word_id, locked.word_id)

        state.handle_text("z", baseline_y=500)
        state.handle_text("z", baseline_y=500)
        events = state.handle_text("z", baseline_y=500)
        self.assertIsInstance(events[0], MistakeEvent)
        self.assertEqual(events[0].word_id, locked.word_id)

    def test_voice_lock_clears_when_locked_word_completes_or_drops(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="one", answer="a")], seed=12)
        state.update(0.0, baseline_y=500)
        state.lock_voice_target("a", baseline_y=500)
        state.handle_text("a", baseline_y=500)

        self.assertIsNone(state.voice_locked_runtime_id)

        state = GameState([PracticeWord(id=1, prompt="one", answer="a")], seed=12)
        state.update(0.0, baseline_y=500)
        state.lock_voice_target("a", baseline_y=500)
        state.active[0].y = 500
        state.update(0.1, baseline_y=500)

        self.assertIsNone(state.voice_locked_runtime_id)

    def test_round_speed_stays_low_for_every_round(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="apple", answer="a")], seed=3)
        self.assertAlmostEqual(state.speed_multiplier, 0.2)

        state.update(0.0, baseline_y=500)
        state.handle_text("a", baseline_y=500)
        state.update(0.1, baseline_y=500)
        self.assertEqual(state.current_round, 2)
        self.assertAlmostEqual(state.speed_multiplier, 0.2)

        state.update(1.2, baseline_y=500)
        state.handle_text("a", baseline_y=500)
        state.update(1.3, baseline_y=500)
        self.assertEqual(state.current_round, 3)
        self.assertAlmostEqual(state.speed_multiplier, 0.2)

    def test_speed_adjustment_snaps_to_three_levels_and_holds(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="apple", answer="a")], seed=3)
        state.set_speed_multiplier(1.4)
        self.assertAlmostEqual(state.speed_multiplier, 0.7)

        state.update(0.0, baseline_y=500)
        state.handle_text("a", baseline_y=500)
        state.update(0.1, baseline_y=500)
        self.assertAlmostEqual(state.speed_multiplier, 0.7)

    def test_clearing_manual_speed_restores_round_bound_speed(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="apple", answer="a")], seed=3)
        state.set_speed_multiplier(0.7)
        state.clear_manual_speed_multiplier()

        self.assertAlmostEqual(state.speed_multiplier, 0.2)

    def test_retry_current_round_after_failure_keeps_round_number(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="apple", answer="a")], seed=3)
        state.update(0.0, baseline_y=500)
        state.handle_text("a", baseline_y=500)
        state.update(0.1, baseline_y=500)
        self.assertEqual(state.current_round, 2)
        self.assertAlmostEqual(state.speed_multiplier, 0.2)

        state.update(1.2, baseline_y=500)
        state.lives = 1
        state.active[0].y = 500
        state.update(1.3, baseline_y=500)
        self.assertFalse(state.game_active)

        state.retry_current_round(2.0)

        self.assertTrue(state.game_active)
        self.assertEqual(state.current_round, 2)
        self.assertEqual(state.lives, 3)
        self.assertAlmostEqual(state.speed_multiplier, 0.2)
        self.assertEqual(len(state.active), 0)
        self.assertEqual(len(state.pending_round), 1)

    def test_snapshot_restore_keeps_round_progress_and_adds_new_words(self) -> None:
        state = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ab"),
                PracticeWord(id=2, prompt="two", answer="cd"),
            ],
            seed=5,
        )
        state.update(0.0, baseline_y=500)
        state.update(2.1, baseline_y=500)
        active = state.active[0]
        state.handle_text(active.answer[0], baseline_y=500)
        snapshot = state.snapshot(2.1)

        restored = GameState(
            [
                PracticeWord(id=1, prompt="one", answer="ab"),
                PracticeWord(id=2, prompt="two", answer="cd"),
                PracticeWord(id=3, prompt="three", answer="ef"),
            ],
            seed=5,
        )
        restored.load_snapshot(snapshot, 10.0)

        self.assertEqual(restored.current_round, state.current_round)
        self.assertEqual(restored.active[0].progress, 1)
        self.assertIn(3, [word.id for word in restored.pending_round])

    def test_snapshot_restore_preserves_manual_speed_and_round_progress(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=5)
        state.set_speed_multiplier(0.7)
        state.update(0.0, baseline_y=500)
        state.add_repeats_for_runtime_id(state.active[0].runtime_id, repeat_count=3)
        snapshot = state.snapshot(0.0)

        restored = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=5)
        restored.load_snapshot(snapshot, 10.0)

        self.assertAlmostEqual(restored.speed_multiplier, 0.7)
        self.assertEqual(restored.round_total_words, 4)

    def test_manual_hint_marks_current_word_free_and_persists_in_snapshot(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=5)
        state.update(0.0, baseline_y=500)
        runtime_id = state.active[0].runtime_id

        target = state.add_repeats_for_runtime_id(runtime_id, repeat_count=3)
        events = state.handle_text("a", baseline_y=500)

        self.assertIs(target, state.active[0])
        self.assertTrue(events[0].free_hint)
        self.assertTrue(events[0].manual_hint_used)
        self.assertEqual(state.round_total_words, 4)

        snapshot = state.snapshot(0.0)
        restored = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=5)
        restored.load_snapshot(snapshot, 10.0)

        self.assertTrue(restored.active[0].manual_hint_used)
        restored_events = restored.handle_text("b", baseline_y=500)
        self.assertTrue(restored_events[0].free_hint)
        self.assertTrue(restored_events[0].manual_hint_used)

    def test_manual_hint_mistake_event_is_free_hint(self) -> None:
        state = GameState([PracticeWord(id=1, prompt="one", answer="ab")], seed=5)
        state.update(0.0, baseline_y=500)
        state.add_repeats_for_runtime_id(state.active[0].runtime_id, repeat_count=3)

        events = []
        for _ in range(3):
            events = state.handle_text("x", baseline_y=500)

        self.assertTrue(events)
        self.assertTrue(events[0].free_hint)
        self.assertTrue(events[0].manual_hint_used)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone

from pet.moon_phase import (
    NEW_MOON_EPOCH,
    PHASE_EMOJIS,
    PHASE_NAMES,
    SYNODIC_MONTH_DAYS,
    MoonPhase,
    calculate_moon_phase,
)


class MoonPhaseTests(unittest.TestCase):
    def test_returns_documented_data_shape_at_reference_new_moon(self) -> None:
        phase = calculate_moon_phase(NEW_MOON_EPOCH)

        self.assertIsInstance(phase, MoonPhase)
        self.assertAlmostEqual(phase.fraction, 0.0, places=7)
        self.assertAlmostEqual(phase.age_days, 0.0, places=6)
        self.assertAlmostEqual(phase.illumination, 0.0, places=7)
        self.assertEqual(phase.index, 0)
        self.assertEqual(phase.name, "新月")
        self.assertEqual(phase.emoji, "🌑")
        self.assertTrue(phase.is_principal)
        self.assertEqual(phase.principal_event_key(), phase.event_key)

    def test_nasa_january_2000_principal_phases_are_classified(self) -> None:
        # Approximate event instants from NASA's 2000 phase table. This module
        # uses a mean synodic month, so classification is tested more strictly
        # than exact event timing.
        cases = (
            (datetime(2000, 1, 6, 18, 15, tzinfo=UTC), 0, "新月"),
            (datetime(2000, 1, 14, 13, 34, tzinfo=UTC), 2, "上弦月"),
            (datetime(2000, 1, 21, 4, 40, tzinfo=UTC), 4, "满月"),
            (datetime(2000, 1, 28, 7, 57, tzinfo=UTC), 6, "下弦月"),
        )

        for moment, index, name in cases:
            with self.subTest(moment=moment):
                phase = calculate_moon_phase(moment)
                self.assertEqual(phase.index, index)
                self.assertEqual(phase.name, name)
                self.assertTrue(phase.is_principal)

    def test_principal_phase_illumination_is_plausible(self) -> None:
        new_moon = calculate_moon_phase(datetime(2000, 1, 6, 18, 15, tzinfo=UTC))
        first_quarter = calculate_moon_phase(
            datetime(2000, 1, 14, 13, 34, tzinfo=UTC)
        )
        full_moon = calculate_moon_phase(
            datetime(2000, 1, 21, 4, 40, tzinfo=UTC)
        )
        last_quarter = calculate_moon_phase(
            datetime(2000, 1, 28, 7, 57, tzinfo=UTC)
        )

        self.assertLess(new_moon.illumination, 0.01)
        self.assertAlmostEqual(first_quarter.illumination, 0.5, delta=0.08)
        self.assertGreater(full_moon.illumination, 0.99)
        self.assertAlmostEqual(last_quarter.illumination, 0.5, delta=0.08)

    def test_mean_synodic_period_repeats_phase(self) -> None:
        later = NEW_MOON_EPOCH + timedelta(days=SYNODIC_MONTH_DAYS)
        phase = calculate_moon_phase(later)

        self.assertEqual(phase.index, 0)
        self.assertAlmostEqual(phase.fraction, 0.0, delta=2e-9)
        self.assertAlmostEqual(phase.illumination, 0.0, delta=1e-12)
        self.assertNotEqual(
            phase.event_key,
            calculate_moon_phase(NEW_MOON_EPOCH).event_key,
        )

    def test_same_instant_is_identical_across_timezones(self) -> None:
        utc_moment = datetime(2026, 7, 25, 12, 30, tzinfo=UTC)
        china_moment = utc_moment.astimezone(timezone(timedelta(hours=8)))

        self.assertEqual(
            calculate_moon_phase(utc_moment),
            calculate_moon_phase(china_moment),
        )

    def test_naive_datetime_is_interpreted_as_utc(self) -> None:
        naive = datetime(2026, 7, 25, 12, 30)
        aware = naive.replace(tzinfo=UTC)

        self.assertEqual(
            calculate_moon_phase(naive),
            calculate_moon_phase(aware),
        )

    def test_all_eight_phase_names_and_emojis_are_reachable(self) -> None:
        for expected_index in range(8):
            moment = NEW_MOON_EPOCH + timedelta(
                days=SYNODIC_MONTH_DAYS * expected_index / 8
            )
            with self.subTest(index=expected_index):
                phase = calculate_moon_phase(moment)
                self.assertEqual(phase.index, expected_index)
                self.assertEqual(phase.name, PHASE_NAMES[expected_index])
                self.assertEqual(phase.emoji, PHASE_EMOJIS[expected_index])

    def test_eighth_phase_boundary_rounds_to_nearest_phase(self) -> None:
        boundary_days = SYNODIC_MONTH_DAYS / 16
        before = NEW_MOON_EPOCH + timedelta(days=boundary_days, seconds=-1)
        after = NEW_MOON_EPOCH + timedelta(days=boundary_days, seconds=1)

        self.assertEqual(calculate_moon_phase(before).index, 0)
        self.assertEqual(calculate_moon_phase(after).index, 1)

    def test_pre_epoch_fraction_and_ranges_are_safe(self) -> None:
        moments = (
            NEW_MOON_EPOCH - timedelta(seconds=1),
            datetime(1900, 1, 1, tzinfo=UTC),
            datetime(2099, 12, 31, 23, 59, 59, tzinfo=UTC),
        )

        for moment in moments:
            with self.subTest(moment=moment):
                phase = calculate_moon_phase(moment)
                self.assertGreaterEqual(phase.fraction, 0.0)
                self.assertLess(phase.fraction, 1.0)
                self.assertGreaterEqual(phase.age_days, 0.0)
                self.assertLess(phase.age_days, SYNODIC_MONTH_DAYS)
                self.assertGreaterEqual(phase.illumination, 0.0)
                self.assertLessEqual(phase.illumination, 1.0)
                self.assertGreaterEqual(phase.event_distance_days, 0.0)
                self.assertLessEqual(
                    phase.event_distance_days,
                    SYNODIC_MONTH_DAYS / 16 + 1e-8,
                )

    def test_principal_event_key_is_stable_and_filterable(self) -> None:
        before = calculate_moon_phase(NEW_MOON_EPOCH - timedelta(hours=12))
        after = calculate_moon_phase(NEW_MOON_EPOCH + timedelta(hours=12))
        crescent = calculate_moon_phase(
            NEW_MOON_EPOCH + timedelta(days=SYNODIC_MONTH_DAYS / 8)
        )
        next_principal = calculate_moon_phase(
            NEW_MOON_EPOCH + timedelta(days=SYNODIC_MONTH_DAYS / 4)
        )

        self.assertEqual(before.principal_event_key(), after.principal_event_key())
        self.assertIsNone(before.principal_event_key(within_days=0.25))
        self.assertIsNone(crescent.principal_event_key())
        self.assertNotEqual(
            before.principal_event_key(),
            next_principal.principal_event_key(),
        )

    def test_principal_event_window_rejects_invalid_values(self) -> None:
        phase = calculate_moon_phase(NEW_MOON_EPOCH)

        for invalid in (-0.1, float("inf"), float("nan")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    phase.principal_event_key(within_days=invalid)

    def test_rejects_non_datetime_input(self) -> None:
        with self.assertRaises(TypeError):
            calculate_moon_phase("2000-01-06")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

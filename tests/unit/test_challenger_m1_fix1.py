"""Adversarial Verification Suite for Milestone 1 Fix 1.

Written by Challenger M1 Fix 1 (teamwork_preview_challenger).
Empirically stress-tests FailsafeWatchdog:
1. Rapid trigger -> reset -> trigger cycles (thread liveness and subsequent intrusion detection).
2. Concurrent start() and stop() cycles across multiple threads (thread safety, race conditions, deadlocks).
3. Extreme coordinates (negative overshoot (-60.0, -60.0), subpixel bounds, NaN/Inf, and float overflow).
"""

from __future__ import annotations

import math
import random
import threading
import time
import unittest
from typing import List, Tuple

from src.actuators.failsafe import FailsafeWatchdog
from src.actuators.types import (
    CornerLocation,
    FailsafeEmergencyStop,
    FailsafeTriggerInfo,
    get_screen_corner,
    is_screen_corner,
)


class TestFailsafeRapidTriggerResetCycles(unittest.TestCase):
    """Verifies thread liveness and re-arming across rapid trigger -> reset -> trigger cycles."""

    def test_rapid_trigger_reset_trigger_cycles_all_corners(self) -> None:
        """50 consecutive cycles of corner intrusion -> reset -> secondary intrusion.

        Verifies:
        1. Thread remains alive throughout all cycles.
        2. reset() cleanly re-arms the watchdog without thread death.
        3. Subsequent intrusions in different corners are reliably intercepted.
        """
        simulated_pos = [500.0, 500.0]
        wd = FailsafeWatchdog(
            position_getter=lambda: (simulated_pos[0], simulated_pos[1]),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            frequency=100.0,
            auto_release_modifiers=False,
            trap_signals=False,
        )
        wd.start()
        self.assertTrue(wd.is_running)

        corners = [
            (0.0, 0.0),        # top-left
            (1920.0, 0.0),     # top-right
            (0.0, 1080.0),     # bottom-left
            (1920.0, 1080.0),  # bottom-right
        ]

        try:
            for cycle in range(50):
                # 1. Trigger intrusion
                target_corner = corners[cycle % 4]
                simulated_pos[0], simulated_pos[1] = target_corner
                deadline = time.time() + 0.5
                while not wd.is_triggered and time.time() < deadline:
                    time.sleep(0.002)

                self.assertTrue(wd.is_triggered, f"Cycle {cycle}: failed to trigger on {target_corner}")
                self.assertTrue(wd.is_running, f"Cycle {cycle}: watchdog thread died on trigger")

                # 2. Reset to safe coordinates
                simulated_pos[0], simulated_pos[1] = 500.0, 500.0
                wd.reset()
                self.assertFalse(wd.is_triggered, f"Cycle {cycle}: still triggered after reset")
                self.assertTrue(wd.is_running, f"Cycle {cycle}: watchdog thread died on reset")

                # 3. Subsequent intrusion in another corner
                second_corner = corners[(cycle + 1) % 4]
                simulated_pos[0], simulated_pos[1] = second_corner
                deadline = time.time() + 0.5
                while not wd.is_triggered and time.time() < deadline:
                    time.sleep(0.002)

                self.assertTrue(wd.is_triggered, f"Cycle {cycle}: failed to trigger on second intrusion {second_corner}")
                self.assertTrue(wd.is_running, f"Cycle {cycle}: thread died on second intrusion")

                # 4. Clean reset for next iteration
                simulated_pos[0], simulated_pos[1] = 500.0, 500.0
                wd.reset()

        finally:
            wd.stop()
            self.assertFalse(wd.is_running)


class TestFailsafeConcurrentStartStopCycles(unittest.TestCase):
    """Stress-tests multi-threaded start() and stop() cycles for deadlocks and race conditions."""

    def test_concurrent_multi_threaded_start_stop_stress(self) -> None:
        """8 worker threads aggressively interleaving start, stop, reset, and check calls."""
        wd = FailsafeWatchdog(
            position_getter=lambda: (
                random.choice([0.0, 500.0, 1920.0]),
                random.choice([0.0, 500.0, 1080.0]),
            ),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            frequency=200.0,
            auto_release_modifiers=False,
            trap_signals=False,
        )

        num_threads = 8
        iterations_per_thread = 40
        errors: List[Exception] = []

        def worker(action: str) -> None:
            try:
                for _ in range(iterations_per_thread):
                    if action == "start":
                        wd.start()
                    elif action == "stop":
                        wd.stop(timeout=0.05)
                    elif action == "reset":
                        wd.reset()
                    elif action == "check":
                        try:
                            wd.check_failsafe()
                        except FailsafeEmergencyStop:
                            pass
                    time.sleep(random.uniform(0.0001, 0.001))
            except Exception as e:
                errors.append(e)

        threads = []
        actions = ["start", "stop", "reset", "check"] * 2
        for act in actions:
            t = threading.Thread(target=worker, args=(act,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive(), "Worker thread deadlocked during concurrent start/stop/reset/check")

        wd.stop(timeout=0.5)
        self.assertEqual(len(errors), 0, f"Errors during concurrent start/stop: {errors}")


class TestFailsafeExtremeCoordinates(unittest.TestCase):
    """Adversarial testing of extreme coordinates: negative, subpixel, NaN, Inf, and overflow."""

    def setUp(self) -> None:
        self.wd = FailsafeWatchdog(
            margin=5.0,
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
        )

    def test_negative_panic_overshoot_detection(self) -> None:
        """Negative coordinates like (-60.0, -60.0) must be detected as top-left corner."""
        self.assertTrue(is_screen_corner(-60.0, -60.0, 1920.0, 1080.0, 5.0))
        match = self.wd.check_corner(-60.0, -60.0)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "top-left")

        # Past bottom-right
        self.assertTrue(is_screen_corner(2000.0, 1200.0, 1920.0, 1080.0, 5.0))
        match_br = self.wd.check_corner(2000.0, 1200.0)
        self.assertIsNotNone(match_br)
        self.assertEqual(match_br[0], "bottom-right")

    def test_subpixel_precision_and_boundary_discrimination(self) -> None:
        """Discriminates coordinates at subpixel epsilon around the 5.0px margin."""
        w, h = 1920.0, 1080.0
        m = 5.0

        # Exact threshold: inside corner
        self.assertTrue(is_screen_corner(5.0, 5.0, w, h, m))
        self.assertIsNotNone(self.wd.check_corner(5.0, 5.0))

        # Epsilon beyond threshold: outside corner
        self.assertFalse(is_screen_corner(5.0000000000001, 5.0000000000001, w, h, m))
        self.assertIsNone(self.wd.check_corner(5.0000000000001, 5.0000000000001))

        # Epsilon within threshold: inside corner
        self.assertTrue(is_screen_corner(4.9999999999999, 4.9999999999999, w, h, m))
        self.assertIsNotNone(self.wd.check_corner(4.9999999999999, 4.9999999999999))

    def test_nan_and_inf_graceful_handling(self) -> None:
        """NaN, +Inf, -Inf must return None safely without raising exceptions or triggering."""
        # NaN
        self.assertIsNone(self.wd.check_corner(float("nan"), 500.0))
        self.assertIsNone(self.wd.check_corner(500.0, float("nan")))
        self.assertIsNone(self.wd.check_corner(float("nan"), float("nan")))

        # Inf
        self.assertIsNone(self.wd.check_corner(float("inf"), 500.0))
        self.assertIsNone(self.wd.check_corner(500.0, float("inf")))
        self.assertIsNone(self.wd.check_corner(float("-inf"), float("-inf")))

    def test_extreme_float_overflow_vulnerability(self) -> None:
        """Empirically demonstrates that large floats (>1.34e154) trigger OverflowError in check_corner.

        In check_corner line 310, dist_sq = (x - cx) ** 2 + (y - cy) ** 2 squares the
        coordinate, which exceeds IEEE-754 64-bit float capacity (~1.797e308).
        """
        extreme_coord = 1e300
        with self.assertRaises(OverflowError):
            self.wd.check_corner(extreme_coord, extreme_coord)


class TestFailsafeDeadlockVulnerability(unittest.TestCase):
    """Empirically demonstrates lock contention and thread join delay in stop()."""

    def test_stop_holds_lock_during_thread_join_vulnerability(self) -> None:
        """Empirically demonstrates that stop() holds self._lock while waiting for thread.join().

        When the watchdog thread is inside trigger(), it requests self._lock.
        If stop() is called, stop() holds self._lock and calls thread.join(timeout),
        blocking for the entire timeout duration (200ms) rather than exiting immediately.
        """
        pos_hook_entered = threading.Event()
        allow_trigger = threading.Event()

        def sync_pos() -> Tuple[float, float]:
            pos_hook_entered.set()
            allow_trigger.wait()
            return (0.0, 0.0)

        wd = FailsafeWatchdog(
            position_getter=sync_pos,
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            frequency=100.0,
            auto_release_modifiers=False,
            trap_signals=False,
        )
        wd.start()
        pos_hook_entered.wait()

        def stopper():
            wd.stop(timeout=0.2)

        t_stop = threading.Thread(target=stopper)
        t_stop.start()

        # Deterministically wait for stopper to enter stop() and acquire wd._lock
        self.assertTrue(wd._stop_requested.wait(timeout=1.0))

        t0 = time.perf_counter()
        allow_trigger.set()

        t_stop.join(timeout=1.0)
        elapsed = time.perf_counter() - t0

        # Demonstrates the join timeout delay: took ~0.2s because thread was blocked on _lock
        self.assertGreaterEqual(
            elapsed, 0.18,
            f"stop() should have blocked for join timeout due to lock contention; took {elapsed:.4f}s",
        )
        # Demonstrates post-stop trigger execution
        self.assertTrue(wd.is_triggered, "Watchdog triggered after stop() was called")


if __name__ == "__main__":
    unittest.main()

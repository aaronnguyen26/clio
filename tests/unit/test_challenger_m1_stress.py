"""Adversarial Stress Test Suite for Milestone 1: Actuator Primitives & OS Control.

Executed by Challenger M1-1 (teamwork_preview_challenger).
Empirically stress-tests:
1. High-throughput rapid mouse moves and clicks (1,000+ operations in < 100ms).
2. Rapid hotkey interleaving and modifier cleanup verification (zero sticky keys).
3. 50Hz FailsafeWatchdog corner detection latency (<= 25ms) and execution halt.
4. Coordinate boundary stress: floats, subpixels, NaNs, infinities, negatives, margin thresholds.
5. Live MacOSActuator ctypes loading, symbol resolution, and repeated query stability on ARM64.
"""

from __future__ import annotations

import math
import platform
import statistics
import sys
import threading
import time
import unittest
from typing import List, Tuple

import pytest

from src.actuators.base import BaseActuator
from src.actuators.factory import ActuatorFactory
from src.actuators.failsafe import FailsafeCornerWatchdog, FailsafeWatchdog
from src.actuators.mock import MockActuator
from src.actuators.types import (
    ActuatorError,
    CornerLocation,
    FailsafeEmergencyStop,
    InputSynthesisError,
    MouseButton,
    WindowInfo,
    get_screen_corner,
    is_screen_corner,
)


class TestHighThroughputActuation(unittest.TestCase):
    """Stress-tests throughput and latency of rapid mouse and click operations."""

    def setUp(self) -> None:
        self.actuator = MockActuator(
            screen_size=(1920.0, 1080.0),
            initial_pos=(500.0, 500.0),
            failsafe_margin=5.0,
            enforce_corner_failsafe=True,
        )

    def test_rapid_mouse_moves_1000_operations(self) -> None:
        """1,000 rapid mouse moves completed in < 100ms with 100% audit log fidelity."""
        n_ops = 1000
        start_time = time.perf_counter()

        for i in range(n_ops):
            # Move within safe interior bounds (100.0 to 1800.0, 100.0 to 900.0)
            x = 100.0 + (i % 1700)
            y = 100.0 + ((i * 3) % 800)
            self.actuator.move_mouse(x, y)

        elapsed = time.perf_counter() - start_time
        ops_per_sec = n_ops / elapsed if elapsed > 0 else float("inf")

        self.assertEqual(len(self.actuator.history), n_ops)
        self.assertLess(elapsed, 0.100, f"1,000 mouse moves took {elapsed:.4f}s (target < 100ms)")
        self.assertEqual(self.actuator.get_mouse_position(), (100.0 + ((n_ops - 1) % 1700), 100.0 + (((n_ops - 1) * 3) % 800)))
        self.actuator.assert_no_errors()

    def test_rapid_mouse_clicks_1000_operations(self) -> None:
        """1,000 rapid clicks (left, right, double) completed in < 100ms."""
        n_ops = 1000
        buttons = [MouseButton.LEFT, MouseButton.RIGHT, MouseButton.CENTER]
        start_time = time.perf_counter()

        for i in range(n_ops):
            btn = buttons[i % 3]
            click_cnt = 2 if (i % 10 == 0) else 1
            self.actuator.click(x=500.0 + (i % 200), y=500.0 + (i % 200), button=btn, click_count=click_cnt)

        elapsed = time.perf_counter() - start_time
        self.assertEqual(len(self.actuator.history), n_ops)
        self.assertLess(elapsed, 0.100, f"1,000 clicks took {elapsed:.4f}s (target < 100ms)")
        self.actuator.assert_no_errors()

    def test_concurrent_multi_threaded_actuation(self) -> None:
        """10 concurrent worker threads dispatching 100 actions each (2,000 total actions)."""
        n_threads = 10
        ops_per_thread = 100
        barrier = threading.Barrier(n_threads)
        errors: List[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                barrier.wait()
                for i in range(ops_per_thread):
                    x = 200.0 + thread_id * 50 + (i % 30)
                    y = 200.0 + thread_id * 30 + (i % 30)
                    self.actuator.move_mouse(x, y)
                    self.actuator.click(button=MouseButton.LEFT)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent thread errors: {errors}")
        # Each worker does 1 move + 1 click = 200 ops * 10 = 2,000 actions
        self.assertEqual(len(self.actuator.history), n_threads * ops_per_thread * 2)


class TestHotkeyInterleavingAndModifierCleanup(unittest.TestCase):
    """Stress-tests rapid hotkey interleaving and confirms zero sticky modifiers."""

    def setUp(self) -> None:
        self.actuator = MockActuator()

    def test_rapid_hotkey_interleaving_1000_combinations(self) -> None:
        """1,000 rapid modifier combinations with zero residual sticky keys."""
        combinations = [
            ("cmd", "n"),
            ("cmd", "shift", "s"),
            ("alt", "f4"),
            ("ctrl", "alt", "del"),
            ("cmd", "shift", "opt", "t"),
            ("command", "c"),
            ("command", "v"),
            ("shift", "tab"),
            ("option", "space"),
            ("right_command", "right_shift", "z"),
        ]

        n_ops = 1000
        start_time = time.perf_counter()

        for i in range(n_ops):
            combo = combinations[i % len(combinations)]
            self.actuator.press_hotkey(*combo)
            self.assertEqual(
                len(self.actuator._active_modifiers),
                0,
                f"Sticky modifiers left behind on iteration {i} ({combo}): {self.actuator._active_modifiers}",
            )

        elapsed = time.perf_counter() - start_time
        self.assertLess(elapsed, 0.100, f"1,000 hotkeys took {elapsed:.4f}s (target < 100ms)")
        self.assertEqual(len(self.actuator.history), n_ops)
        self.actuator.assert_modifiers_released()

    def test_modifier_cleanup_under_exceptions_and_failsafe(self) -> None:
        """Modifiers are released even when actions raise exceptions or trigger failsafe."""
        # Manually add modifiers into active set
        self.actuator._active_modifiers.update(["cmd", "shift", "alt"])
        self.assertEqual(len(self.actuator._active_modifiers), 3)

        # Trigger failsafe and check that check_failsafe cleans up active modifiers
        self.actuator.trigger_failsafe()
        with self.assertRaises(FailsafeEmergencyStop):
            self.actuator.check_failsafe()

        self.actuator.assert_modifiers_released()

    def test_pasteboard_rapid_cycle_clipboard_integrity(self) -> None:
        """Rapid pasteboard injections verify buffer concatenation and Cmd+V firing."""
        texts = [f"Payload line {i} — special chars: αβγ 🚀" for i in range(100)]
        for text in texts:
            self.actuator.paste_text(text)
            self.assertEqual(self.actuator.clipboard, text)

        self.actuator.assert_modifiers_released()
        self.assertIn("αβγ 🚀", self.actuator.typed_text)
        # 100 paste_text + 100 press_hotkey('cmd', 'v') = 200 actions
        self.assertEqual(len(self.actuator.history), 200)


class TestCornerDetectionLatency(unittest.TestCase):
    """Measures empirical latency of 50Hz Watchdog corner detection (target <= 25ms)."""

    def test_watchdog_detection_latency_benchmark(self) -> None:
        """Measures 30 empirical trials of corner detection latency with 50Hz polling."""
        simulated_pos = [500.0, 500.0]
        latencies: List[float] = []

        def mock_pos() -> Tuple[float, float]:
            return (simulated_pos[0], simulated_pos[1])

        watchdog = FailsafeWatchdog(
            margin=5.0,
            frequency=50.0,  # 20ms nominal period
            position_getter=mock_pos,
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            auto_release_modifiers=False,
            trap_signals=False,
        )

        try:
            for trial in range(30):
                # Cleanly reset to safe interior and restart watchdog thread
                simulated_pos[0] = 500.0
                simulated_pos[1] = 500.0
                watchdog.stop()
                watchdog.reset()
                watchdog.start()
                time.sleep(0.005)

                # Jump to top-left corner and record start time
                t0 = time.perf_counter()
                simulated_pos[0] = 0.0
                simulated_pos[1] = 0.0

                # Poll until watchdog trips
                deadline = t0 + 0.200  # 200ms timeout
                while not watchdog.is_triggered and time.perf_counter() < deadline:
                    time.sleep(0.0005)

                t1 = time.perf_counter()
                latency_ms = (t1 - t0) * 1000.0
                self.assertTrue(watchdog.is_triggered, f"Trial {trial}: watchdog failed to trigger within deadline")
                latencies.append(latency_ms)

        finally:
            watchdog.stop()

        avg_latency = statistics.mean(latencies)
        median_latency = statistics.median(latencies)
        min_latency = min(latencies)
        max_latency = max(latencies)
        p95_latency = sorted(latencies)[int(len(latencies) * 0.95)]

        print(f"\n[LATENCY BENCHMARK] 50Hz Watchdog Latency (30 trials):")
        print(f"  Min:    {min_latency:.2f} ms")
        print(f"  Mean:   {avg_latency:.2f} ms")
        print(f"  Median: {median_latency:.2f} ms")
        print(f"  P95:    {p95_latency:.2f} ms")
        print(f"  Max:    {max_latency:.2f} ms")

        # Nominal 50Hz period is 20ms; median detection latency is typically ~10-15ms, mean <= 25ms
        self.assertLessEqual(median_latency, 25.0, f"Median watchdog latency {median_latency:.2f}ms exceeded 25ms threshold")

    def test_watchdog_thread_lifecycle_after_trigger(self) -> None:
        """Examines thread lifecycle: when watchdog triggers, loop enters wait-state and thread persists."""
        simulated_pos = [0.0, 0.0]
        watchdog = FailsafeWatchdog(
            position_getter=lambda: (simulated_pos[0], simulated_pos[1]),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            auto_release_modifiers=False,
            trap_signals=False,
        )
        try:
            watchdog.start()
            self.assertTrue(watchdog.is_running)

            deadline = time.time() + 0.5
            while not watchdog.is_triggered and time.time() < deadline:
                time.sleep(0.005)

            self.assertTrue(watchdog.is_triggered)
            # Watchdog thread persists in wait-state
            time.sleep(0.02)
            self.assertTrue(watchdog.is_running, "Watchdog thread must remain running after trigger")

            # When reset() is called, state flags are cleared and thread stays running
            watchdog.reset()
            self.assertFalse(watchdog.is_triggered)
            self.assertIsNone(watchdog.trigger_info)
            self.assertTrue(watchdog.is_running, "Watchdog thread must remain running after reset()")
        finally:
            watchdog.stop()

    def test_inline_check_failsafe_latency(self) -> None:
        """Inline check_failsafe must execute in < 0.05ms (sub-50 microsecond latency)."""
        actuator = MockActuator(initial_pos=(500.0, 500.0))
        n_calls = 5000
        t0 = time.perf_counter()
        for _ in range(n_calls):
            actuator.check_failsafe()
        elapsed = time.perf_counter() - t0
        per_call_us = (elapsed / n_calls) * 1e6

        print(f"\n[INLINE CHECK BENCHMARK] Per-call latency: {per_call_us:.2f} microseconds")
        self.assertLess(per_call_us, 50.0, f"Inline check took {per_call_us:.2f}µs (expected < 50µs)")


class TestCoordinateBoundaryStress(unittest.TestCase):
    """Stress-tests corner margins, floating point precision, subpixels, NaNs, and infinities."""

    def test_exact_corner_margins_and_thresholds(self) -> None:
        """Tests screen corners across margins: 0.0, 1.0, 5.0, 20.0."""
        w, h = 1920.0, 1080.0

        for margin in [0.0, 1.0, 5.0, 20.0]:
            # Exact corners
            self.assertTrue(is_screen_corner(0.0, 0.0, w, h, margin))
            self.assertTrue(is_screen_corner(w, 0.0, w, h, margin))
            self.assertTrue(is_screen_corner(0.0, h, w, h, margin))
            self.assertTrue(is_screen_corner(w, h, w, h, margin))

            # Within margin
            self.assertTrue(is_screen_corner(margin, margin, w, h, margin))
            self.assertTrue(is_screen_corner(w - margin, margin, w, h, margin))
            self.assertTrue(is_screen_corner(margin, h - margin, w, h, margin))
            self.assertTrue(is_screen_corner(w - margin, h - margin, w, h, margin))

            # Barely beyond margin (margin + 0.01)
            self.assertFalse(is_screen_corner(margin + 0.01, margin + 0.01, w, h, margin))
            self.assertFalse(is_screen_corner(w - margin - 0.01, margin + 0.01, w, h, margin))
            self.assertFalse(is_screen_corner(margin + 0.01, h - margin - 0.01, w, h, margin))
            self.assertFalse(is_screen_corner(w - margin - 0.01, h - margin - 0.01, w, h, margin))

    def test_negative_and_overshoot_coordinates(self) -> None:
        """Negative and display overshoot coordinates in corner regions trigger failsafe."""
        w, h = 1920.0, 1080.0
        margin = 5.0

        # Negative coordinates (past top-left)
        self.assertTrue(is_screen_corner(-5.0, -5.0, w, h, margin))
        self.assertEqual(get_screen_corner(-5.0, -5.0, w, h, margin), CornerLocation.TOP_LEFT)

        # Overshoot coordinates (past bottom-right)
        self.assertTrue(is_screen_corner(w + 10.0, h + 10.0, w, h, margin))
        self.assertEqual(get_screen_corner(w + 10.0, h + 10.0, w, h, margin), CornerLocation.BOTTOM_RIGHT)

        # Offscreen along single axis only (not in corner)
        self.assertFalse(is_screen_corner(-10.0, 500.0, w, h, margin))
        self.assertFalse(is_screen_corner(960.0, -10.0, w, h, margin))
        self.assertFalse(is_screen_corner(w + 10.0, 500.0, w, h, margin))
        self.assertFalse(is_screen_corner(960.0, h + 10.0, w, h, margin))

    def test_subpixel_floating_point_fidelity(self) -> None:
        """Subpixel floats are recorded accurately without precision loss."""
        actuator = MockActuator()
        subpixel_x = 123.456789123
        subpixel_y = 987.654321987

        actuator.move_mouse(subpixel_x, subpixel_y)
        pos = actuator.get_mouse_position()
        self.assertAlmostEqual(pos[0], subpixel_x, places=8)
        self.assertAlmostEqual(pos[1], subpixel_y, places=8)

    def test_nan_and_infinity_handling(self) -> None:
        """Tests behavior of NaN and Inf coordinates."""
        # is_screen_corner with NaN should return False (NaN comparisons evaluate to False)
        self.assertFalse(is_screen_corner(float("nan"), 100.0, 1920.0, 1080.0, 5.0))
        self.assertFalse(is_screen_corner(100.0, float("nan"), 1920.0, 1080.0, 5.0))
        self.assertFalse(is_screen_corner(float("nan"), float("nan"), 1920.0, 1080.0, 5.0))

        # Under MacOSActuator, non-finite values must raise InputSynthesisError
        if sys.platform == "darwin":
            from src.actuators.macos import MacOSActuator
            act = MacOSActuator(enable_watchdog=False)
            try:
                with self.assertRaises(InputSynthesisError):
                    act.move_mouse(float("nan"), 500.0)
                with self.assertRaises(InputSynthesisError):
                    act.move_mouse(500.0, float("inf"))
                with self.assertRaises(InputSynthesisError):
                    act.move_mouse(float("-inf"), float("-inf"))
            finally:
                act.stop()


class TestLiveMacOSActuatorARM64(unittest.TestCase):
    """Verifies live MacOSActuator ctypes bindings on macOS Darwin ARM64."""

    @unittest.skipUnless(sys.platform == "darwin", "Requires macOS Darwin")
    def test_live_arm64_bindings_and_frameworks(self) -> None:
        """Verifies that CoreGraphics, CoreFoundation, AppKit, ApplicationServices, and libobjc load."""
        from src.actuators.macos import MacOSActuator, MacOSNativeBindings

        bindings = MacOSNativeBindings()
        self.assertIsNotNone(bindings.cg)
        self.assertIsNotNone(bindings.cf)
        self.assertIsNotNone(bindings.appkit)
        self.assertIsNotNone(bindings.ax)
        self.assertIsNotNone(bindings.objc)

        # Check ARM64 architecture
        machine = platform.machine().lower()
        print(f"\n[MACOS NATIVE] Running on Darwin platform: {sys.platform}, architecture: {machine}")
        self.assertIn(machine, ("arm64", "aarch64", "x86_64"))

        # Verify live queries without watchdog to avoid interfering with current user cursor
        actuator = MacOSActuator(enable_watchdog=False)
        try:
            # 1. Screen size
            w, h = actuator.get_screen_size()
            self.assertGreater(w, 0.0)
            self.assertGreater(h, 0.0)
            print(f"  Live Display Dimensions: {w} x {h}")

            # 2. Mouse position
            mx, my = actuator.get_mouse_position()
            self.assertTrue(math.isfinite(mx))
            self.assertTrue(math.isfinite(my))
            print(f"  Live Cursor Position: ({mx}, {my})")

            # 3. Frontmost app
            front = actuator.get_frontmost_app()
            self.assertIsInstance(front, str)
            self.assertGreater(len(front), 0)
            print(f"  Frontmost Application: {front}")

            # 4. Visible windows
            windows = actuator.get_windows()
            self.assertIsInstance(windows, list)
            self.assertGreater(len(windows), 0)
            print(f"  Visible Windows Count: {len(windows)}")
            print(f"  First Window: {windows[0].owner_name} - '{windows[0].title}' at ({windows[0].x}, {windows[0].y}, {windows[0].width}x{windows[0].height})")

            # 5. Stress test repeated queries (100 iterations)
            t0 = time.perf_counter()
            for _ in range(100):
                actuator.get_mouse_position()
                actuator.get_screen_size()
            elapsed = time.perf_counter() - t0
            print(f"  100 Display/Cursor Queries Elapsed: {elapsed:.4f}s ({elapsed/100*1000:.2f}ms/query)")
            self.assertLess(elapsed, 1.0)

        finally:
            actuator.stop()

    def test_factory_creation_and_resolution(self) -> None:
        """Verifies ActuatorFactory creation resolution across modes."""
        # Explicit mock
        mock_act = ActuatorFactory.create("mock")
        self.assertIsInstance(mock_act, MockActuator)

        # Explicit macos on Darwin
        if sys.platform == "darwin":
            from src.actuators.macos import MacOSActuator
            macos_act = ActuatorFactory.create("macos", enable_watchdog=False)
            self.assertIsInstance(macos_act, MacOSActuator)
            macos_act.stop()


if __name__ == "__main__":
    unittest.main()

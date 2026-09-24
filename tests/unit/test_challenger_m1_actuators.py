"""Empirical Adversarial Challenge Suite for Milestone 1 Actuators.

Written by Challenger M1-2 (`teamwork_preview_challenger`).
Adversarially stress-tests:
1. FailsafeWatchdog:
   - Daemon thread lifecycle: start, stop, double-start, double-stop, and thread leak detection.
   - High-concurrency race conditions: concurrent check_failsafe() under active 50Hz polling.
   - Emergency stop callback invocation reliability, isolation, and exception behavior.
2. ActuatorFactory:
   - Environment variable resolution hierarchy (TASK_AUTOMATOR_ACTUATOR, CI, GITHUB_ACTIONS).
   - Platform spoofing and fallback behavior (mock fallback vs. ActuatorError).
   - Error handling for invalid modes and boundary cases.
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

from src.actuators.base import BaseActuator
from src.actuators.factory import ActuatorFactory, get_actuator
from src.actuators.failsafe import FailsafeWatchdog
from src.actuators.mock import MockActuator
from src.actuators.types import (
    ActuatorError,
    ActuatorMode,
    CornerLocation,
    FailsafeEmergencyStop,
    FailsafeTriggerInfo,
)


class TestFailsafeWatchdogLifecycleAdversarial(unittest.TestCase):
    """Stress tests for FailsafeWatchdog daemon thread lifecycle and leak detection."""

    def setUp(self) -> None:
        self.initial_threads = threading.enumerate()

    def test_single_start_and_stop_lifecycle(self) -> None:
        """Verifies clean thread startup, daemon property, and clean exit."""
        wd = FailsafeWatchdog(
            position_getter=lambda: (500.0, 500.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )
        self.assertFalse(wd.is_running)
        wd.start()
        self.assertTrue(wd.is_running)
        self.assertTrue(wd._thread.is_alive())
        self.assertTrue(wd._thread.daemon, "Watchdog thread must be marked daemon")

        wd.stop(timeout=1.0)
        self.assertFalse(wd.is_running)
        self.assertIsNone(wd._thread)

    def test_double_start_is_idempotent(self) -> None:
        """Calling start() twice must not spawn multiple threads or corrupt state."""
        wd = FailsafeWatchdog(
            position_getter=lambda: (500.0, 500.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )
        wd.start()
        first_thread = wd._thread
        self.assertIsNotNone(first_thread)

        # Call start again
        wd.start()
        second_thread = wd._thread

        self.assertIs(first_thread, second_thread, "Second start() must not replace running thread")
        self.assertTrue(wd.is_running)

        wd.stop()
        self.assertFalse(wd.is_running)

    def test_double_stop_is_idempotent(self) -> None:
        """Calling stop() multiple times on a running or already stopped watchdog must not raise."""
        wd = FailsafeWatchdog(
            position_getter=lambda: (500.0, 500.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )
        # Calling stop before start
        wd.stop()
        self.assertFalse(wd.is_running)

        # Start and stop once
        wd.start()
        self.assertTrue(wd.is_running)
        wd.stop()
        self.assertFalse(wd.is_running)

        # Stop second and third time
        wd.stop()
        wd.stop()
        self.assertFalse(wd.is_running)

    def test_rapid_start_stop_cycles_no_thread_leak(self) -> None:
        """50 rapid start/stop cycles must terminate all threads without leaking."""
        initial_count = len(threading.enumerate())

        wd = FailsafeWatchdog(
            position_getter=lambda: (500.0, 500.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )

        for _ in range(50):
            wd.start()
            self.assertTrue(wd.is_running)
            wd.stop(timeout=0.5)
            self.assertFalse(wd.is_running)

        # Ensure no watchdog threads remain in threading.enumerate()
        active_threads = threading.enumerate()
        watchdog_threads = [t for t in active_threads if "FailsafeWatchdogThread" in t.name]
        self.assertEqual(len(watchdog_threads), 0, f"Leaked watchdog threads: {watchdog_threads}")
        self.assertEqual(len(active_threads), initial_count)

    def test_concurrent_start_stop_stress(self) -> None:
        """Concurrent calls to start() and stop() from multiple threads must not deadlock."""
        wd = FailsafeWatchdog(
            position_getter=lambda: (500.0, 500.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )

        def worker_start():
            for _ in range(25):
                wd.start()
                time.sleep(0.001)

        def worker_stop():
            for _ in range(25):
                wd.stop()
                time.sleep(0.001)

        threads = [
            threading.Thread(target=worker_start),
            threading.Thread(target=worker_start),
            threading.Thread(target=worker_stop),
            threading.Thread(target=worker_stop),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)
            self.assertFalse(t.is_alive(), "Worker thread deadlocked during concurrent start/stop")

        # Clean final stop
        wd.stop()
        self.assertFalse(wd.is_running)


class TestFailsafeConcurrencyAndRaceConditions(unittest.TestCase):
    """Stress tests for concurrent check_failsafe calls and emergency stop dispatch."""

    def test_concurrent_check_failsafe_under_active_polling(self) -> None:
        """20 worker threads continuously calling check_failsafe() while 50Hz watchdog polls.

        Simulates cursor transitioning into corner mid-execution; verifies all workers catch
        FailsafeEmergencyStop and callbacks are invoked exactly once.
        """
        cursor_pos = [500.0, 500.0]
        pos_lock = threading.Lock()

        def mock_pos() -> tuple[float, float]:
            with pos_lock:
                return (cursor_pos[0], cursor_pos[1])

        callback_invocations: list[FailsafeTriggerInfo] = []
        callback_lock = threading.Lock()

        def emergency_callback(info: FailsafeTriggerInfo) -> None:
            with callback_lock:
                callback_invocations.append(info)

        releaser_invocations: list[tuple[float, float]] = []

        def mock_releaser(current_pos=None) -> None:
            releaser_invocations.append(current_pos or (0.0, 0.0))

        wd = FailsafeWatchdog(
            margin=5.0,
            frequency=50.0,
            position_getter=mock_pos,
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            modifier_releaser=mock_releaser,
            auto_release_modifiers=True,
            trap_signals=False,
            on_emergency=emergency_callback,
        )
        wd.start()

        num_workers = 20
        checks_per_worker = 100
        exceptions_caught: list[FailsafeEmergencyStop] = []
        normal_checks_passed = [0]
        workers_done = threading.Event()

        def worker_task():
            for _ in range(checks_per_worker):
                try:
                    wd.check_failsafe()
                    normal_checks_passed[0] += 1
                except FailsafeEmergencyStop as exc:
                    exceptions_caught.append(exc)
                    # Once triggered, remaining checks will raise as well
                time.sleep(0.0005)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(worker_task) for _ in range(num_workers)]

            # Let workers run normally for a brief moment
            time.sleep(0.01)

            # Move cursor into top-left corner (0.0, 0.0)
            with pos_lock:
                cursor_pos[0] = 0.0
                cursor_pos[1] = 0.0

            # Wait for all workers to complete
            concurrent.futures.wait(futures, timeout=5.0)

        wd.stop()

        # Empirical Assertions
        self.assertTrue(wd.is_triggered, "Watchdog must be in triggered state")
        self.assertGreater(len(exceptions_caught), 0, "Workers must have caught FailsafeEmergencyStop")
        self.assertEqual(
            len(callback_invocations), 1,
            f"Emergency callback must be called exactly once; was called {len(callback_invocations)} times",
        )
        self.assertEqual(callback_invocations[0].corner, "top-left")
        self.assertGreaterEqual(len(releaser_invocations), 1, "Modifiers must be released upon emergency stop")

    def test_callback_exception_isolation(self) -> None:
        """A crashing callback must not prevent other registered callbacks from executing."""
        healthy_called = []

        def failing_callback(info: FailsafeTriggerInfo) -> None:
            raise RuntimeError("Catastrophic callback failure")

        def healthy_callback(info: FailsafeTriggerInfo) -> None:
            healthy_called.append(info.corner)

        wd = FailsafeWatchdog(
            position_getter=lambda: (0.0, 0.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )
        wd.add_callback(failing_callback)
        wd.add_callback(healthy_callback)

        # Trigger failsafe
        wd.trigger(corner="top-left")

        self.assertEqual(healthy_called, ["top-left"], "Healthy callback must run despite failing peer callback")
        self.assertTrue(wd.is_triggered)

    def test_inline_check_failsafe_microsecond_latency(self) -> None:
        """Inline check_failsafe must execute in under 50 microseconds on clean path."""
        wd = FailsafeWatchdog(
            position_getter=lambda: (500.0, 500.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )

        # Warm up
        for _ in range(100):
            wd.check_failsafe()

        # Benchmark 1000 invocations
        iterations = 1000
        start = time.perf_counter()
        for _ in range(iterations):
            wd.check_failsafe()
        elapsed = time.perf_counter() - start

        avg_latency_us = (elapsed / iterations) * 1_000_000
        # Typical latency on modern Apple Silicon is < 5 microseconds
        self.assertLess(
            avg_latency_us, 50.0,
            f"check_failsafe() average latency ({avg_latency_us:.2f}µs) exceeded 50µs budget",
        )


class TestActuatorFactoryAdversarial(unittest.TestCase):
    """Adversarial challenge of ActuatorFactory environment resolution and fallback."""

    def setUp(self) -> None:
        self._orig_env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_env_var_override_mock(self) -> None:
        """TASK_AUTOMATOR_ACTUATOR=mock must always instantiate MockActuator."""
        os.environ["TASK_AUTOMATOR_ACTUATOR"] = "mock"
        actuator = ActuatorFactory.create()
        self.assertIsInstance(actuator, MockActuator)
        actuator.stop()

    def test_env_var_override_test(self) -> None:
        """TASK_AUTOMATOR_ACTUATOR=test must always instantiate MockActuator."""
        os.environ["TASK_AUTOMATOR_ACTUATOR"] = "test"
        actuator = ActuatorFactory.create()
        self.assertIsInstance(actuator, MockActuator)
        actuator.stop()

    def test_ci_environment_resolution(self) -> None:
        """CI=true and GITHUB_ACTIONS=true must select MockActuator in AUTO mode."""
        os.environ.pop("TASK_AUTOMATOR_ACTUATOR", None)

        # CI=true
        os.environ["CI"] = "true"
        actuator_ci = ActuatorFactory.create(mode=ActuatorMode.AUTO)
        self.assertIsInstance(actuator_ci, MockActuator)
        actuator_ci.stop()

        # CI=1
        os.environ["CI"] = "1"
        actuator_ci1 = ActuatorFactory.create(mode="auto")
        self.assertIsInstance(actuator_ci1, MockActuator)
        actuator_ci1.stop()

        # GITHUB_ACTIONS=true
        os.environ.pop("CI", None)
        os.environ["GITHUB_ACTIONS"] = "true"
        actuator_gha = ActuatorFactory.create()
        self.assertIsInstance(actuator_gha, MockActuator)
        actuator_gha.stop()

    def test_non_darwin_platform_fallback_behavior(self) -> None:
        """On non-Darwin OS, MACOS mode must fall back to Mock when allowed, or raise ActuatorError."""
        os.environ.pop("TASK_AUTOMATOR_ACTUATOR", None)
        os.environ.pop("CI", None)
        os.environ.pop("GITHUB_ACTIONS", None)

        with patch("sys.platform", "linux"):
            self.assertFalse(ActuatorFactory.is_macos())

            # AUTO mode falls back to MockActuator
            actuator_auto = ActuatorFactory.create(mode=ActuatorMode.AUTO)
            self.assertIsInstance(actuator_auto, MockActuator)
            actuator_auto.stop()

            # Explicit MACOS mode with fallback=True returns MockActuator
            actuator_fallback = ActuatorFactory.create(mode=ActuatorMode.MACOS, fallback_to_mock=True)
            self.assertIsInstance(actuator_fallback, MockActuator)
            actuator_fallback.stop()

            # Explicit MACOS mode with fallback=False MUST raise ActuatorError
            with self.assertRaises(ActuatorError) as ctx:
                ActuatorFactory.create(mode=ActuatorMode.MACOS, fallback_to_mock=False)
            self.assertIn("non-macOS platform", str(ctx.exception))

    def test_macos_actuator_init_failure_fallback(self) -> None:
        """If MacOSActuator constructor raises, fallback_to_mock=True must succeed, False must raise."""
        os.environ.pop("TASK_AUTOMATOR_ACTUATOR", None)
        os.environ.pop("CI", None)
        os.environ.pop("GITHUB_ACTIONS", None)

        with patch("sys.platform", "darwin"):
            with patch("src.actuators.macos.MacOSActuator", side_effect=RuntimeError("CoreGraphics failure")):
                # With fallback -> returns MockActuator
                actuator = ActuatorFactory.create(mode=ActuatorMode.MACOS, fallback_to_mock=True)
                self.assertIsInstance(actuator, MockActuator)
                actuator.stop()

                # Without fallback -> raises ActuatorError
                with self.assertRaises(ActuatorError) as ctx:
                    ActuatorFactory.create(mode=ActuatorMode.MACOS, fallback_to_mock=False)
                self.assertIn("Failed to initialize MacOSActuator", str(ctx.exception))

    def test_invalid_mode_string_raises_actuator_error(self) -> None:
        """Passing an unrecognized mode string must raise ActuatorError with diagnostic info."""
        os.environ.pop("TASK_AUTOMATOR_ACTUATOR", None)

        invalid_modes = ["bogus", "virtual_reality", "quantum", ""]
        for invalid_mode in invalid_modes:
            with self.assertRaises(ActuatorError) as ctx:
                ActuatorFactory.create(mode=invalid_mode)
            self.assertIn("Unknown actuator mode", str(ctx.exception))

    def test_convenience_get_actuator_entrypoint(self) -> None:
        """get_actuator() wrapper function delegates correctly to ActuatorFactory.create()."""
        os.environ["TASK_AUTOMATOR_ACTUATOR"] = "mock"
        actuator = get_actuator(mode="mock", screen_size=(1024.0, 768.0))
        self.assertIsInstance(actuator, MockActuator)
        self.assertEqual(actuator.screen_size, (1024.0, 768.0))
        actuator.stop()

    def test_factory_macos_creation_on_darwin(self) -> None:
        """On Darwin, ActuatorFactory.create(mode='macos') returns MacOSActuator."""
        if sys.platform != "darwin":
            self.skipTest("Requires native macOS Darwin platform")

        os.environ.pop("TASK_AUTOMATOR_ACTUATOR", None)
        os.environ.pop("CI", None)
        os.environ.pop("GITHUB_ACTIONS", None)

        from src.actuators.macos import MacOSActuator

        # Explicit mode
        actuator = ActuatorFactory.create(mode="macos")
        self.assertIsInstance(actuator, MacOSActuator)
        actuator.stop()

        # Via TASK_AUTOMATOR_ACTUATOR=macos
        os.environ["TASK_AUTOMATOR_ACTUATOR"] = "macos"
        actuator_env = ActuatorFactory.create()
        self.assertIsInstance(actuator_env, MacOSActuator)
        actuator_env.stop()


class TestFailsafeWatchdogEmpiricalBenchmarks(unittest.TestCase):
    """Empirical latency and high-contention stress tests for FailsafeWatchdog."""

    def test_watchdog_50hz_reaction_latency(self) -> None:
        """Measures empirical reaction time from corner entry to triggered event.

        At 50Hz (20ms cycle), reaction time should reliably fall under 80ms.
        """
        cursor_pos = [500.0, 500.0]
        pos_lock = threading.Lock()

        def mock_pos() -> tuple[float, float]:
            with pos_lock:
                return (cursor_pos[0], cursor_pos[1])

        emergency_event = threading.Event()
        trigger_times = []

        def on_trigger(info: FailsafeTriggerInfo) -> None:
            trigger_times.append(time.perf_counter())
            emergency_event.set()

        wd = FailsafeWatchdog(
            margin=5.0,
            frequency=50.0,
            position_getter=mock_pos,
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
            on_emergency=on_trigger,
        )
        wd.start()

        # Let the loop establish steady state
        time.sleep(0.04)

        t_move = time.perf_counter()
        with pos_lock:
            cursor_pos[0] = 0.0
            cursor_pos[1] = 0.0

        triggered = emergency_event.wait(timeout=0.5)
        wd.stop()

        self.assertTrue(triggered, "Watchdog failed to trigger within 500ms")
        reaction_latency_ms = (trigger_times[0] - t_move) * 1000.0
        # Verification: latency should be <= 80ms (4x 20ms period)
        self.assertLess(
            reaction_latency_ms, 80.0,
            f"Reaction latency {reaction_latency_ms:.2f}ms exceeded 80ms budget",
        )

    def test_concurrent_check_failsafe_100_threads(self) -> None:
        """100 concurrent threads calling check_failsafe() during corner trigger."""
        cursor_pos = [500.0, 500.0]
        pos_lock = threading.Lock()

        def mock_pos() -> tuple[float, float]:
            with pos_lock:
                return (cursor_pos[0], cursor_pos[1])

        wd = FailsafeWatchdog(
            margin=5.0,
            frequency=50.0,
            position_getter=mock_pos,
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )
        wd.start()

        num_threads = 100
        exceptions: list[FailsafeEmergencyStop] = []
        barrier = threading.Barrier(num_threads)

        def worker():
            barrier.wait()
            for _ in range(20):
                try:
                    wd.check_failsafe()
                except FailsafeEmergencyStop as e:
                    exceptions.append(e)
                time.sleep(0.0005)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()

        # Shortly after starting, trip the corner
        time.sleep(0.005)
        with pos_lock:
            cursor_pos[0] = 1920.0
            cursor_pos[1] = 1080.0

        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "Worker thread deadlocked during 100-thread contention")

        wd.stop()
        self.assertTrue(wd.is_triggered)
        self.assertGreater(len(exceptions), 0, "At least one exception must be caught")

    def test_dynamic_callback_mutation_concurrency(self) -> None:
        """Concurrently adding and removing callbacks while failsafe is triggering."""
        wd = FailsafeWatchdog(
            position_getter=lambda: (0.0, 0.0),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )

        stop_event = threading.Event()

        def callback_mutator():
            cb = lambda info: None
            while not stop_event.is_set():
                wd.add_callback(cb)
                wd.remove_callback(cb)

        threads = [threading.Thread(target=callback_mutator) for _ in range(5)]
        for t in threads:
            t.start()

        # Trigger while mutations are happening
        for _ in range(10):
            wd.trigger(corner="top-left")
            wd.reset()

        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)
            self.assertFalse(t.is_alive())

    def test_failsafe_watchdog_thread_death_on_trigger(self) -> None:
        """Verifies that watchdog thread remains alive after trigger and reset resumes monitoring."""
        simulated_pos = [500.0, 500.0]
        wd = FailsafeWatchdog(
            position_getter=lambda: (simulated_pos[0], simulated_pos[1]),
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
            trap_signals=False,
        )
        try:
            wd.start()
            # Trigger corner
            simulated_pos[0], simulated_pos[1] = 0.0, 0.0
            # Wait for trigger
            deadline = time.time() + 0.5
            while not wd.is_triggered and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(wd.is_triggered)

            # Allow thread loop to persist in wait-state
            time.sleep(0.05)
            self.assertTrue(wd.is_running, "Watchdog thread must remain alive after trigger")

            # Reset failsafe to resume automation
            simulated_pos[0], simulated_pos[1] = 500.0, 500.0
            wd.reset()
            self.assertFalse(wd.is_triggered)
            self.assertTrue(wd.is_running, "Watchdog thread must remain alive after reset()")

            # Second corner intrusion
            simulated_pos[0], simulated_pos[1] = 0.0, 0.0
            deadline = time.time() + 0.5
            while not wd.is_triggered and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(wd.is_triggered, "Watchdog must detect second corner intrusion after reset")
        finally:
            wd.stop()

    test_watchdog_thread_dies_on_trigger_rendering_reset_ineffective = test_failsafe_watchdog_thread_death_on_trigger

    def test_corner_detection_clamping_discrepancy_on_extreme_overshoot(self) -> None:
        """Verifies that check_corner detects extreme negative coordinate overshoot as top-left corner."""
        from src.actuators.types import is_screen_corner

        wd = FailsafeWatchdog(
            margin=5.0,
            bounds_getter=lambda: [(0.0, 0.0, 1920.0, 1080.0)],
        )
        self.assertTrue(is_screen_corner(-60.0, -60.0, 1920.0, 1080.0, 5.0))
        match = wd.check_corner(-60.0, -60.0)
        self.assertIsNotNone(match)
        self.assertEqual(match[0], "top-left")


if __name__ == "__main__":
    unittest.main()


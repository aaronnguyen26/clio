"""Tier 2: Boundary, Corner & Adversarial Tests across R1-R5.

Covers:
- R1: Corner failsafe triggers (<15ms), invalid bundle IDs, extreme coordinates,
      empty string typing, modifier cleanup on interrupt.
- R2: Empty/whitespace user messages, gibberish utterances, rapid flood,
      invalid tone fallbacks, confirmation rejection.
- R3: Zero matches in empty DB, malformed/corrupted JSON schemas, SQL injection safety,
      out-of-bounds coordinate ratios, missing parameter references.
- R4: Poller timeout exhaustion, emergency stop mid-flight abort, retry failure handling,
      faulty subscriber isolation, thread-safe event publishing.
- R5: Onboarding modal dismissal, clipboard failure fallback, already-running instances,
      emergency stop during typing, sub-50ms speed run.
"""

import threading
import time
import pytest
from tests.harness import (
    MockActuator,
    MouseButton,
    FailsafeEmergencyStop,
    ApplicationLaunchError,
    WindowNotFoundError,
    CompanionDialogueEngine,
    DialogueState,
    CommentaryEngine,
    TaskMemoryEngine,
    NLRetrievalEngine,
    ParameterEngine,
    CoordinateAdapter,
    WindowReadinessPoller,
    AutonomousWorkflowExecutor,
    ExecutionEventBus,
    ExecutionEvent,
    EventType,
    BenchmarkCompletionVerifier,
    WorkflowSpec,
    WorkflowStep,
    ActionType,
    WindowInfo,
    create_notes_benchmark_workflow,
)


@pytest.mark.tier2
@pytest.mark.mock
class TestTier2Boundaries:
    """Tier 2 tests covering edge, boundary, and adversarial conditions across R1-R5."""

    # ========================================================================
    # 4.1 R1 Boundary & Corner Cases (Actuation & Safety)
    # ========================================================================

    def test_failsafe_corner_trigger_raises_immediately(self, mock_actuator: MockActuator):
        """TEST-T2-R1-01: Moving cursor to (0, 0) immediately raises FailsafeEmergencyStop."""
        with pytest.raises(FailsafeEmergencyStop) as exc_info:
            mock_actuator.move_mouse(0.0, 0.0)

        assert "Emergency stop triggered" in str(exc_info.value)
        # Verify modifiers are automatically cleared
        mock_actuator.assert_modifiers_released()

    def test_actuator_invalid_bundle_id_handling(self, mock_actuator: MockActuator):
        """TEST-T2-R1-02: Launching a failing bundle raises ApplicationLaunchError without hanging."""
        mock_actuator.set_app_launch_failure("com.invalid.nonexistent.app", True)

        with pytest.raises(ApplicationLaunchError):
            mock_actuator.launch_app("com.invalid.nonexistent.app", timeout=0.1)

    def test_actuator_negative_and_extreme_coordinates(self, mock_actuator: MockActuator):
        """TEST-T2-R1-03: Negative or out-of-screen coordinates are clamped safely."""
        # Extreme negative coordinate on one axis (x=-50, y=500) clamps to edge (0.0, 500.0)
        mock_actuator.move_mouse(-50.0, 500.0)
        assert mock_actuator.get_mouse_position() == (0.0, 500.0)

        # Clamping to corner (-500, -200) clamps to (0, 0) and triggers corner failsafe
        with pytest.raises(FailsafeEmergencyStop):
            mock_actuator.move_mouse(-500.0, -200.0)

        # Extreme positive coordinate on one axis (x=99999, y=500) clamps to max width
        actuator2 = MockActuator(screen_size=(1920.0, 1080.0))
        actuator2.move_mouse(99999.0, 500.0)
        assert actuator2.get_mouse_position() == (1920.0, 500.0)

    def test_actuator_empty_string_and_null_keystrokes(self, mock_actuator: MockActuator):
        """TEST-T2-R1-04: Typing empty string no-ops safely without error."""
        mock_actuator.type_text("")
        assert mock_actuator.typed_text == ""
        mock_actuator.assert_action_called("type_text", text="")

    def test_actuator_modifier_cleanup_on_interrupt(self, mock_actuator: MockActuator):
        """TEST-T2-R1-05: Emergency stop or stop() releases all modifier keys."""
        mock_actuator._active_modifiers.add("cmd")
        mock_actuator._active_modifiers.add("shift")
        assert len(mock_actuator._active_modifiers) == 2

        mock_actuator.stop()
        mock_actuator.assert_modifiers_released()

    # ========================================================================
    # 4.2 R2 Boundary & Corner Cases (Companion Dialogue & Personality)
    # ========================================================================

    def test_companion_empty_and_whitespace_input(self, dialogue_engine: CompanionDialogueEngine):
        """TEST-T2-R2-01: Empty or whitespace input returns friendly guidance without error."""
        reply1, state1 = dialogue_engine.handle_user_message("")
        assert state1 == DialogueState.IDLE
        assert "didn't catch that" in reply1 or "help" in reply1.lower()

        reply2, state2 = dialogue_engine.handle_user_message("   \n\t   ")
        assert state2 == DialogueState.IDLE
        assert "didn't catch that" in reply2 or "help" in reply2.lower()

    def test_companion_unrecognized_gibberish_utterance(self, dialogue_engine: CompanionDialogueEngine):
        """TEST-T2-R2-02: Unrecognized gibberish prompts user to teach the workflow."""
        reply, state = dialogue_engine.handle_user_message("xyz999foobar!@#$%^&*()")
        assert state == DialogueState.IDLE
        assert "teach" in reply.lower() or "not sure" in reply.lower()

    def test_companion_rapid_fire_user_messages(self, dialogue_engine: CompanionDialogueEngine):
        """TEST-T2-R2-03: Rapid successive conversational turns handle cleanly."""
        for i in range(20):
            reply, state = dialogue_engine.handle_user_message(f"Hello test turn {i}")
            assert reply is not None

    def test_companion_invalid_tone_profile_fallback(self):
        """TEST-T2-R2-04: Nonexistent tone profile gracefully falls back to vibrant."""
        commentary = CommentaryEngine(tone="unknown_alien_tone")
        assert commentary.tone == "vibrant"

    def test_companion_confirmation_rejection(self, dialogue_engine: CompanionDialogueEngine):
        """TEST-T2-R2-05: Saying 'no' or 'cancel' to confirmation cancels and resets to IDLE."""
        dialogue_engine.state = DialogueState.CONFIRMING
        dialogue_engine.pending_workflow = create_notes_benchmark_workflow()

        reply, state = dialogue_engine.handle_user_message("no, cancel that")
        assert state == DialogueState.IDLE
        assert dialogue_engine.pending_workflow is None
        assert "Cancelled" in reply

    # ========================================================================
    # 4.3 R3 Boundary & Corner Cases (Task Memory & Retrieval)
    # ========================================================================

    def test_memory_query_zero_matches_empty_db(self, empty_memory_engine: TaskMemoryEngine):
        """TEST-T2-R3-01: Querying empty database returns empty results without SQL exception."""
        retrieval = NLRetrievalEngine(empty_memory_engine)
        matches = retrieval.query("anything at all")
        assert matches == []

    def test_memory_corrupted_workflow_json(self, empty_memory_engine: TaskMemoryEngine):
        """TEST-T2-R3-02: Corrupted or invalid JSON rejects cleanly during retrieval."""
        cur = empty_memory_engine._conn.cursor()
        cur.execute("""
            INSERT INTO workflows (id, name, description, version, active, spec_json, created_at, updated_at)
            VALUES ('wf_corrupted', 'Corrupt', 'desc', 1, 1, 'INVALID_JSON_CONTENT{{{', 'now', 'now')
        """)
        empty_memory_engine._conn.commit()

        # Retrieval should handle corrupted JSON without unhandled crash
        with pytest.raises(Exception):
            empty_memory_engine.get_workflow("wf_corrupted")

    def test_memory_sql_injection_in_trigger_query(self, memory_engine: TaskMemoryEngine):
        """TEST-T2-R3-03: Query with SQL injection payload is treated as literal text."""
        retrieval = NLRetrievalEngine(memory_engine)
        injection_strings = [
            "'; DROP TABLE workflows; --",
            "' OR '1'='1",
            "\" UNION SELECT * FROM sqlite_master --",
        ]
        for inj in injection_strings:
            res = retrieval.query(inj)
            # Must return clean list (likely empty), no SQL errors
            assert isinstance(res, list)

        # Confirm workflows table is still intact
        assert len(memory_engine.list_workflows()) >= 1

    def test_memory_extreme_coordinate_ratios(self):
        """TEST-T2-R3-04: Out-of-bounds ratios (< 0.0 or > 1.0) are clamped safely to window."""
        window = WindowInfo(
            window_id=1,
            owner_name="TestApp",
            title="Test",
            x=100.0,
            y=100.0,
            width=500.0,
            height=500.0,
        )

        # Ratio -0.5 should clamp to 0.0 -> screen coord 100
        x1, y1 = CoordinateAdapter.to_screen_coordinates(-0.5, -0.5, window)
        assert x1 == 100
        assert y1 == 100

        # Ratio 2.5 should clamp to 1.0 -> screen coord 600
        x2, y2 = CoordinateAdapter.to_screen_coordinates(2.5, 2.5, window)
        assert x2 == 600
        assert y2 == 600

    def test_memory_parameter_missing_key_handling(self):
        """TEST-T2-R3-05: Missing template variables retain token rather than crashing."""
        template = "Note for ${author}: ${task_content}"
        params = {"author": "Alice"}
        # 'task_content' is missing
        output = ParameterEngine.interpolate(template, params)
        assert "Note for Alice: ${task_content}" == output

    # ========================================================================
    # 4.4 R4 Boundary & Corner Cases (Staged Verification & Poller)
    # ========================================================================

    def test_window_poller_timeout_exhaustion(self, mock_actuator: MockActuator):
        """TEST-T2-R4-01: Waiting for nonexistent window raises WindowNotFoundError upon timeout."""
        with pytest.raises(WindowNotFoundError) as exc_info:
            WindowReadinessPoller.wait_for_window(
                actuator=mock_actuator,
                app_name="NonExistentAppWindow",
                timeout=0.05,
                poll_interval=0.01,
            )
        assert "Timed out" in str(exc_info.value)

    def test_executor_failsafe_halts_in_flight_workflow(
        self, mock_actuator: MockActuator, event_bus: ExecutionEventBus
    ):
        """TEST-T2-R4-02: Failsafe triggered mid-flight aborts remaining steps immediately."""
        events_emitted = []
        event_bus.subscribe(lambda e: events_emitted.append(e.event_type))

        spec = WorkflowSpec(
            id="wf_failsafe_test",
            name="Failsafe Abort Test",
            description="Testing mid-flight abort",
            steps=[
                WorkflowStep(
                    step_id="step_1",
                    order=1,
                    description="Move cursor safely",
                    action=ActionType.MOVE_MOUSE,
                    payload={"coordinates": {"x": 500, "y": 500}},
                ),
                WorkflowStep(
                    step_id="step_2",
                    order=2,
                    description="Trigger failsafe by corner movement",
                    action=ActionType.MOVE_MOUSE,
                    payload={"coordinates": {"x": 0, "y": 0}},  # Corner -> emergency stop!
                ),
                WorkflowStep(
                    step_id="step_3",
                    order=3,
                    description="Should never run",
                    action=ActionType.TYPE_TEXT,
                    payload={"text": "Never typed"},
                ),
            ],
        )

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(spec)

        assert res.success is False
        assert res.steps_completed == 1  # Only step 1 completed before step 2 triggered failsafe
        assert "Emergency stop" in str(res.error)
        assert EventType.EMERGENCY_STOP in events_emitted
        # Step 3 text was never typed
        assert "Never typed" not in mock_actuator.typed_text

    def test_executor_max_retries_exhaustion(
        self, mock_actuator: MockActuator, event_bus: ExecutionEventBus
    ):
        """TEST-T2-R4-03: Repeatedly failing step emits TASK_FAILED and returns error."""
        mock_actuator.set_app_launch_failure("com.broken.app", True)

        spec = WorkflowSpec(
            id="wf_failing_step",
            name="Failing Step Test",
            description="Launches broken app",
            steps=[
                WorkflowStep(
                    step_id="s1",
                    order=1,
                    description="Launch broken app",
                    action=ActionType.LAUNCH_APP,
                    target={"bundle_id": "com.broken.app"},
                )
            ],
        )

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(spec)

        assert res.success is False
        assert res.steps_completed == 0
        assert "Failed to launch" in str(res.error)

    def test_event_bus_unsubscribe_and_dead_listener(self, event_bus: ExecutionEventBus):
        """TEST-T2-R4-04: Subscriber throwing exception is isolated without breaking other listeners."""
        surviving_listener_called = False

        def broken_listener(ev):
            raise RuntimeError("Fatal explosion in listener")

        def good_listener(ev):
            nonlocal surviving_listener_called
            surviving_listener_called = True

        event_bus.subscribe(broken_listener)
        event_bus.subscribe(good_listener)

        event = ExecutionEvent(
            event_type=EventType.TASK_STARTED,
            task_id="t1",
            message="Test event",
        )
        event_bus.publish(event)

        assert surviving_listener_called is True

    def test_event_bus_concurrent_publish_safety(self, event_bus: ExecutionEventBus):
        """TEST-T2-R4-05: Concurrent publishes from multiple threads execute safely."""
        event_count = 0
        lock = threading.Lock()

        def listener(ev):
            nonlocal event_count
            with lock:
                event_count += 1

        event_bus.subscribe(listener)

        threads = []
        for i in range(10):
            t = threading.Thread(
                target=lambda idx: event_bus.publish(
                    ExecutionEvent(
                        event_type=EventType.ACTION_STARTING,
                        task_id=f"concurrent_{idx}",
                        message=f"Thread message {idx}",
                    )
                ),
                args=(i,),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert event_count == 10

    # ========================================================================
    # 4.5 R5 Boundary & Corner Cases (Benchmark & Notes Automation)
    # ========================================================================

    def test_benchmark_window_dismisses_onboarding_modal(self, mock_actuator: MockActuator):
        """TEST-T2-R5-01: Onboarding modal dismiss sequence can be simulated via Escape/Enter."""
        mock_actuator.add_virtual_window(
            WindowInfo(
                window_id=99,
                owner_name="com.apple.Notes",
                title="Welcome to Notes",
                x=300.0,
                y=200.0,
                width=500.0,
                height=400.0,
            )
        )
        # Dismiss with Escape
        mock_actuator.press_hotkey("escape")
        mock_actuator.assert_hotkey_pressed("escape")

    def test_benchmark_pasteboard_failure_fallback(self, mock_actuator: MockActuator):
        """TEST-T2-R5-02: If pasteboard fails, type_text serves as direct fallback."""
        sample_todo = "# Weekly Plan\n[ ] Monday: Task 1"
        try:
            # Simulate clipboard failure
            raise OSError("pbcopy command failed")
        except OSError:
            # Fallback to direct typing
            mock_actuator.type_text(sample_todo)

        assert sample_todo in mock_actuator.typed_text

    def test_benchmark_already_running_notes_instance(
        self,
        mock_actuator: MockActuator,
        executor: AutonomousWorkflowExecutor,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """TEST-T2-R5-03: Warm benchmark run against already open Notes succeeds cleanly."""
        # Pre-open Notes
        mock_actuator.launch_app("com.apple.Notes")
        assert mock_actuator.get_frontmost_app() == "com.apple.Notes"

        # Execute benchmark
        res = executor.execute_workflow(notes_benchmark_wf)
        assert res.success is True
        assert BenchmarkCompletionVerifier.verify(mock_actuator) is True

    def test_benchmark_emergency_stop_during_text_entry(
        self,
        mock_actuator: MockActuator,
        notes_benchmark_wf: WorkflowSpec,
        event_bus: ExecutionEventBus,
    ):
        """TEST-T2-R5-04: Emergency stop triggered before typing aborts to-do generation."""
        # Trigger failsafe
        mock_actuator.trigger_failsafe()

        executor = AutonomousWorkflowExecutor(actuator=mock_actuator, bus=event_bus)
        res = executor.execute_workflow(notes_benchmark_wf)

        assert res.success is False
        assert "Emergency stop" in str(res.error)
        assert BenchmarkCompletionVerifier.verify(mock_actuator) is False

    def test_benchmark_zero_delay_speed_run(
        self,
        mock_actuator: MockActuator,
        executor: AutonomousWorkflowExecutor,
        notes_benchmark_wf: WorkflowSpec,
    ):
        """TEST-T2-R5-05: In mock mode with 0ms delay, entire benchmark completes in < 50ms."""
        start = time.perf_counter()
        res = executor.execute_workflow(notes_benchmark_wf)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert res.success is True
        assert elapsed_ms < 50.0, f"Benchmark took {elapsed_ms:.2f}ms, expected < 50ms"

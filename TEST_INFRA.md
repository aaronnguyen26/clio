# Autonomous Desktop Companion — End-to-End Test Infrastructure Specification

**Document Version**: 1.0.0  
**Status**: APPROVED  
**Author**: Test Engineering Subsystem (`teamwork_preview_test_writer`)  
**Project**: Task Memory Automator (Autonomous Desktop Companion)  
**Requirements Addressed**: R1, R2, R3, R4, R5  

---

## 1. Testing Philosophy & Guiding Principles

### 1.1 Requirement-Driven & Opaque-Box Methodology
The Autonomous Desktop Companion operates at the boundary between human intent and native OS desktop environments. Testing this system requires strict adherence to:
1. **Opaque-Box Verification**: Tests validate observable external behaviors, state changes, emitted events, and OS actuation contracts without coupling to internal private implementations.
2. **Deterministic Predictability**: OS automation can inherently introduce non-determinism (timing jitter, window focus lag, display sleep). The test architecture isolates timing dependencies using synthetic event buses and deterministic mock actuators for CI, while preserving high-fidelity execution paths for live verification.
3. **Progressive Testability (R4)**: Each subsystem is independently verifiable in isolation. Tests for higher-level features degrade gracefully or leverage mock contracts when lower-level native OS facilities are executing in headless environments.
4. **Safety as a First-Class Invariant**: Every actuation sequence must strictly guarantee failsafe responsiveness (screen corner detection) and modifier state cleanup (preventing sticky keys).

---

## 2. 4-Tier Test Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Tier 4: Real-World Application Scenarios             │
│   - Hands-Free Apple Notes To-Do Benchmark (Cold & Warm)               │
│   - Emergency Corner Failsafe Interruption During Active Workflows     │
│   - End-to-End Conversational Teach -> Replay Flow                    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│                Tier 3: Cross-Feature Combinations (Pairwise)           │
│   - R1 (Actuators) + R2 (Companion Commentary Bus)                     │
│   - R1 (Actuators) + R3 (Workflow Memory & Normalized Coordinates)     │
│   - R2 (Companion) + R3 (Conversational Task Query & Disambiguation)   │
│   - R1 (Actuators) + R5 (Benchmark Execution & Failsafe Watchdog)      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│                 Tier 2: Boundary, Corner & Adversarial Cases           │
│   - Out-of-bounds coordinates, empty strings, missing bundle IDs       │
│   - Emergency corner cursor abort (< 15ms latency)                    │
│   - Unrecognized intents, corrupted JSON, database recovery            │
│   - Sticky modifier recovery, rapid multi-turn conversational flood   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────┴────────────────────────────────────┐
│                    Tier 1: Feature Coverage (R1 - R5)                  │
│   - Unit and behavioral validation (>= 5 cases per feature)            │
│   - Deterministic execution in < 100ms per test case                  │
│   - 100% runnable in headless CI environments                          │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Tier 1: Feature Coverage Specifications (>= 5 Cases per Feature)

### 3.1 Feature R1: Generic Zero-Wiring OS Control & App Launching
*Target Interface*: `BaseActuator`, `MacOSActuator`, `MockActuator`, `ActuatorFactory`

| Test ID | Test Name | Target Behavior | Expected Output / Invariant |
|---|---|---|---|
| `TEST-T1-R1-01` | `test_actuator_app_launch_records_state` | Launching application by bundle ID | `launch_app("com.apple.Notes")` returns `True`; frontmost app updated to target; history records `launch_app`. |
| `TEST-T1-R1-02` | `test_actuator_focus_app` | Bringing background app to foreground | `focus_app("com.apple.Notes")` returns `True`; frontmost app switches to `com.apple.Notes`. |
| `TEST-T1-R1-03` | `test_actuator_mouse_move_and_coordinates` | Mouse movement synthesis | `move_mouse(350, 420)` updates cursor position to `(350.0, 420.0)`; audit log records movement. |
| `TEST-T1-R1-04` | `test_actuator_click_synthesis` | Primary and secondary mouse clicks | `click(100, 200, button=LEFT, click_count=1)` and `click(button=RIGHT)` log click events with target coordinates. |
| `TEST-T1-R1-05` | `test_actuator_keyboard_text_typing` | Typing text without key stickiness | `type_text("Autonomous Companion")` logs text typing; virtual buffer contains string. |
| `TEST-T1-R1-06` | `test_actuator_hotkey_combination` | Synthesizing shortcut combinations | `press_hotkey("cmd", "n")` logs sequence `cmd+n`; releases all modifiers cleanly. |
| `TEST-T1-R1-07` | `test_actuator_pasteboard_injection` | High-speed multi-line text paste | `paste_text("Line 1\nLine 2")` stores text into clipboard buffer and fires `cmd+v`. |

### 3.2 Feature R2: Conversational Companion with Personality
*Target Interface*: `CompanionDialogueEngine`, `DialogueStateMachine`, `ToneProfiles`, `CommentaryEngine`

| Test ID | Test Name | Target Behavior | Expected Output / Invariant |
|---|---|---|---|
| `TEST-T1-R2-01` | `test_companion_greeting_intent` | Processing casual greetings | Input "Hello Clio" transitions state machine to `IDLE`; response contains greeting and capabilities. |
| `TEST-T1-R2-02` | `test_companion_tone_profile_switching` | Switching commentary tone | Selecting `vibrant`, `concise`, `zen`, or `developer` outputs tone-specific phrasing. |
| `TEST-T1-R2-03` | `test_companion_intent_direct_execution` | Direct actionable workflow trigger | Input "write my weekly to-do list" resolves to workflow `wf_notes_weekly_todo` with confidence > 0.85. |
| `TEST-T1-R2-04` | `test_companion_commentary_throttling` | 800ms debounce/throttle on events | Publishing 10 rapid `ACTION_STARTING` events results in throttled output without UI freezing. |
| `TEST-T1-R2-05` | `test_companion_conversational_task_listing` | User asking "what tasks do you remember?" | Returns list/summary of all active workflows in memory. |
| `TEST-T1-R2-06` | `test_companion_clarification_on_ambiguity` | Ambiguous trigger handling | Utterance matching multiple workflows triggers `CLARIFYING` state with options. |

### 3.3 Feature R3: Teach-Mode Workflow Memory & Retrieval
*Target Interface*: `TaskMemoryEngine`, `NLRetrievalEngine`, `ParameterEngine`, `CoordinateAdapter`

| Test ID | Test Name | Target Behavior | Expected Output / Invariant |
|---|---|---|---|
| `TEST-T1-R3-01` | `test_memory_save_and_retrieve_workflow` | Persisting workflow spec to database | `save_workflow(spec)` generates unique ID; `get_workflow(id)` returns identical schema. |
| `TEST-T1-R3-02` | `test_memory_version_history_increment` | Updating existing workflow | Saving updated workflow increments version number (e.g. v1 -> v2) and creates audit log. |
| `TEST-T1-R3-03` | `test_memory_nl_exact_and_alias_match` | NL query matching canonical/alias triggers | Querying "plan my week" matches `wf_notes_weekly_todo` with high confidence. |
| `TEST-T1-R3-04` | `test_memory_parameter_interpolation` | Dynamic token replacement in step payloads | Template `"Note: ${doc_title} (${CURRENT_DATE})"` interpolates runtime variables correctly. |
| `TEST-T1-R3-05` | `test_memory_coordinate_ratio_projection` | Normalized ratio coordinates to screen pixels | Window `(200, 100, 800, 600)` with `(norm_x=0.5, norm_y=0.5)` yields screen coords `(600, 400)`. |
| `TEST-T1-R3-06` | `test_memory_execution_telemetry_logging` | Recording execution metrics and outcomes | `record_execution()` writes execution duration, step count, and success status to DB. |

### 3.4 Feature R4: Staged Verification & Progressive Test Suite
*Target Interface*: `ActuatorFactory`, `WindowReadinessPoller`, `AutonomousWorkflowExecutor`, `MockHarness`

| Test ID | Test Name | Target Behavior | Expected Output / Invariant |
|---|---|---|---|
| `TEST-T1-R4-01` | `test_actuator_factory_mock_in_ci` | Factory detects test/CI environment | `ActuatorFactory.create(mode="mock")` returns `MockActuator` instance without touching display. |
| `TEST-T1-R4-02` | `test_window_readiness_poller_mock_success` | Dynamic state poller waiting for window | Poller resolves immediately when window is present; does not rely on brittle sleeps. |
| `TEST-T1-R4-03` | `test_executor_executes_multi_step_workflow` | Step-by-step workflow sequencer | Executor runs 5-step workflow sequentially, validating step completion events. |
| `TEST-T1-R4-04` | `test_execution_event_bus_pub_sub` | Decoupled event bus messaging | Subscribers receive typed `ExecutionEvent` instances with accurate monotonic timestamps. |
| `TEST-T1-R4-05` | `test_executor_step_retry_recovery` | Transient step failure retry | Step configured with `retry` executes fallback or succeeds on subsequent attempt. |

### 3.5 Feature R5: End-to-End Autonomous Benchmark Task
*Target Interface*: `NotesBenchmarkRecipe`, `BenchmarkRunner`, `BenchmarkCompletionVerifier`

| Test ID | Test Name | Target Behavior | Expected Output / Invariant |
|---|---|---|---|
| `TEST-T1-R5-01` | `test_benchmark_recipe_step_structure` | Structure of 5-phase Notes recipe | Recipe specifies phases: Launch, Focus, Window Settle, New Note (`Cmd+N`), Type/Paste Content. |
| `TEST-T1-R5-02` | `test_benchmark_mock_execution_completes` | Running benchmark through mock actuator | All 5 phases execute cleanly; mock actuator records app launch, hotkey, and text insertion. |
| `TEST-T1-R5-03` | `test_benchmark_todo_content_verification` | Content formatting of weekly to-do list | Verifies output contains checklist markers `[ ]`, Monday–Friday headers, and footer. |
| `TEST-T1-R5-04` | `test_benchmark_completion_verifier_logic` | Verifier confirming successful note generation | Verifier returns `is_successful=True` when target bundle is active and content injected. |
| `TEST-T1-R5-05` | `test_benchmark_telemetry_event_stream` | Lifecycle events emitted during benchmark | Event bus captures `TASK_STARTED`, `APP_LAUNCH_INIT`, `ACTION_STARTING`, `TASK_COMPLETED`. |

---

## 4. Tier 2: Boundary, Corner & Adversarial Cases (>= 5 per Feature)

### 4.1 R1 Boundary & Corner Cases (Actuation & Safety)
| Test ID | Test Name | Edge Condition | Expected Outcome |
|---|---|---|---|
| `TEST-T2-R1-01` | `test_failsafe_corner_trigger_raises_immediately` | Cursor moves to `(0, 0)` or screen corner | `check_failsafe()` raises `FailsafeEmergencyStop`; execution halts in < 15ms. |
| `TEST-T2-R1-02` | `test_actuator_invalid_bundle_id_handling` | Launching nonexistent bundle `com.invalid.nonexistent.app` | Returns `False` or raises `ApplicationLaunchError`; does not hang. |
| `TEST-T2-R1-03` | `test_actuator_negative_and_extreme_coordinates` | Coordinates `(-50, -50)` or `(99999, 99999)` | Clamped to screen boundary or safely logged without crashing display server. |
| `TEST-T2-R1-04` | `test_actuator_empty_string_and_null_keystrokes` | Typing empty string `""` or special Unicode glyphs | Safely no-ops for empty string; synthesizes glyphs without unhandled encoding exception. |
| `TEST-T2-R1-05` | `test_actuator_modifier_cleanup_on_interrupt` | Process halted while `Cmd` key is down | `stop()` executes cleanup routine ensuring all modifier keys are explicitly released. |

### 4.2 R2 Boundary & Corner Cases (Companion Dialogue & Personality)
| Test ID | Test Name | Edge Condition | Expected Outcome |
|---|---|---|---|
| `TEST-T2-R2-01` | `test_companion_empty_and_whitespace_input` | User sends `""`, `"   "`, or newlines | Companion prompts gracefully for input without throwing empty string error. |
| `TEST-T2-R2-02` | `test_companion_unrecognized_gibberish_utterance` | User input `"asdfqwerty12345!@#"` | Companion responds helpfully in current tone, stating it didn't recognize any workflow. |
| `TEST-T2-R2-03` | `test_companion_rapid_fire_user_messages` | Sending 50 messages in 100ms | Dialogue state machine queues or processes turns sequentially without race conditions. |
| `TEST-T2-R2-04` | `test_companion_invalid_tone_profile_fallback` | Initializing with nonexistent tone `"sarcastic_robot"` | Falls back to default tone (`"vibrant"`) with warning log. |
| `TEST-T2-R2-05` | `test_companion_confirmation_rejection` | User answers "no" / "cancel" to destructive confirmation | Aborts workflow; transitions cleanly back to `IDLE` state. |

### 4.3 R3 Boundary & Corner Cases (Task Memory & Retrieval)
| Test ID | Test Name | Edge Condition | Expected Outcome |
|---|---|---|---|
| `TEST-T2-R3-01` | `test_memory_query_zero_matches_empty_db` | Querying database when no workflows exist | Returns empty match list `[]` with confidence 0.0 without SQL error. |
| `TEST-T2-R3-02` | `test_memory_corrupted_workflow_json` | Ingesting malformed JSON schema | Rejects document with validation error; preserves database integrity. |
| `TEST-T2-R3-03` | `test_memory_sql_injection_in_trigger_query` | Query with `' OR '1'='1` or SQL meta-characters | FTS5 parameterized query safely handles string as literal query without syntax error. |
| `TEST-T2-R3-04` | `test_memory_extreme_coordinate_ratios` | Ratio `norm_x = 1.5`, `norm_y = -0.2` | Clamps ratio to `[0.0, 1.0]` or raises coordinate out-of-bounds error. |
| `TEST-T2-R3-05` | `test_memory_parameter_missing_key_handling` | Template references `${missing_var}` | Retains placeholder or raises clear `MissingParameterError` rather than crashing. |

### 4.4 R4 Boundary & Corner Cases (Staged Verification & Poller)
| Test ID | Test Name | Edge Condition | Expected Outcome |
|---|---|---|---|
| `TEST-T2-R4-01` | `test_window_poller_timeout_exhaustion` | Waiting for window that never appears | Poller raises `WindowNotFoundError` cleanly after configured timeout expires. |
| `TEST-T2-R4-02` | `test_executor_failsafe_halts_in_flight_workflow` | Emergency stop triggered during step 3 of 5 | Steps 4 and 5 are cancelled immediately; event bus emits `EMERGENCY_STOP`. |
| `TEST-T2-R4-03` | `test_executor_max_retries_exhaustion` | Step fails repeatedly beyond retry limit | Step terminates; emits `TASK_FAILED`; returns failed `ExecutionResult`. |
| `TEST-T2-R4-04` | `test_event_bus_unsubscribe_and_dead_listener` | Subscriber raises unhandled exception | Event bus isolates error, logs warning, and continues notifying remaining subscribers. |
| `TEST-T2-R4-05` | `test_event_bus_concurrent_publish_safety` | Multiple threads publishing events simultaneously | Thread-safe queue delivery without lost events or corrupted event sequences. |

### 4.5 R5 Boundary & Corner Cases (Benchmark & Notes Automation)
| Test ID | Test Name | Edge Condition | Expected Outcome |
|---|---|---|---|
| `TEST-T2-R5-01` | `test_benchmark_window_dismisses_onboarding_modal` | Window title matches "Welcome to Notes" | Synthesizes `Enter` or `Esc` to dismiss modal before proceeding to note creation. |
| `TEST-T2-R5-02` | `test_benchmark_pasteboard_failure_fallback` | Clipboard command fails or is unavailable | Falls back to character-by-character typing synthesis (`type_text`). |
| `TEST-T2-R5-03` | `test_benchmark_already_running_notes_instance` | Apple Notes is already running before benchmark | Activates existing instance without launching duplicate redundant processes. |
| `TEST-T2-R5-04` | `test_benchmark_emergency_stop_during_text_entry` | Failsafe triggered while writing to-do items | Halts text generation instantly; releases modifier keys; reports abort status. |
| `TEST-T2-R5-05` | `test_benchmark_zero_delay_speed_run` | Running benchmark in mock mode with 0ms delay | Executes entire 5-phase sequence in < 50ms total test duration. |

---

## 5. Tier 3: Cross-Feature Combinations (Pairwise Interactions)

1. **R1 (Actuators) + R2 (Companion Personality)**:
   - *Interaction*: As the actuator progresses through launching an app, creating a note, and typing, the companion commentary engine subscribes to the actuator event stream and synthesizes contextual remarks in real time matching the chosen tone profile.
2. **R1 (Actuators) + R3 (Workflow Memory)**:
   - *Interaction*: Autonomous workflow executor reads a recorded `WorkflowSpec` from the task memory engine, parses step parameters and normalized ratio coordinates, translates them to screen coordinates via `CoordinateAdapter`, and invokes the actuator.
3. **R2 (Companion) + R3 (Conversational Task Memory)**:
   - *Interaction*: User talks to the companion asking to run or edit a task. The companion routes the natural language utterance through `NLRetrievalEngine`, identifies candidates, prompts for clarification if ambiguous, and confirms before execution.
4. **R1 (Actuators) + R5 (Benchmark & Safety Watchdog)**:
   - *Interaction*: Notes benchmark recipe runs via actuator; when a failsafe event is simulated, the actuator halts, the watchdog releases modifier keys, and the benchmark returns an aborted status code.
5. **R2 (Companion) + R4 (Progressive Testability)**:
   - *Interaction*: Dialogue engine and tone synthesis run completely headless under `MockActuator` in CI, verifying that all conversational states function without live OS audio/display dependencies.

---

## 6. Tier 4: Real-World Application Scenarios

1. **Scenario 1 — Cold-Start Hands-Free Apple Notes Benchmark (R5 Primary)**:
   - Pre-condition: Apple Notes is closed.
   - Flow: System launches `com.apple.Notes`, waits for main window readiness, triggers `Cmd+N`, pastes weekly to-do list, verifies window state, outputs lively completion celebration.
2. **Scenario 2 — Warm-Start Notes Benchmark**:
   - Pre-condition: Apple Notes is already running with an active document.
   - Flow: System detects existing running process, focuses the application, creates a new note without creating redundant windows, and enters to-do items.
3. **Scenario 3 — Emergency Human Interruption (Safety Invariant)**:
   - Flow: During text typing of the weekly to-do list, the human user jerks the mouse into the top-left screen corner `(0, 0)`.
   - Verification: System aborts in < 15ms, releases modifier keys, ceases all synthetic input, and emits `EMERGENCY_STOP` with companion apologetic alert.
4. **Scenario 4 — Natural Language Conversational Task Delegation**:
   - Flow: User enters CLI REPL and types: *"Hey Clio, please create my weekly to-do list in Notes"*.
   - Verification: Companion parses intent, retrieves `wf_notes_weekly_todo`, provides vibrant commentary, executes recipe, and confirms completion back to user.
5. **Scenario 5 — Full Teach-Mode Demonstration & Replay**:
   - Flow: User initiates teach mode for a multi-step desktop task; recorder captures events, coalesces noise, clamps pauses, projects window-relative ratios, saves to SQLite with version 1, and verifies immediate retrieval.

---

## 7. Test Execution & Reporting Protocol

### 7.1 Test Invocation Commands
```bash
# 1. Run all Tier 1 and Tier 2 tests (Mock mode, fast headless execution)
python3 -m pytest tests/ -m "tier1 or tier2" -v

# 2. Run standalone zero-dependency test runner
python3 run_tests.py --tier 1,2

# 3. Run complete test suite including cross-feature integration
python3 -m pytest tests/ -v

# 4. Run live macOS desktop benchmark (interactive display required)
python3 -m pytest tests/live/ -m live_desktop -v -s
```

### 7.2 Pass/Fail Criteria
- **Tier 1 & Tier 2 Pass Threshold**: 100% pass rate in headless environment. Zero flaky tests. Total execution time < 5.0 seconds for unit suite.
- **Modifier Cleanup Invariant**: In all test terminations (pass, fail, abort), synthetic modifier key state must be verified clean (`Cmd`, `Shift`, `Alt`, `Ctrl` released).
- **Audit Log Integrity**: Every actuation test must inspect `MockActuator.history` to confirm exact ordering and parameters of synthetic events.

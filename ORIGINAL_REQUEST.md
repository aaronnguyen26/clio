# Original User Request

## Initial Request — 2026-09-22T23:33:35Z

An autonomous desktop companion app with an engaging personality that observes and remembers user tasks, operating the computer hands-free across native applications (mouse, keyboard, app launching) without requiring app-specific API wiring or pre-configuration.

Working directory: ~/teamwork_projects/task_memory_automator
Integrity mode: development

## Requirements

### R1. Generic Zero-Wiring OS Control & App Launching
Autonomously interact with any application on the operating system without requiring custom integrations, API keys, or pre-wired plugins. Control native app launching, window focusing, mouse clicks/movements, and keyboard entry directly at the OS level.

### R2. Conversational Companion with Personality
Provide an interactive companion persona that communicates with the user in natural language, confirms intentions, provides lively commentary during task execution, and manages task memory through conversation.

### R3. Teach-Mode Workflow Memory & Retrieval
Record demonstrated user tasks into a structured, persistent task memory. Map natural language triggers to recorded workflows so users can request tasks casually without rigid syntax.

### R4. Staged Verification & Progressive Test Suite
Deliver the application in verified stages. Each stage (actuator primitives, recorder, memory engine, closed-loop executor, companion interface) must pass automated unit and integration tests before proceeding to the next stage.

### R5. End-to-End Autonomous Benchmark Task
Validate the completed application by executing an autonomous end-to-end task: launching the Notes app, creating a new document, and writing out a weekly to-do list entirely hands-free.

## Acceptance Criteria

### Stage-by-Stage Verification
- [ ] Each subsystem includes programmatic tests verifying functionality independently.
- [ ] All test suites pass cleanly with automated reporting.

### Zero-Wiring Desktop Execution
- [ ] Can launch native desktop applications (e.g. Notes) and navigate windows without app-specific plugins or API tokens.
- [ ] Performs mouse movements, clicks, and text typing accurately via OS accessibility and input synthesis.
- [ ] Includes a failsafe emergency stop (e.g. mouse in screen corner or designated hotkey) that halts execution immediately.

### Personality & Interaction
- [ ] Companion displays a distinct, engaging personality when responding, asking clarifications, and reporting execution status.

### End-to-End Autonomous Benchmark
- [ ] The app autonomously opens the native Notes app on the machine.
- [ ] Creates a new note and writes a formatted weekly to-do list without any manual human typing or clicking.
- [ ] Confirms successful completion of the to-do list creation back to the user.

## Follow-up — 2026-09-23T18:52:00Z

An autonomous desktop companion app with an engaging personality that observes and remembers user tasks, operating the computer hands-free across native applications (mouse, keyboard, app launching) without requiring app-specific API wiring or pre-configuration.

Working directory: ~/teamwork_projects/task_memory_automator
Integrity mode: development

## Prior Work & State: Resume from Milestone 2
Milestone 1 is completely implemented, verified, and certified:
- 191/191 tests passing across `tests/unit/` and `tests/integration/`.
- `src/actuators/` has full macOS CoreGraphics ctypes bindings (`macos.py`), 50Hz failsafe watchdog (`failsafe.py`), and mock simulator (`mock.py`).
- Detailed technical blueprints for Milestone 2 (`src/memory/`) have already been formulated by explorers in `.agents/teamwork/explorer_m2_1_rep/handoff.md`, `explorer_m2_2_rep/handoff.md`, and `explorer_m2_3_rep/handoff.md`.

Please survey existing code, do not re-do Milestone 1, and proceed directly to implement Milestone 2 (`src/memory/`) followed by Milestones 3, 4, and 5.

## Requirements

### R1. Generic Zero-Wiring OS Control & App Launching
Autonomously interact with any application on the operating system without requiring custom integrations, API keys, or pre-wired plugins. Control native app launching, window focusing, mouse clicks/movements, and keyboard entry directly at the OS level. (Status: Milestone 1 COMPLETED)

### R2. Conversational Companion with Personality
Provide an interactive companion persona that communicates with the user in natural language, confirms intentions, provides lively commentary during task execution, and manages task memory through conversation.

### R3. Teach-Mode Workflow Memory & Retrieval
Record demonstrated user tasks into a structured, persistent task memory. Map natural language triggers to recorded workflows so users can request tasks casually without rigid syntax.

### R4. Staged Verification & Progressive Test Suite
Deliver the application in verified stages. Each stage (actuator primitives, recorder, memory engine, closed-loop executor, companion interface) must pass automated unit and integration tests before proceeding to the next stage.

### R5. End-to-End Autonomous Benchmark Task
Validate the completed application by executing an autonomous end-to-end task: launching the Notes app, creating a new document, and writing out a weekly to-do list entirely hands-free.

## Acceptance Criteria

### Stage-by-Stage Verification
- [x] Milestone 1: Subsystem includes programmatic tests verifying functionality independently (191 tests passed).
- [ ] Milestone 2: `src/memory/` verified with comprehensive unit and boundary tests.
- [ ] Milestone 3: `src/executor/` verified with closed-loop replay tests.
- [ ] Milestone 4: `src/companion/` verified with conversational intent and tone tests.
- [ ] Milestone 5: Full end-to-end benchmark in native Notes app verified.

### Zero-Wiring Desktop Execution
- [x] Can launch native desktop applications (e.g. Notes) and navigate windows without app-specific plugins or API tokens.
- [x] Performs mouse movements, clicks, and text typing accurately via OS accessibility and input synthesis.
- [x] Includes a failsafe emergency stop (e.g. mouse in screen corner or designated hotkey) that halts execution immediately.

### Personality & Interaction
- [ ] Companion displays a distinct, engaging personality when responding, asking clarifications, and reporting execution status.

### End-to-End Autonomous Benchmark
- [ ] The app autonomously opens the native Notes app on the machine.
- [ ] Creates a new note and writes a formatted weekly to-do list without any manual human typing or clicking.
- [ ] Confirms successful completion of the to-do list creation back to the user.

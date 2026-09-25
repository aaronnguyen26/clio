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

## Follow-up — 2026-09-24T17:58:04Z

Conduct a comprehensive security and logic architecture audit of the Clio autonomous desktop companion app as a senior security and logic engineer (10+ years experience), diagnosing latent vulnerabilities, race conditions, and architectural flaws, and generating architectural test cases that have zero impact on the user's computer and desktop workflow.

Working directory: /Users/minhnguyen/Desktop/Coding/imitate
Integrity mode: demo

## Requirements

### R1. Non-Disruptive & Background Safety Guarantees
All architectural investigations, validations, and test case executions must be strictly non-disruptive to the user's machine and workflow:
- Independent Virtual Cursor Only: Never touch or displace the user's physical hardware mouse pointer (`kCGHIDEventTap` / `CGWarpMouseCursorPosition` are strictly prohibited). All cursor simulation must route through Clio's independent `VirtualCursor` (`CGEventPostToPid` or `MockActuator`).
- Background-Only Application Launches: Any application launched or inspected during tests must run strictly in the background (e.g. `open -g -j` or headless mock) without stealing active window focus, popping up over user windows, or interrupting user input.
- Strict View-Only Codebase Preservation: Zero modifications to existing files in `src/` or `tests/`.

### R2. Threat Model & Attack Surface Audit
Perform a systematic security analysis across all application boundaries:
- Local HTTP and SSE Server (`src/server/server.py`): Cross-Origin Resource Sharing (CORS), Cross-Site Request Forgery (CSRF), DNS rebinding, unauthenticated local REST endpoints (`/api/replay`, `/api/record/*`, `/api/chat`) enabling local/remote arbitrary OS action replay or execution, and lack of origin verification.
- OS Actuators & IPC Security (`src/actuators/`): Ctypes CoreGraphics boundary safety, AppleScript/shell injection vulnerabilities in application launching or window management (`subprocess.run(["open", ...])`), Quartz event injection vectors, and failsafe watchdog bypass conditions.
- Storage & Data Security (`src/memory/`): SQL injection vectors in dynamic queries or schema migrations, plaintext storage of sensitive user input in SQLite task logs, and local file permission boundaries.

### R3. Core Logic, State Machine & Concurrency Diagnosis
Audit internal runtime logic, event loops, and concurrency models for:
- State Machine Race Conditions: Thread contention and state desynchronization between `ThreadingHTTPServer`, `AutonomousWorkflowExecutor`, `LiveDemonstrationCapture`, and `ExecutionPoller`.
- Event Coalescing & Recording: Edge cases in `DemonstrationRecorder` and `LiveDemonstrationCapture` during high-frequency user input, multi-modifier hotkeys, mouse drag segmentation, and focus transitions.
- SQLite Concurrency & Transactions: Database lock timeouts, write contention across worker threads, and uncommitted transaction rollbacks during unexpected aborts or failsafe triggers.
- Parameter Extraction & Retrieval Logic: Failure modes in `NLRetrievalEngine` token scoring, coordinate scaling errors across multi-monitor setups, and dynamic variable binding hallucinations.

### R4. Architectural Test Case Specifications
Develop a complete architectural test suite specification covering the diagnosed failure modes and edge cases:
- Each test case must specify Preconditions, Trigger Action, Expected Behavior, Failure/Vulnerability Mode, and Impact.
- All test case executions must operate non-destructively using `MockActuator` or Clio's background `VirtualCursor` with isolated in-memory databases (`:memory:`).

### R5. Senior Engineer Architecture Audit Deliverable
Produce an in-depth, professional audit report (`AUDIT_REPORT.md`) containing:
- Executive Summary & System Architecture Overview.
- Threat Matrix & Severity Classification (Critical, High, Medium, Low/Informational).
- Detailed Vulnerability & Logic Flaw Catalog with exact file and line references, root cause analysis, exploit/failure scenarios, and impact assessments.
- Concrete, actionable remediation guidelines and hardened architecture recommendations.

## Acceptance Criteria

### Workflow & User Safety Guardrails
- [ ] No hardware mouse displacement occurs during any testing phase (100% verified via virtual cursor invariants or mock actuators).
- [ ] No active window focus is stolen from the user; background flags (`open -g -j` or mocks) are strictly used.
- [ ] Existing codebase integrity is 100% preserved (`git diff src/ tests/` is clean).

### Subsystem Audit Depth
- [ ] Complete threat modeling and logic analysis across `src/actuators`, `src/memory`, `src/executor`, `src/server`, `src/companion`, and `src/benchmark`.
- [ ] Vulnerabilities and concurrency bugs cited with specific file paths and line ranges.

### Architectural Test Cases & Report Quality
- [ ] Test cases specify clear, repeatable steps to verify each diagnosed vulnerability and logic failure mode without host machine side effects.
- [ ] Final report in `AUDIT_REPORT.md` details threat ratings, architectural diagnosis, and remediation blueprints.


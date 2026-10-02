# GEMINI.md - Core Operating Rules

> **MANDATORY**: The AI must read and follow this file before beginning execution on any task or prompt.

## 1. Plan Before Execution
- Break down tasks into structured, incremental phases and smaller actionable steps before touching code.
- Validate requirements and assumptions upfront.

## 2. Efficiency & Accuracy
- Optimize for lean, correct solutions without unnecessary overhead or bloat.
- Prioritize high precision, clean code, and zero regressions.

## 3. UI Design with Stitch
- Always use **Stitch** (`StitchMCP`) for UI design, screen generation, layout iterations, and design system alignment.

## 4. Verification & Testing
- **Always Test Before Ending**: Create automated tests and execute verification steps before completing any prompt.
- **Subagents for Verification**: Spin up dedicated verification subagents when dealing with complex changes, audits, or independent validation.
- Never declare a task complete without concrete verification evidence.

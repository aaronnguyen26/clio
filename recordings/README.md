# Clio Screen Demonstrations & Recordings

This directory stores all screen recordings, frame captures, metadata, and quality evaluation reports captured during user demonstrations.

## Structure
Each recorded demonstration is saved under its unique session UUID:
```
recordings/<session_id>/
├── recording.mov          # Native macOS Retina screen recording
├── frames/                # Periodic frame captures (PNG)
├── metadata.json          # Temporal window coordinates & interaction events
└── quality_report.json    # Evaluator grade, resolution, and defect analysis
```

You can view `.mov` recordings directly in QuickTime Player or right within the Clio Bar HUD.

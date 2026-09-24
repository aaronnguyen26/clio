"""Conversational Companion Subsystem.

Belongs to Milestone M4 (Conversational Companion Persona & Lively Commentary Engine).
"""

from __future__ import annotations

from src.companion.commentary import CommentaryEngine
from src.companion.dialogue import CompanionDialogueEngine, DialogueState
from src.companion.session import CompanionSession

__all__ = [
    "CommentaryEngine",
    "CompanionDialogueEngine",
    "DialogueState",
    "CompanionSession",
]

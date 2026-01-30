"""Conversation storage and retrieval."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class ConversationMetadata:
    """Metadata for a saved conversation."""

    id: str
    model: str
    started: str
    ended: str
    message_count: int
    input_tokens: int
    output_tokens: int
    first_user_message: str = ""

    @property
    def display_preview(self) -> str:
        """Short preview for listing."""
        preview = self.first_user_message[:60]
        if len(self.first_user_message) > 60:
            preview += "..."
        return preview


@dataclass
class Conversation:
    """A complete saved conversation."""

    id: str
    model: str
    system_prompt: str
    history: list[dict[str, Any]]
    started: str
    ended: str
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conversation":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            model=data["model"],
            system_prompt=data["system_prompt"],
            history=data["history"],
            started=data["started"],
            ended=data["ended"],
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
        )


class ConversationStore:
    """Manages saving and loading conversations."""

    def __init__(self, base_dir: Path) -> None:
        self.conversations_dir = base_dir / "conversations"
        self.conversations_dir.mkdir(parents=True, exist_ok=True)

    def _generate_id(self) -> str:
        """Generate a conversation ID from current timestamp."""
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    def save(
        self,
        model: str,
        system_prompt: str,
        history: list[dict[str, Any]],
        input_tokens: int,
        output_tokens: int,
        started: datetime,
        conversation_id: str | None = None,
    ) -> Path:
        """Save a conversation and return the file path."""
        conv_id = conversation_id or self._generate_id()
        ended = datetime.now()

        conversation = Conversation(
            id=conv_id,
            model=model,
            system_prompt=system_prompt,
            history=history,
            started=started.isoformat(),
            ended=ended.isoformat(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        file_path = self.conversations_dir / f"{conv_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(conversation.to_dict(), f, indent=2, ensure_ascii=False)

        return file_path

    def load(self, conversation_id: str) -> Conversation | None:
        """Load a conversation by ID."""
        file_path = self.conversations_dir / f"{conversation_id}.json"
        if not file_path.exists():
            return None

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        return Conversation.from_dict(data)

    def list_conversations(self, limit: int = 20) -> list[ConversationMetadata]:
        """List recent conversations, newest first."""
        files = sorted(
            self.conversations_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]

        results = []
        for file_path in files:
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)

                # Find first user message for preview
                first_user = ""
                for msg in data.get("history", []):
                    if msg.get("role") == "user" and msg.get("content"):
                        first_user = msg["content"]
                        break

                results.append(
                    ConversationMetadata(
                        id=data["id"],
                        model=data.get("model", "unknown"),
                        started=data.get("started", ""),
                        ended=data.get("ended", ""),
                        message_count=len(data.get("history", [])),
                        input_tokens=data.get("input_tokens", 0),
                        output_tokens=data.get("output_tokens", 0),
                        first_user_message=first_user,
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue

        return results

    def get_latest_id(self) -> str | None:
        """Get the ID of the most recent conversation."""
        conversations = self.list_conversations(limit=1)
        return conversations[0].id if conversations else None

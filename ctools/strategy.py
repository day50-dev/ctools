#!/usr/bin/env python3
"""
strategy - chunking strategies for concept extraction

A strategy defines how to extract concepts from a conversation.
Since concept extraction is subjective (ontology is contestable),
different strategies can produce different chunkings.

A strategy is: {host, model, api_key, prompt}

Strategy lookup order:
1. If path is absolute or contains /, use as-is
2. Check current directory for name.json
3. Check ~/.config/ctools/strategies/name.json
"""

import json
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List
from rich.console import Console

console = Console()

STRATEGIES_DIR = Path.home() / ".config" / "ctools" / "strategies"

DEFAULT_PROMPT = """Extract the key concepts from this conversation.
For each concept, output a JSON object with these fields:
- type: one of "constraint", "goal", "preference", "observation", "reference"
- description: a short (<20 word) summary
- short: the concept in under 250 chars
- medium: the concept in under 1000 chars
- long: the full concept text

Output a JSON array of these objects. Do not include any other text."""


@dataclass
class Strategy:
    """A chunking strategy for concept extraction."""
    host: str
    model: str
    api_key: Optional[str] = None
    prompt: str = DEFAULT_PROMPT

    def save(self, path: str):
        """Save strategy to a JSON file."""
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)
            f.write("\n")

    @classmethod
    def load(cls, path: str) -> "Strategy":
        """Load strategy from a JSON file."""
        p = Path(path)
        if not p.exists():
            console.print(f"[red]Strategy file not found: {path}[/red]")
            raise SystemExit(1)
        with open(p) as f:
            data = json.load(f)
        return cls(**data)

    @classmethod
    def resolve(cls, name: str) -> "Strategy":
        """Resolve a strategy name to a Strategy object.

        Lookup order:
        1. If name is a path with / or starts with ., use as-is
        2. Check current directory for name.json
        3. Check ~/.config/ctools/strategies/name.json
        """
        # If it looks like a path, use as-is
        if "/" in name or name.startswith("."):
            return cls.load(name)

        # Check current directory
        local_path = Path.cwd() / f"{name}.json"
        if local_path.exists():
            return cls.load(str(local_path))

        # Check strategies directory
        global_path = STRATEGIES_DIR / f"{name}.json"
        if global_path.exists():
            return cls.load(str(global_path))

        # Not found anywhere
        console.print(f"[red]Strategy not found: {name}[/red]")
        console.print(f"[dim]Searched: ./{local_path.name}, {global_path}[/dim]")
        raise SystemExit(1)

    def extract(self, messages: list) -> list:
        """Use this strategy to extract concepts from messages.
        
        This calls the LLM with the strategy's prompt and returns
        the extracted concepts as a list of dicts.
        """
        import requests

        # Build the conversation for the LLM
        conversation = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if content:
                conversation.append({"role": role, "content": content})

        # Add the extraction prompt
        conversation.append({"role": "user", "content": self.prompt})

        # Make the API call
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        base_url = self.host.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"

        req = {
            "model": self.model,
            "messages": conversation,
            "temperature": 0.0,
        }

        try:
            r = requests.post(
                f"{base_url}/chat/completions",
                json=req,
                headers=headers,
                timeout=60,
            )
            r.raise_for_status()
            resp = r.json()
            content = resp["choices"][0]["message"]["content"]

            # Parse JSON from response (may be wrapped in markdown)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            concepts = json.loads(content.strip())
            if not isinstance(concepts, list):
                concepts = [concepts]
            return concepts

        except Exception as e:
            console.print(f"[red]Strategy extraction failed: {e}[/red]")
            return []


DEFAULT_STRATEGY = Strategy(
    host="http://localhost:11434",
    model="qwen2.5:3b",
    prompt=DEFAULT_PROMPT,
)


def list_strategies() -> List[str]:
    """List all strategy names in the default location."""
    if not STRATEGIES_DIR.exists():
        return []

    return [p.stem for p in STRATEGIES_DIR.glob("*.json")]


def ensure_strategies_dir():
    """Create the strategies directory if it doesn't exist."""
    STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)

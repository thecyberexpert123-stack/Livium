"""Write one verified experience record and exit: process one of demonstration M5."""

import json

from livium.config import Config
from livium.memory import Memory
from livium.store import Store


def main() -> None:
    config = Config.load()
    config.ensure_dirs()
    memory = Memory(Store(config.paths.db))
    memory_id, action = memory.remember(
        "capability",
        "counting words: word_count already exists and is validated; reuse it instead of "
        "writing a new capability",
        key="word_count", verification="verified", confidence=0.9,
        evidence="promoted after its tests passed", tags=["reuse:word_count"],
    )
    print(json.dumps({"memory_id": memory_id, "action": action}))


if __name__ == "__main__":
    main()

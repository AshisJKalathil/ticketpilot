"""Re-pin prompt hashes after an intentional, version-bumped prompt change.

    python scripts/lock_prompts.py

Refuses to update the lock when a template changed but its version did not.
"""

from ticketpilot.config import Settings
from ticketpilot.prompts import PromptRegistry

if __name__ == "__main__":
    lock = PromptRegistry(Settings().prompts_dir).write_lock()
    for pid, entry in sorted(lock.items()):
        print(f"{pid:20s} {entry['version']:8s} {entry['sha256'][:12]}")

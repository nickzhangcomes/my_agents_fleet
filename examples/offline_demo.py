"""Exercise real execution recording with an explicitly fake, offline Agent."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from my_agents2 import Agent, Caps, Event
from my_agents_fleet import Capacity, RecordedAgent, RunStore


class OfflineAgent(Agent):
    """Deterministic demonstration only: no model, subprocess, or network."""

    caps = Caps(cwd=True)

    def stream(self, prompt, *, continuation=None, handle=None):
        if handle is not None and handle.stopped:
            yield Event(kind="error", text="Stopped before the offline response")
            return
        text = "Offline response: " + prompt
        yield Event(kind="text", text=text)
        yield Event(kind="result", text=text,
                    raw={"cost_usd": 0.0, "continuation": "offline-demo"})


def main():
    with TemporaryDirectory(prefix="my-agents-fleet-demo-") as directory:
        root = Path(directory)
        store = RunStore(root / "fleet.db")
        agent = RecordedAgent(
            OfflineAgent(name="demo", cwd=directory), store=store,
            name="demo", backend="offline-demo", max_attempts=1,
            capacity=Capacity(root / "locks"), max_concurrent=1,
            workspace=directory,
        )
        conversation = agent.conversation()
        for prompt in ("Summarize the project.", "Suggest one next step."):
            result = conversation.ask(prompt)
            if not result.ok:
                raise RuntimeError(str(result.failure))
            print(result.text)
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    print("Two recorded runs completed. Temporary data removed. No model was called.")


if __name__ == "__main__":
    main()

"""
Validate the Foundry side of the relay without the Teams/bot layer.

Exercises the two doc-verified-but-not-yet-live calls:
  1. responses.create(..., extra_body={"agent_reference": ...})
  2. containers.files.content.retrieve(file_id=, container_id=)

Needs only FOUNDRY_PROJECT_ENDPOINT + FOUNDRY_AGENT_NAME in .env and a logged-in
identity with Foundry access (AZURE_* service principal vars, or az login —
run via run_test.ps1 to put the pip-installed az on PATH).

Usage:
  .venv\Scripts\python test_foundry_pipeline.py            # CSV + chart prompts
  .venv\Scripts\python test_foundry_pipeline.py "custom prompt"
"""

import json
import os
import sys

import app  # noqa: E402  (constructs clients from .env)

OUT_DIR = os.path.join(os.path.dirname(__file__), "test_outputs")


def show_response(response) -> None:
    print(f"\nresponse id={response.id} status={response.status}")
    for i, item in enumerate(response.output):
        print(f"  output[{i}] type={getattr(item, 'type', '?')}")
        for block in getattr(item, "content", None) or []:
            btype = getattr(block, "type", "?")
            print(f"    block type={btype}")
            if btype == "output_text":
                print(f"      text: {block.text[:300]!r}")
                for ann in block.annotations or []:
                    print(f"      annotation: type={getattr(ann, 'type', '?')} "
                          f"file={getattr(ann, 'filename', '?')} "
                          f"file_id={getattr(ann, 'file_id', '?')} "
                          f"container={getattr(ann, 'container_id', '?')}")


def run_turn(conv_key: str, prompt: str) -> None:
    print(f"\n=== USER: {prompt}")
    response = app._run_agent_sync(conv_key, prompt)
    show_response(response)

    activity = app.build_reply(response)
    print(f"\nreply text: {activity.text!r}")
    print(f"attachments: {len(activity.attachments or [])}")
    for att in activity.attachments or []:
        card = att.content
        kinds = [el.get("type") for el in card.get("body", [])]
        print(f"  card body: {kinds}, actions: {len(card.get('actions', []))}")

    # also save raw files locally as proof
    os.makedirs(OUT_DIR, exist_ok=True)
    seen = set()
    for item in response.output:
        if getattr(item, "type", None) != "message":
            continue
        for block in item.content or []:
            for ann in getattr(block, "annotations", None) or []:
                if getattr(ann, "type", None) == "container_file_citation" and ann.file_id not in seen:
                    seen.add(ann.file_id)
                    data = app._download_container_file_sync(ann.container_id, ann.file_id)
                    path = os.path.join(OUT_DIR, ann.filename or ann.file_id)
                    with open(path, "wb") as f:
                        f.write(data)
                    print(f"  saved {path} ({len(data)} bytes)")


if __name__ == "__main__":
    conv_key = "local-test"
    if len(sys.argv) > 1:
        run_turn(conv_key, " ".join(sys.argv[1:]))
    else:
        run_turn(conv_key, "Create a CSV of 12 months of sample sales data and give me the file.")
        run_turn(conv_key, "Plot that data as a bar chart.")
    print("\nDone. Files in", OUT_DIR)

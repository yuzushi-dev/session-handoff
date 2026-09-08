"""Translate the shared synthetic history to Anthropic message blocks."""

import json

from benchmark.information_preservation import validate_structured_conversation


def native_messages(conversation):
    source = validate_structured_conversation(conversation)
    messages = []
    for item in source["messages"]:
        role = item["role"]
        if role == "tool":
            role = "user"
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": item["call_id"],
                    "content": item["content"],
                }
            ]
        else:
            blocks = [{"type": "text", "text": item["content"]}]
            if "tool_call" in item:
                call = item["tool_call"]
                arguments = json.loads(call["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Anthropic tool input must be an object")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["call_id"],
                        "name": call["name"],
                        "input": arguments,
                    }
                )
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    return messages

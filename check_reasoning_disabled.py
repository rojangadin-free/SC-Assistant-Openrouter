"""
Verifies whether OpenRouter is actually turning off "thinking" for
deepseek/deepseek-v4-flash-0731 when you pass reasoning: {enabled: False}.

Run:
    export OPENROUTER_API_KEY=sk-or-...
    python check_reasoning_disabled.py
"""

import os
import json
import requests

API_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = "deepseek/deepseek-v4-flash-0731"

PROMPT = "What is 17 * 24? Just answer, don't show your work."


def call(extra_body_reasoning):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": 0.2,
    }
    if extra_body_reasoning is not None:
        body["reasoning"] = extra_body_reasoning

    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def summarize(label, data):
    choice = data["choices"][0]
    message = choice["message"]
    usage = data.get("usage", {})

    reasoning_text = message.get("reasoning") or message.get("reasoning_content")
    reasoning_details = message.get("reasoning_details")
    reasoning_tokens = (
        usage.get("completion_tokens_details", {}).get("reasoning_tokens")
    )

    print(f"\n=== {label} ===")
    print("content:", message.get("content"))
    print("reasoning field present:", bool(reasoning_text))
    if reasoning_text:
        print("reasoning (truncated):", reasoning_text[:200], "...")
    print("reasoning_details present:", bool(reasoning_details))
    print("usage:", json.dumps(usage, indent=2))
    print("reasoning_tokens (billed):", reasoning_tokens)


if __name__ == "__main__":
    # 1. Default behavior (thinking mode defaults to ON / high effort per DeepSeek docs)
    default_result = call(extra_body_reasoning=None)
    summarize("Default (no reasoning param)", default_result)

    # 2. Explicitly disabled via OpenRouter's normalized `reasoning` field
    disabled_result = call(extra_body_reasoning={"enabled": False})
    summarize("reasoning: {enabled: False}", disabled_result)

    # Quick verdict
    def reasoning_tokens(data):
        return (
            data.get("usage", {})
            .get("completion_tokens_details", {})
            .get("reasoning_tokens", 0)
        ) or 0

    default_tok = reasoning_tokens(default_result)
    disabled_tok = reasoning_tokens(disabled_result)

    print("\n=== Verdict ===")
    print(f"Default reasoning tokens:  {default_tok}")
    print(f"Disabled reasoning tokens: {disabled_tok}")
    if disabled_tok == 0 and default_tok > 0:
        print("✅ reasoning:{enabled:False} is working — thinking is off and you're not billed for it.")
    elif disabled_tok > 0:
        print("⚠️  Reasoning tokens were still billed even with enabled:False — check the provider routed to; not all backing providers honor the flag identically.")
    else:
        print("ℹ️  Default call also produced 0 reasoning tokens — this prompt may be too trivial to trigger thinking either way. Try a harder prompt.")

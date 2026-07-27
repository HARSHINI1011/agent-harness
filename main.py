"""
Run Budget & Loop Guard endpoint.

POST / (and POST /decide, same handler) with:
{
  "budget_tokens": <int>,
  "steps": [
    {"step_number": <int>, "tool": "<string>", "args": <object>, "tokens_used": <int>},
    ...
  ]
}

Returns:
{ "decision": "continue" | "halt", "reason": "short human-readable string" }
"""

import json
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


# ---------- Canonicalization ----------

def _normalize_whitespace(s: str) -> str:
    # Collapse any run of whitespace to a single space, trim ends.
    return " ".join(s.split())


def canonicalize(value: Any) -> Any:
    """Recursively normalize a JSON-like value so that cosmetic
    differences (key order, whitespace-only string diffs, and the
    client_ts tracing field) disappear before comparison."""
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            if k == "client_ts":
                continue  # tracing id, never meaningful to the action
            cleaned[k] = canonicalize(v)
        # sort_keys=True on dump handles key-order; nothing else needed here
        return cleaned
    elif isinstance(value, list):
        return [canonicalize(v) for v in value]
    elif isinstance(value, str):
        return _normalize_whitespace(value)
    else:
        return value


def step_signature(step: Dict[str, Any]) -> Tuple[str, str]:
    """A hashable/comparable fingerprint for a step: (tool, canonical args as text)."""
    tool = step.get("tool")
    canon_args = canonicalize(step.get("args", {}))
    canon_text = json.dumps(canon_args, sort_keys=True, separators=(",", ":"))
    return (tool, canon_text)


# ---------- Loop detection ----------

def trailing_repeat_count(signatures: List[Tuple[str, str]]) -> int:
    """How many steps, counting back from the end, share the exact same signature."""
    if not signatures:
        return 0
    last = signatures[-1]
    count = 0
    for sig in reversed(signatures):
        if sig == last:
            count += 1
        else:
            break
    return count


def has_two_step_cycle(signatures: List[Tuple[str, str]]) -> bool:
    """Detect an A,B,A,B,A,B pattern in the last 6 steps."""
    if len(signatures) < 6:
        return False
    tail = signatures[-6:]
    a, b = tail[0], tail[1]
    if a == b:
        return False  # that's a same-call repeat, handled separately
    expected = [a, b, a, b, a, b]
    return tail == expected


# ---------- Core decision policy ----------

def decide(budget_tokens: int, steps: List[Dict[str, Any]]) -> Dict[str, str]:
    total_tokens = sum(int(s.get("tokens_used", 0)) for s in steps)

    # Rule 1: budget check (independent of loop check)
    if total_tokens >= budget_tokens:
        return {
            "decision": "halt",
            "reason": (
                f"Cumulative tokens_used ({total_tokens}) has reached the "
                f"budget ({budget_tokens})."
            ),
        }

    signatures = [step_signature(s) for s in steps]

    # Rule 2a: same tool+args 3+ times in a row
    repeat_count = trailing_repeat_count(signatures)
    if repeat_count >= 3:
        tool_name = steps[-1].get("tool") if steps else "unknown"
        return {
            "decision": "halt",
            "reason": (
                f"Detected {repeat_count} consecutive identical calls to "
                f"'{tool_name}' with functionally identical arguments — "
                f"looks like a stuck loop."
            ),
        }

    # Rule 2b: 2-step alternating cycle over the trailing 6+ steps
    if has_two_step_cycle(signatures):
        return {
            "decision": "halt",
            "reason": (
                "Detected an alternating 2-step cycle (A, B, A, B, A, B) in "
                "the trailing steps — looks like a stuck loop."
            ),
        }

    return {
        "decision": "continue",
        "reason": "Budget has room and no repeated-call or cycle pattern detected.",
    }


# ---------- HTTP layer ----------

@app.get("/")
async def health():
    return {"status": "ok", "service": "run-budget-and-loop-guard"}


@app.post("/")
async def guard_root(request: Request):
    return await _handle(request)


@app.post("/decide")
async def guard_decide(request: Request):
    return await _handle(request)


async def _handle(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        budget_tokens = int(body["budget_tokens"])
        steps = body.get("steps", []) or []
        result = decide(budget_tokens, steps)
        return JSONResponse(content=result)
    except Exception as e:
        # Fail safe: malformed input should not crash the harness.
        return JSONResponse(
            content={"decision": "halt", "reason": f"Bad request: {e}"},
            status_code=200,
        )

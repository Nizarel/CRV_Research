"""One strict JSON-schema chat completion with throttling retries (Azure OpenAI client)."""

from __future__ import annotations

import asyncio
import json
from typing import Any


async def structured(
    client: Any,
    deployment: str,
    instructions: str,
    content: str,
    name: str,
    schema: dict[str, Any],
    *,
    max_tokens: int = 4000,
    effort: str = "low",
) -> dict[str, Any]:
    """Parsed JSON of one completion; 429 and transient 5xx are retried with backoff."""

    for attempt in range(6):
        try:
            response = await client.chat.completions.create(
                model=deployment,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema},
                },
                max_completion_tokens=max_tokens,
                reasoning_effort=effort,
            )
            break
        except Exception as error:  # throttling and transient service errors
            status = getattr(error, "status_code", None)
            if status not in (429, 500, 502, 503, 504) or attempt == 5:
                raise
            await asyncio.sleep(min(60, 4 * 2**attempt))
    parsed: dict[str, Any] = json.loads(response.choices[0].message.content or "{}")
    return parsed

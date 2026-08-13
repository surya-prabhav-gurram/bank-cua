"""
The action space the LLM emits during discovery.

Kept tiny and structured so any provider (real API tool-use, or the bridge)
returns the same validated object. Elements are addressed by the integer `ref`
from the observation's element index -- the model never writes selectors; the
system synthesises robust locators from the live element it picked.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class DiscoveryAction(BaseModel):
    action: Literal["navigate", "click", "fill", "select", "press",
                    "extract", "finish", "escalate"]
    intent: str = Field(default="", description="Why the model is doing this.")

    # element-targeted actions reference an element by ref from the observation
    ref: Optional[int] = None

    # navigate
    url: Optional[str] = None

    # fill / select / press
    value: Optional[str] = None
    select_by: Literal["value", "label"] = "label"
    key: Optional[str] = None

    # extract
    output_name: Optional[str] = None
    attribute: Literal["text", "inner_text", "value", "href"] = "text"

    # finish / escalate
    success: Optional[bool] = None
    reason: str = ""


# JSON schema for the single tool exposed to the model via the Messages API.
ACT_TOOL = {
    "name": "act",
    "description": "Take one action to make progress toward the goal, or finish/escalate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["navigate", "click", "fill", "select", "press",
                                "extract", "finish", "escalate"]},
            "intent": {"type": "string", "description": "Why you are doing this."},
            "ref": {"type": "integer",
                    "description": "Element ref from the observation (click/fill/select/extract)."},
            "url": {"type": "string", "description": "Path or URL (navigate)."},
            "value": {"type": "string", "description": "Text to type / option to choose."},
            "select_by": {"type": "string", "enum": ["value", "label"]},
            "key": {"type": "string", "description": "Key name (press), e.g. Enter."},
            "output_name": {"type": "string",
                            "description": "Declared output to populate (extract)."},
            "attribute": {"type": "string",
                          "enum": ["text", "inner_text", "value", "href"]},
            "success": {"type": "boolean", "description": "Goal met? (finish)"},
            "reason": {"type": "string", "description": "Explanation (finish/escalate)."},
        },
        "required": ["action", "intent"],
    },
}

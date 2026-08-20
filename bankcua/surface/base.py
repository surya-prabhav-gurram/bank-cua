"""
Surface abstraction: the seam between *how we perceive/act on a surface* and
*the recorded flow*.

The artifact schema, the discovery loop, and the replay engine all speak only
this interface. Swapping the concrete implementation (Playwright web today; a
legacy-web frame driver or a desktop accessibility driver tomorrow) does not
touch the schema or the engines -- that is the whole point of the seam.

Two access paths are provided deliberately:

  * `index_elements()` -> [ElementInfo]  (perception; used by DISCOVERY)
      Enumerates addressable controls across all frames and, for each, computes
      *several* locator strategies from the live DOM. The model picks a control
      by `ref`; the compiler turns the ElementInfo into a robust, multi-candidate
      Locator. Synthesising locators from the live element at record time is what
      makes replay's locators trustworthy.

  * Locator-based primitives: `click/fill/select/read/check/detect`  (REPLAY)
      Operate purely on a stored Locator with no LLM and no refs.

Discovery reuses the Locator-based primitives too (it synthesises a Locator from
the chosen element and acts through it), so the exact targeting that gets
recorded is the exact targeting that gets exercised.
"""
from __future__ import annotations

import abc
from typing import Optional

from pydantic import BaseModel, Field

from ..schema import Checkpoint, ConditionDetector, Locator


class ElementInfo(BaseModel):
    """A single addressable control, as perceived during discovery."""
    ref: int
    frame_path: list[str] = Field(default_factory=list)
    tag: str = ""
    role: str = ""
    name: str = ""            # accessible name
    type_attr: str = ""       # <input type=...>
    label: str = ""
    placeholder: str = ""
    near_label: str = ""      # proximate label text (legacy tables w/o <label for>)
    text: str = ""            # trimmed visible text
    # Structural evidence of an irreversible side effect. A submit control inside
    # a POST form is far stronger evidence than a word match on the caption, and
    # the same distinction exists on a desktop a11y tree (invoke vs. navigate).
    is_submit: bool = False
    form_method: str = ""     # "", "get", "post"
    css: str = ""             # computed reasonably-stable css path
    xpath: str = ""
    visible: bool = True
    enabled: bool = True

    def summary(self) -> str:
        bits = [f"#{self.ref}", self.role or self.tag]
        label = self.name or self.label or self.placeholder or self.near_label
        if label:
            bits.append(repr(label[:50]))
        # value hint (selected option / checked state) shown separately so the
        # model can see progress; typed text/password values are never captured.
        if self.text and self.text != label:
            bits.append(f"[{self.text[:40]}]")
        if self.type_attr:
            bits.append(f"type={self.type_attr}")
        if self.frame_path:
            bits.append(f"frame={'/'.join(self.frame_path)}")
        return " ".join(bits)


class ReadField(BaseModel):
    """A read-only labelled value (e.g. a table row 'Savings | $4,213.55').

    Extraction targets are usually NOT interactive controls, so they are indexed
    separately with a label-relative locator that survives layout churn far
    better than an absolute path.
    """
    ref: int
    frame_path: list[str] = Field(default_factory=list)
    label: str = ""
    value_preview: str = ""
    locator: Locator

    def summary(self) -> str:
        fp = f" frame={'/'.join(self.frame_path)}" if self.frame_path else ""
        return f"#{self.ref} field {self.label!r} = {self.value_preview!r}{fp}"


class Observation(BaseModel):
    """What the agent sees at one step."""
    url: str
    title: str = ""
    http_status: Optional[int] = None
    text_excerpt: str = ""
    screenshot_path: Optional[str] = None
    elements: list[ElementInfo] = Field(default_factory=list)
    readouts: list[ReadField] = Field(default_factory=list)
    frames: list[str] = Field(default_factory=list)

    def render_for_model(self, max_elems: int = 40) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.http_status is not None:
            lines.append(f"HTTP: {self.http_status}")
        if self.text_excerpt:
            lines.append("VISIBLE TEXT:\n" + self.text_excerpt[:1200])
        lines.append("INTERACTIVE ELEMENTS:")
        for e in self.elements[:max_elems]:
            lines.append("  " + e.summary())
        if self.readouts:
            lines.append("READABLE FIELDS (use extract with these refs):")
            for r in self.readouts[:max_elems]:
                lines.append("  " + r.summary())
        return "\n".join(lines)


class ActResult(BaseModel):
    ok: bool
    message: str = ""
    candidate_index: Optional[int] = Field(
        default=None, description="Which locator candidate resolved (0-based)."
    )
    value: Optional[str] = None
    rows: Optional[list[dict[str, str]]] = Field(
        default=None,
        description="Structured rows from a read with attribute='table'. A second "
        "TYPED channel rather than widening `value` to Any: every existing caller "
        "of `value` keeps its str contract, and a caller that wants rows cannot "
        "accidentally receive a stringified grid it then has to re-parse.",
    )


class Surface(abc.ABC):
    """Abstract perceive/act interface. See module docstring."""

    #: Locator strategies this surface can actually resolve. A surface with no
    #: DOM cannot honour css/xpath/test_id, and saying so in data (rather than
    #: failing at run time) is what lets `portability_report` answer "will this
    #: artifact run here?" BEFORE anything is launched.
    supported_locator_kinds: frozenset = frozenset()

    # ---- lifecycle -------------------------------------------------------
    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    # ---- perception ------------------------------------------------------
    @abc.abstractmethod
    def observe(self) -> Observation: ...

    @abc.abstractmethod
    def index_elements(self) -> list[ElementInfo]:
        """Enumerate addressable controls across frames (discovery)."""

    @abc.abstractmethod
    def index_readouts(self, start_ref: int) -> list[ReadField]:
        """Enumerate read-only labelled values across frames (discovery)."""

    @abc.abstractmethod
    def locator_for_element(self, element: ElementInfo) -> Locator:
        """Synthesise a robust, multi-candidate Locator from a live element."""

    # ---- action (Locator-based; used by both discovery and replay) -------
    @abc.abstractmethod
    def navigate(self, url: str) -> ActResult: ...

    @abc.abstractmethod
    def click(self, locator: Locator) -> ActResult: ...

    @abc.abstractmethod
    def fill(self, locator: Locator, text: str) -> ActResult: ...

    @abc.abstractmethod
    def select_option(self, locator: Locator, value: str, by: str = "value") -> ActResult: ...

    @abc.abstractmethod
    def press(self, key: str) -> ActResult: ...

    @abc.abstractmethod
    def read(self, locator: Locator, attribute: str = "text") -> ActResult: ...

    # ---- verification / detection ---------------------------------------
    @abc.abstractmethod
    def check(self, checkpoint: Checkpoint) -> bool: ...

    @abc.abstractmethod
    def detect(self, detector: ConditionDetector) -> bool: ...

    # ---- evidence --------------------------------------------------------
    @abc.abstractmethod
    def screenshot(self, path: str) -> Optional[str]: ...

    @abc.abstractmethod
    def dom_snapshot(self) -> str: ...

    @abc.abstractmethod
    def current_url(self) -> str: ...

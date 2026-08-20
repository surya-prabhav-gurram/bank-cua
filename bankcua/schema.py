"""
Typed, versioned capability artifact schema.

An artifact is the *reusable capability* produced by a successful discovery run.
It is deliberately decoupled from the raw model transcript: the transcript is
evidence of *how it was discovered*; the artifact is a stable *contract* that a
calling agent invokes and a human reviews.

Design goals baked into this schema:

  * Contract-first. Typed `inputs` and `outputs` make the artifact callable by
    another agent without reading the steps. `success` states what "done" means.
  * Robust targeting. Every element is addressed by an ordered list of locator
    *candidates*, most-stable-first, each with human reasoning. Replay tries them
    in order (see replay.engine), so the flow survives one strategy going stale.
  * Explicit error model. `known_conditions` classify runtime states as
    business-outcome / recoverable / hard-failure -- the single most important
    distinction for production replay.
  * Safety in the schema. Each step carries a risk class, the *reason* it was
    classified that way, and whether a human has ratified that call. The
    heuristic proposes; the approval gate requires a person to confirm before
    a capability may run unattended.
  * Reuse across tenants. `target` separates the *shared flow* from the
    *per-tenant binding* (base_url, tenant_id, vendor/version), and locators
    avoid hard-coding tenant specifics. See REPORT section 4.

Everything is Pydantic v2 so it serialises to/from JSON with validation.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class SurfaceType(str, Enum):
    """The kind of surface the flow runs against.

    The replay engine and locator model are surface-agnostic; only the concrete
    Surface implementation (see surface/) differs. `desktop`/`legacy_web` are
    declared here so artifacts recorded later don't need a schema bump.
    """
    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


class ActionType(str, Enum):
    NAVIGATE = "navigate"          # go to a URL (templated with params)
    CLICK = "click"
    FILL = "fill"                  # type into an input
    SELECT = "select_option"       # choose from a <select>
    PRESS = "press_key"            # keyboard key (e.g. Enter)
    WAIT_FOR = "wait_for"          # wait for a condition, no interaction
    EXTRACT = "extract"            # read data out into a declared output
    ASSERT = "assert_checkpoint"   # assert a checkpoint without acting


class LocatorKind(str, Enum):
    """Ordered roughly by robustness for legacy surfaces.

    Semantic/accessibility strategies (role, label, placeholder, text) tend to
    survive markup churn far better than structural ones (css, xpath) and are
    also what a desktop accessibility tree exposes -- so they port across
    surfaces. `coordinates` is the last-resort screenshot strategy.

    NEAR_LABEL deserves its own note. Legacy forms routinely leave a control with
    no accessible name at all -- no <label for>, no aria-label -- which makes it
    unaddressable by name on ANY surface, including a desktop accessibility tree.
    The only durable handle a human uses there is proximity: "the box next to the
    words 'User ID'". Expressing that as an XPath would bind it to a DOM; as a
    NEAR_LABEL it is a statement of intent each surface resolves in its own terms
    (structurally on the web, spatially against node bounds on an a11y tree). It
    is the strategy that makes an artifact recorded on one surface portable to
    another, and it exists because building the second surface proved that
    role+name alone was not enough.
    """

    ROLE = "role"              # accessibility role + accessible name
    NEAR_LABEL = "near_label"  # the control adjacent to this label text
    LABEL = "label"           # associated <label> text
    PLACEHOLDER = "placeholder"
    TEXT = "text"             # visible text / link text
    ALT_TEXT = "alt_text"
    TITLE = "title"
    TEST_ID = "test_id"       # rare in legacy, but honoured if present
    CSS = "css"
    XPATH = "xpath"
    COORDINATES = "coordinates"  # [x, y] fraction of viewport; screenshot fallback


class RiskClass(str, Enum):
    SAFE = "safe"        # reversible / read-only: navigate, read, fill, select
    RISKY = "risky"      # irreversible side effect: submit/create/confirm/delete


class ValueType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    MONEY = "money"      # normalised to cents on extract
    DATE = "date"


class ConditionClass(str, Enum):
    """The three-way split the brief calls out as the core design point."""
    BUSINESS_OUTCOME = "business_outcome"  # legitimate result the caller needs
    RECOVERABLE = "recoverable"            # handle in-band (dismiss/retry) & go on
    HARD_FAILURE = "hard_failure"          # stop, surface a debuggable error


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------
class LocatorCandidate(BaseModel):
    """One way to find a control. Replay tries candidates in list order."""
    kind: LocatorKind
    value: str = Field(
        description="Selector payload: role name, label text, css, xpath, or "
        "'x,y' fractions for COORDINATES."
    )
    role: Optional[str] = Field(
        default=None,
        description="ARIA role when kind=ROLE (e.g. 'button', 'textbox', 'link').",
    )
    exact: bool = Field(
        default=False, description="Whether text/name matching must be exact."
    )
    reasoning: str = Field(
        default="",
        description="Why this candidate was chosen and how robust it is expected "
        "to be. This is for the human reviewer.",
    )


class Locator(BaseModel):
    """A frame-aware, multi-candidate way to address a control.

    `frame_path` lists nested iframe names/urls from the top document down, so a
    control living inside a legacy <iframe> (like the balance pane) is
    addressable. An empty list means the top-level document.
    """
    description: str = Field(description="Human label, e.g. 'the Search button'.")
    frame_path: list[str] = Field(
        default_factory=list,
        description="Ordered iframe identifiers (name or url substring) to enter "
        "before resolving candidates. Empty = top document.",
    )
    candidates: list[LocatorCandidate] = Field(
        min_length=1,
        description="Ordered most-stable-first. Replay uses the first that "
        "resolves to exactly one visible element.",
    )


# ---------------------------------------------------------------------------
# Values, waits, checkpoints, extraction
# ---------------------------------------------------------------------------
class ValueSource(BaseModel):
    """Where a step's input value comes from at replay time."""
    kind: Literal["literal", "param", "secret_param", "from_output"] = "literal"
    literal: Optional[str] = None
    param: Optional[str] = Field(
        default=None, description="Name of an input parameter or prior output."
    )

    def resolve(self, params: dict[str, Any], outputs: dict[str, Any]) -> str:
        if self.kind == "literal":
            return "" if self.literal is None else str(self.literal)
        if self.kind in ("param", "secret_param"):
            if self.param not in params:
                raise KeyError(f"missing required parameter '{self.param}'")
            return str(params[self.param])
        if self.kind == "from_output":
            return str(outputs[self.param])
        raise ValueError(f"unknown value kind {self.kind}")


class WaitSpec(BaseModel):
    strategy: Literal["selector", "url", "load", "timeout", "network_idle"] = "load"
    target: Optional[str] = Field(
        default=None, description="Selector or url substring, per strategy."
    )
    timeout_ms: int = 10_000


class Checkpoint(BaseModel):
    """An assertion that we actually reached the intended state."""
    kind: Literal["url_matches", "element_visible", "text_present", "text_absent",
                  "http_status_lt"]
    value: str = Field(description="url substring / selector / text / status code.")
    frame_path: list[str] = Field(default_factory=list)
    description: str = ""


class Extraction(BaseModel):
    """Read a value out of the page into a declared output."""
    output: str = Field(description="Name of the OutputField this populates.")
    locator: Locator
    attribute: Literal["text", "inner_text", "value", "href"] = "text"
    transform: Optional[Literal["strip", "money_to_cents", "digits_only"]] = None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
class Step(BaseModel):
    index: int
    intent: str = Field(description="Plain-language purpose, for reviewers/logs.")
    action: ActionType

    # element actions
    target: Optional[Locator] = None
    value: Optional[ValueSource] = None
    select_by: Literal["value", "label"] = "value"
    key: Optional[str] = Field(default=None, description="Key name for PRESS.")

    # navigation
    url_template: Optional[str] = Field(
        default=None,
        description="For NAVIGATE: path relative to base_url; may contain "
        "{param} placeholders bound from inputs.",
    )

    # data out
    extract: Optional[Extraction] = None

    # control / verification
    wait: Optional[WaitSpec] = None
    checkpoint: Optional[Checkpoint] = Field(
        default=None,
        description="Asserted AFTER the action -- the 'did it actually work' "
        "guard that makes replay deterministic rather than hopeful.",
    )

    # verification of the action itself (as opposed to the resulting page state)
    verify_value: bool = Field(
        default=False,
        description="For FILL: after typing, read the control back and assert the "
        "value landed. Catches readonly/disabled/JS-managed inputs that silently "
        "swallow a write. Secret values are asserted non-empty only -- never "
        "compared or logged.",
    )

    # safety
    risk: RiskClass = RiskClass.SAFE
    risk_reason: str = Field(
        default="",
        description="Why this step was classified at its risk level. Recorded so a "
        "human reviewer can judge the classification rather than trust it.",
    )
    risk_reviewed: bool = Field(
        default=False,
        description="A human has reviewed this step's risk classification. The "
        "catalog refuses to approve a capability while any risky step is "
        "unreviewed -- the heuristic proposes, a person ratifies.",
    )
    requires_confirmation: bool = Field(
        default=False,
        description="If true, replay will not perform this step unless running "
        "with explicit approval (see safety.policy).",
    )


# ---------------------------------------------------------------------------
# Known runtime conditions (the error taxonomy, expressed declaratively)
# ---------------------------------------------------------------------------
class ConditionDetector(BaseModel):
    kind: Literal["text_present", "url_matches", "http_status", "element_visible"]
    value: str
    frame_path: list[str] = Field(default_factory=list)


class RecoveryAction(BaseModel):
    kind: Literal["click", "reload", "wait_retry"]
    target: Optional[Locator] = None
    max_attempts: int = 2
    backoff_ms: int = 1500


class KnownCondition(BaseModel):
    """A runtime state the flow anticipates, plus how to classify/handle it.

    Detection runs after each step (and on step failure). The first matching
    condition decides the outcome:
      * business_outcome -> stop, return {status: business_outcome, code}
      * recoverable      -> run `recovery`, then continue (bounded attempts)
      * hard_failure     -> stop, return {status: failure, code}
    """
    code: str = Field(description="Stable machine code, e.g. MEMBER_NOT_FOUND.")
    klass: ConditionClass
    detector: ConditionDetector
    message: str = ""
    recovery: Optional[RecoveryAction] = None
    # Which output(s), if any, a business outcome should surface as structured data
    surfaces_outputs: bool = False


# ---------------------------------------------------------------------------
# Contract: inputs, outputs, target, policy, provenance
# ---------------------------------------------------------------------------
class InputParameter(BaseModel):
    name: str
    type: ValueType = ValueType.STRING
    required: bool = True
    description: str = ""
    example: Optional[str] = None
    sensitive: bool = Field(
        default=False,
        description="Credentials / PII. Never persisted to artifact or logs; "
        "redacted everywhere. Supplied only at invocation time.",
    )


class OutputField(BaseModel):
    name: str
    type: ValueType = ValueType.STRING
    description: str = ""


class Target(BaseModel):
    """Separates the shared flow from its per-tenant binding.

    The steps/locators are the *shared* part; `base_url`, `tenant_id`,
    `vendor_product` and `version` are the *binding*. The same artifact can be
    re-pointed at another tenant running the same vendor product by supplying a
    different binding (plus optional overrides -- see REPORT section 4).
    """
    app_id: str
    surface_type: SurfaceType = SurfaceType.WEB
    base_url: str = Field(description="Tenant base URL, e.g. https://host[:port].")
    entry_path: str = "/"
    tenant_id: Optional[str] = None
    vendor_product: Optional[str] = Field(
        default=None, description="e.g. 'Corebank' -- the shared product family."
    )
    version: Optional[str] = None
    allowed_url_patterns: list[str] = Field(
        default_factory=list,
        description="Per-artifact allowlist of URL substrings/globs the flow may "
        "touch. Enforced by safety.policy in addition to any global policy.",
    )


class Provenance(BaseModel):
    """How this artifact came to be -- decoupled from the transcript itself."""
    recorded_at: Optional[str] = None
    recorded_by: str = Field(
        default="",
        description="Model / operator that drove discovery, e.g. 'llm-bridge'.",
    )
    discovery_run_id: Optional[str] = None
    transcript_ref: Optional[str] = Field(
        default=None,
        description="Path/URI to the (separately stored, redacted) transcript. "
        "The raw transcript is NOT inlined here.",
    )
    transcript_sha256: Optional[str] = None
    notes: str = ""


class StabilitySignal(BaseModel):
    """Optional replay-reliability signal (stretch): N runs -> pass rate."""
    runs: int = 0
    passes: int = 0


class CapabilityArtifact(BaseModel):
    """The top-level reusable capability contract."""
    schema_version: int = SCHEMA_VERSION
    id: str = Field(description="Stable slug, e.g. 'corebank.member_savings_lookup'.")
    name: str
    description: str
    version: str = Field(default="1.0.0", description="Semantic version of the flow.")

    target: Target
    inputs: list[InputParameter] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step]
    success: Checkpoint = Field(
        description="Top-level success condition asserted at the end of a run."
    )
    known_conditions: list[KnownCondition] = Field(default_factory=list)

    approval_state: ApprovalState = ApprovalState.DRAFT
    provenance: Provenance = Field(default_factory=Provenance)
    stability: Optional[StabilitySignal] = None

    # ---- convenience -----------------------------------------------------
    def secret_params(self) -> set[str]:
        return {p.name for p in self.inputs if p.sensitive}

    def output_names(self) -> list[str]:
        return [o.name for o in self.outputs]

    def to_json(self, **kw) -> str:
        return self.model_dump_json(indent=2, **kw)

    @classmethod
    def from_json(cls, text: str) -> "CapabilityArtifact":
        return cls.model_validate_json(text)

    def validate_inputs(self, params: dict[str, Any]) -> None:
        """Raise if required inputs are missing (called before replay)."""
        missing = [p.name for p in self.inputs if p.required and p.name not in params]
        if missing:
            raise ValueError(f"missing required inputs: {missing}")

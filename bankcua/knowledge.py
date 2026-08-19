"""
Curated, per-vendor libraries of KnownConditions (the error taxonomy, as data).

Why this lives outside any single artifact: runtime conditions (not-found,
permission denied, session timeout, maintenance interstitial, app error) are
properties of the *vendor product*, not of one recorded flow or one tenant.
Curating them once per vendor and attaching them to every artifact for that
vendor is exactly the cross-tenant reuse the brief asks for -- one place to
maintain "how Corebank signals a locked account," inherited by all tenants
running Corebank. A tenant that brands the text differently supplies an override
(see REPORT section 4); the code stays shared.

Classification follows the brief's three-way split:
  business_outcome -> a legitimate result the caller must be told about
  recoverable      -> handle in-band (dismiss/retry) then continue
  hard_failure     -> stop and surface a debuggable error
"""
from __future__ import annotations

from .schema import (
    ConditionClass,
    ConditionDetector,
    KnownCondition,
    Locator,
    LocatorCandidate,
    LocatorKind,
    RecoveryAction,
)

_CONTINUE_LINK = Locator(
    description="maintenance 'Continue to application' link",
    candidates=[LocatorCandidate(kind=LocatorKind.TEXT,
                                 value="Continue to application",
                                 reasoning="Stable link text on the gate page.")],
)

COREBANK_CONDITIONS: list[KnownCondition] = [
    KnownCondition(
        code="MEMBER_NOT_FOUND", klass=ConditionClass.BUSINESS_OUTCOME,
        detector=ConditionDetector(kind="text_present", value="No member found"),
        message="No member exists for the supplied ID.", surfaces_outputs=False),
    KnownCondition(
        code="PERMISSION_DENIED", klass=ConditionClass.BUSINESS_OUTCOME,
        detector=ConditionDetector(kind="text_present", value="Access Denied"),
        message="The signed-in user is not permitted to view this member/account."),
    KnownCondition(
        code="VALIDATION_ERROR", klass=ConditionClass.BUSINESS_OUTCOME,
        detector=ConditionDetector(kind="text_present", value="Validation error"),
        message="The submitted input failed the application's validation."),
    KnownCondition(
        code="MAINTENANCE_INTERSTITIAL", klass=ConditionClass.RECOVERABLE,
        detector=ConditionDetector(kind="text_present",
                                   value="System Maintenance Notice"),
        message="Unexpected maintenance gate; dismiss and continue.",
        recovery=RecoveryAction(kind="click", target=_CONTINUE_LINK,
                                max_attempts=1)),
    KnownCondition(
        code="APP_ERROR_500", klass=ConditionClass.RECOVERABLE,
        detector=ConditionDetector(kind="text_present", value="Application Error"),
        message="Transient application error; reload and retry.",
        recovery=RecoveryAction(kind="reload", max_attempts=2, backoff_ms=1500)),
    KnownCondition(
        code="SESSION_TIMEOUT", klass=ConditionClass.HARD_FAILURE,
        detector=ConditionDetector(kind="text_present", value="Session expired"),
        message="Session timed out; re-authentication required (surfaced to caller)."),
]

_LIBRARY = {"Corebank": COREBANK_CONDITIONS}


def conditions_for(vendor_product: str | None) -> list[KnownCondition]:
    if not vendor_product:
        return []
    # deep-copy via (de)serialise so callers can't mutate the shared library
    return [KnownCondition.model_validate(c.model_dump())
            for c in _LIBRARY.get(vendor_product, [])]

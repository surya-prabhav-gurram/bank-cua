"""
Turn a sentence into a capability invocation.

The decision this file exists to contain
----------------------------------------
Round 1's whole thesis was getting the model out of the production decision loop.
A conversational front door puts one back, and a reviewer should go straight at
that -- so the boundary matters more than the implementation.

What the model chooses here is WHICH TYPED CAPABILITY to invoke and with what
arguments. What it cannot do is decide how to drive the UI, invent an
interaction, name its own operator, or authorise an irreversible action: the
catalog is the entire action space, arguments are validated against the
capability's declared schema, and every guardrail downstream is unchanged and
server-side (see bankcua/service.py). A wrong routing decision produces a wrong
QUESTION, answered safely -- categorically different from a model clicking
buttons on a member's account.

The routing decision is also the only place a model appears in this path. The
capability it selects is then replayed deterministically with no model involved,
which is the same guarantee round 1 made.

The seam: swap `Router` and the chatbot changes its mind about how it
understands English, without touching the API, the capabilities, or the engine.
`RuleRouter` is deterministic and needs no key; `LLMRouter` uses tool-use over
the very manifest the API publishes. Both return the same `Routed` object, so
nothing downstream can tell which one ran.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Routed:
    """A resolved intent, or an honest failure to resolve one."""
    capability_id: Optional[str] = None
    params: dict[str, str] = field(default_factory=dict)
    operator: Optional[str] = None
    #: Arguments the capability requires that the sentence did not supply.
    missing: list[str] = field(default_factory=list)
    #: Why nothing was matched, phrased for the person who typed it.
    unmatched_reason: str = ""
    #: Notes about input that was PRESENT but unusable, so the reply can say why
    #: rather than reporting it as absent. "I still need a member number" reads
    #: like the user gave none, when in fact they gave one that cannot be a
    #: member number -- and they will simply retype it.
    rejected: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.capability_id) and not self.missing


class Router(abc.ABC):
    """Sentence -> capability call. The seam the chatbot is built on."""

    @abc.abstractmethod
    def route(self, message: str, manifest: list[dict]) -> Routed: ...


# ---------------------------------------------------------------------------
# Deterministic router
# ---------------------------------------------------------------------------
_MEMBER = re.compile(r"\b(\d{6})\b")
_SHARE = re.compile(r"\b(\d{6}-S\d{4}(?:-\d+)?)\b")
_MONEY = re.compile(r"(?:\$\s*)?(\d+(?:,\d{3})*(?:\.\d{1,2})?)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\b\d{3}-\d{4}\b")
_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")

#: Ordered most-specific-first: "transfer" must beat "balance" in a sentence that
#: mentions both, and the first match wins the same way replay's conditions do.
_INTENTS: list[tuple[str, re.Pattern]] = [
    ("meridian.transfer_funds", re.compile(r"\b(transfer|move)\b", re.I)),
    ("meridian.place_hold", re.compile(r"\b(hold|freeze|restrict)\b", re.I)),
    ("meridian.open_share", re.compile(r"\b(open|new)\b.{0,20}\bshare\b", re.I)),
    ("meridian.update_member_info",
     re.compile(r"\b(update|change|set)\b.{0,30}\b(email|e-mail|phone|address|contact)\b", re.I)),
    ("meridian.member_search",
     re.compile(r"\b(search|find|look\s*up)\b.{0,30}\b(name|named|surname|last name)\b",
                re.I)),
    ("meridian.member_lookup",
     re.compile(r"\b(balance|balances|shares|record|details|member|look\s*up|show)\b", re.I)),
    ("meridian.signon", re.compile(r"\b(sign\s*on|log\s*in|authenticate)\b", re.I)),
]

_REASON_CODES = {"fraud": "FRAUD", "legal": "LEGAL", "levy": "LEGAL",
                 "deceased": "DECEASED"}


class RuleRouter(Router):
    """Pattern matching over a small, closed intent set.

    Chosen as the default because it needs no API key and, more usefully, it
    cannot surprise anyone: the mapping from sentence to capability is readable
    in this file. It is deliberately narrow -- it recognises the seven recorded
    capabilities and says so plainly when it recognises nothing, rather than
    guessing at a capability that moves money.
    """

    def route(self, message: str, manifest: list[dict]) -> Routed:
        known = {c["name"]: c for c in manifest}
        cap_id = next((cid for cid, pat in _INTENTS
                       if cid in known and pat.search(message)), None)
        if cap_id is None:
            # The manifest is narrowed by whoever is signed in, so a sentence
            # can match an intent this person has no capability for. Saying that
            # plainly is the difference between "you may not do that" and "I did
            # not understand you" -- and the second answer sends someone into a
            # loop rephrasing a request that was understood perfectly well.
            withheld = next((cid for cid, pat in _INTENTS
                             if cid not in known and pat.search(message)), None)
            if withheld:
                return Routed(unmatched_reason=(
                    f"That reads as **{withheld}**, which is not available "
                    f"under your sign-in. I can only do what your account "
                    f"permits: {', '.join(sorted(known)) or 'nothing here'}."))
            return Routed(unmatched_reason=(
                "I can look up a member, search by last name, transfer funds, "
                "open a share, update contact details, or place a hold. I could "
                "not tell which of those you meant."))

        params = self._slots(cap_id, message)
        rejected = self._rejected(message, params)
        required = set(known[cap_id]["input_schema"].get("required") or [])
        # `operator`, `password` and `branch` are supplied by the service from the
        # operator's identity, never by the caller -- so they are not things the
        # sentence is expected to contain.
        supplied_by_service = {"operator", "password", "branch"}
        missing = sorted(required - supplied_by_service - set(params))
        return Routed(capability_id=cap_id, params=params, missing=missing,
                      rejected={k: v for k, v in rejected.items()
                                if k in missing})

    @staticmethod
    def _rejected(msg: str, params: dict) -> dict[str, str]:
        """Digit runs the sentence offered that cannot be a member number.

        Meridian member numbers are six digits and its search matches on
        SUBSTRING, so a shorter number silently resolves to a different member.
        Refusing here costs nothing and is the cheapest of the four places this
        is caught -- before a browser, before the API, before the taxonomy.
        """
        if "member_id" in params:
            return {}
        loose = re.findall(r"\b\d{1,9}\b", _SHARE.sub(" ", msg))
        candidate = next((n for n in loose if len(n) != 6), None)
        if candidate is None:
            return {}
        return {"member_id": (
            f"{candidate!r} is not a member number — they are six digits, and a "
            f"partial number matches several members on this system")}

    @staticmethod
    def _amount_text(msg: str) -> str:
        """The sentence with every identifier removed, so a number found in it is
        genuinely an amount.

        Shares are stripped BEFORE members, and the order is the whole point:
        stripping members first turns "100234-S0070" into "-S0070", which the
        share pattern can no longer match, leaving "0070" behind to be read as a
        monetary amount. A request that named no amount at all would then
        transfer $70.
        """
        return _MEMBER.sub(" ", _SHARE.sub(" ", msg))

    @staticmethod
    def _slots(cap_id: str, msg: str) -> dict[str, str]:
        out: dict[str, str] = {}
        shares = _SHARE.findall(msg)
        # A share id contains a member number, so members are read from the text
        # with share ids removed -- otherwise "100234-S0070" donates a member id
        # that was never actually named.
        stripped = _SHARE.sub(" ", msg)
        members = _MEMBER.findall(stripped)
        if members:
            out["member_id"] = members[0]
        elif shares:
            out["member_id"] = shares[0].split("-")[0]

        if cap_id == "meridian.member_search":
            quoted = _QUOTED.search(msg)
            if quoted:
                out["last_name"] = quoted.group(1)
            else:
                m = re.search(r"\b(?:named|name|surname|called)\s+([A-Za-z][\w'-]+)",
                              msg, re.I)
                if m:
                    out["last_name"] = m.group(1)
        elif cap_id == "meridian.transfer_funds":
            if len(shares) >= 2:
                out["from_share"], out["to_share"] = shares[0], shares[1]
            amounts = _MONEY.findall(RuleRouter._amount_text(msg))
            if amounts:
                out["amount"] = amounts[0].replace(",", "")
            out.setdefault("memo", "requested via assistant")
        elif cap_id == "meridian.open_share":
            amounts = _MONEY.findall(RuleRouter._amount_text(msg))
            if amounts:
                out["initial_deposit"] = amounts[0].replace(",", "")
            out["share_type"] = "SHARE"
        elif cap_id == "meridian.update_member_info":
            email = _EMAIL.search(msg)
            phone = _PHONE.search(msg)
            if email:
                out["email"] = email.group(0)
            if phone:
                out["phone"] = phone.group(0)
            quoted = _QUOTED.search(msg)
            if quoted:
                out["address"] = quoted.group(1)
        elif cap_id == "meridian.place_hold":
            if shares:
                out["share_id"] = shares[0]
            for word, code in _REASON_CODES.items():
                if re.search(rf"\b{word}\b", msg, re.I):
                    out["reason_code"] = code
                    break
            out.setdefault("notes", "requested via assistant")
        return out


# ---------------------------------------------------------------------------
# Model-backed router
# ---------------------------------------------------------------------------
def _drop_malformed_share_ids(params: dict[str, str]) -> tuple[dict, set]:
    """Discard a share id that does not name its member, and say it is missing.

    A model asked to move money out of "100234-S0070" has been observed to
    return `from_share: "S0070"` -- the suffix alone, which identifies no
    account. Passing that through would let the surface\'s prefix matching pick
    whichever option happened to start with it. Asking is the only safe answer,
    and it is the same rule the deterministic router follows: never invent or
    repair an account identifier.
    """
    member = (params.get("member_id") or "").strip()
    cleaned, malformed = dict(params), set()
    for key in ("from_share", "to_share", "share_id"):
        value = (params.get(key) or "").strip()
        if not value:
            continue
        if member and not value.startswith(member):
            cleaned.pop(key)
            malformed.add(key)
    return cleaned, malformed


class LLMRouter(Router):
    """Tool-use over the API's own published manifest.

    The manifest IS the tool list, which is what bounds this: the model can only
    emit a call to a capability that exists and is published, with arguments
    shaped by that capability's declared input schema. It is never shown a URL,
    a selector, or a password, and the service ignores anything it might say
    about permissions.

    Falls back to `RuleRouter` when no key is configured, so the demo path never
    depends on one.
    """

    def __init__(self, model: str | None = None):
        import anthropic
        import os
        self._client = anthropic.Anthropic()
        self._model = model or os.environ.get("LLM_MODEL")
        if not self._model:
            raise ValueError("no model id; pass --model or set LLM_MODEL")
        self._fallback = RuleRouter()

    SYSTEM = (
        "You route a bank operator's request to exactly one back-office "
        "capability. Choose the single tool that matches their intent and fill "
        "only the arguments they actually supplied.\n"
        "Never invent a member number, share id, or amount: if one is missing, "
        "omit it and it will be asked for.\n"
        "Share ids are written in full, as MEMBER-SHARE (for example "
        "100234-S0070). Never shorten one to its suffix -- a bare 'S0070' does "
        "not identify an account.\n"
        "If the request is not about member servicing at all, do not call any "
        "tool; answer briefly that you cannot help with it.\n"
        "You do not decide permissions, operators, or whether an action is "
        "allowed."
    )

    def route(self, message: str, manifest: list[dict]) -> Routed:
        tools = [{"name": c["name"].replace(".", "__"),
                  "description": c["description"],
                  "input_schema": c["input_schema"]} for c in manifest]
        try:
            resp = self._client.messages.create(
                model=self._model, max_tokens=512, system=self.SYSTEM,
                tools=tools,
                # `auto`, not `any`. Forcing a tool call means an off-topic
                # request still routes somewhere -- "what is the weather" became
                # a sign-on attempt against a bank. Declining is a valid answer,
                # and the manifest no longer asks for credentials, which was the
                # original reason the model answered in prose instead of calling
                # anything.
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": message}])
        except Exception:
            # A routing outage must degrade to the deterministic router rather
            # than take the whole front door down.
            return self._fallback.route(message, manifest)
        for block in resp.content:
            if block.type == "tool_use":
                cap_id = block.name.replace("__", ".")
                params = {k: str(v) for k, v in (block.input or {}).items()
                          if v not in (None, "")}
                params, malformed = _drop_malformed_share_ids(params)
                known = {c["name"]: c for c in manifest}
                required = set(known.get(cap_id, {}).get(
                    "input_schema", {}).get("required") or [])
                missing = sorted((required - {"operator", "password", "branch"}
                                  - set(params)) | malformed)
                return Routed(capability_id=cap_id, params=params, missing=missing)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return Routed(unmatched_reason=(
            text.strip() or "I could not match that to a capability."))

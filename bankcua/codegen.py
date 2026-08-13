"""
Code generation: emit a standalone, runnable Playwright script from an artifact.

This turns a capability into human-readable automation a developer can drop into
a test suite or run directly -- useful for review ("is this what it does?") and
for teams that prefer committed code over a JSON artifact. The generated script
uses each step's PRIMARY locator candidate; the replay engine additionally tries
the recorded fallbacks, so the JSON artifact remains the more robust executor.
"""
from __future__ import annotations

from .schema import ActionType, CapabilityArtifact, LocatorKind, Locator


def _loc_expr(loc: Locator) -> str:
    """Translate the primary locator candidate into a Playwright expression on
    a `ctx` (page or frame)."""
    c = loc.candidates[0]
    if c.kind == LocatorKind.ROLE:
        return f'ctx.get_by_role({c.role!r}, name={c.value!r})'
    if c.kind == LocatorKind.LABEL:
        return f'ctx.get_by_label({c.value!r})'
    if c.kind == LocatorKind.PLACEHOLDER:
        return f'ctx.get_by_placeholder({c.value!r})'
    if c.kind == LocatorKind.TEXT:
        return f'ctx.get_by_text({c.value!r})'
    if c.kind == LocatorKind.XPATH:
        return f'ctx.locator("xpath=" + {c.value!r})'
    return f'ctx.locator({c.value!r})'


def _frame_ctx(loc: Locator) -> str:
    if not loc.frame_path:
        return "page"
    ident = loc.frame_path[-1]
    return f'_frame(page, {ident!r})'


def generate_playwright_script(art: CapabilityArtifact) -> str:
    inputs = ", ".join(f"{p.name}: str" for p in art.inputs)
    lines: list[str] = []
    a = lines.append
    a('"""')
    a(f"Auto-generated Playwright automation for capability: {art.id} v{art.version}")
    a(f"{art.description}")
    a("")
    a("Generated from a bank-cua capability artifact. The JSON artifact + replay")
    a("engine remain the robust executor (they also try locator fallbacks and")
    a("classify runtime conditions); this script is a readable, runnable export.")
    a('"""')
    a("from playwright.sync_api import sync_playwright, expect")
    a("")
    a("BASE_URL = " + repr(art.target.base_url))
    a("")
    a("")
    a("def _frame(page, ident):")
    a("    for f in page.frames:")
    a("        if f.name == ident or ident in (f.url or ''):")
    a("            return f")
    a("    return page")
    a("")
    a("")
    a(f"def run({inputs}) -> dict:")
    a("    outputs = {}")
    a("    with sync_playwright() as p:")
    a("        browser = p.chromium.launch(headless=True)")
    a("        page = browser.new_page()")
    a(f"        page.goto(BASE_URL + {art.target.entry_path!r}, wait_until='load')")
    for s in art.steps:
        a(f"        # step {s.index}: {s.intent}")
        if s.action == ActionType.NAVIGATE:
            a(f"        page.goto(BASE_URL + f{s.url_template!r}, wait_until='load')")
        elif s.action == ActionType.PRESS:
            a(f"        page.keyboard.press({(s.key or 'Enter')!r})")
        elif s.action == ActionType.EXTRACT and s.extract:
            a(f"        ctx = {_frame_ctx(s.extract.locator)}")
            a(f"        outputs[{s.extract.output!r}] = "
              f"{_loc_expr(s.extract.locator)}.first.inner_text().strip()")
        elif s.target is not None:
            a(f"        ctx = {_frame_ctx(s.target)}")
            if s.action == ActionType.CLICK:
                a(f"        {_loc_expr(s.target)}.first.click()")
            elif s.action == ActionType.FILL:
                a(f"        {_loc_expr(s.target)}.first.fill({_value_literal(s)})")
            elif s.action == ActionType.SELECT:
                by = "label" if s.select_by == "label" else "value"
                a(f"        {_loc_expr(s.target)}.first.select_option("
                  f"{by}={_value_literal(s)})")
        if s.checkpoint and s.checkpoint.kind == "url_matches":
            a(f"        assert f{s.checkpoint.value!r} in page.url, 'checkpoint "
              f"failed at step {s.index}'")
    # success checkpoint
    if art.success.kind == "text_present":
        ctx = _frame_ctx_from_paths(art.success.frame_path)
        a(f"        assert {art.success.value!r} in {ctx}.content(), 'success checkpoint failed'")
    a("        browser.close()")
    a("    return outputs")
    a("")
    a("")
    a('if __name__ == "__main__":')
    a("    import sys, json")
    a("    args = dict(a.split('=', 1) for a in sys.argv[1:])")
    a("    print(json.dumps(run(**args), indent=2))")
    return "\n".join(lines) + "\n"


def _value_literal(step) -> str:
    """Render a step's value as an f-string referencing the param, or a literal."""
    v = step.value
    if v is None:
        return "''"
    if v.kind in ("param", "secret_param"):
        return f"{v.param}"
    return repr(v.literal or "")


def _frame_ctx_from_paths(frame_path) -> str:
    if not frame_path:
        return "page.main_frame"
    return f'_frame(page, {frame_path[-1]!r})'

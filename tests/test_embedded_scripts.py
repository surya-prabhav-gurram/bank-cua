"""
The pages this system serves have to actually parse.

Four modules embed an HTML page as a Python string, and three of them carry
real JavaScript. That arrangement has one failure mode nothing else here can
see: a quote inside the template gets un-escaped on its way through the Python
literal, closes a JS string early, and the browser discards the ENTIRE script.
The server is fine, the route returns 200, every test passes -- and the page
renders with an input box that silently does nothing when you press the button.

That is exactly what happened: `dashboard\\'s` in the source reached the browser
as `dashboard's`, and the assistant stopped responding to input with no error
anywhere a test could reach.

So: extract every inline script and parse it. Skipped where `node` is absent,
because a parser is not something this project should vendor -- but on any
machine with playwright installed there is one, and CI has it.
"""
import re
import shutil
import subprocess

import pytest

from bankcua.chat.app import _PAGE as CHAT_PAGE
from bankcua.dashboard import _INDEX_PAGE, _MEMBER_PAGE
from bankcua.portal.app import _LOGIN_PAGE, _SHELL_PAGE

PAGES = {
    "chat/app.py:_PAGE": CHAT_PAGE,
    "dashboard.py:_INDEX_PAGE": _INDEX_PAGE,
    "dashboard.py:_MEMBER_PAGE": _MEMBER_PAGE,
    "portal/app.py:_LOGIN_PAGE": _LOGIN_PAGE,
    "portal/app.py:_SHELL_PAGE": _SHELL_PAGE,
}

_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S)


def _node():
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not available to parse the embedded scripts")
    return exe


@pytest.mark.parametrize("name", sorted(PAGES))
def test_every_embedded_script_parses(name, tmp_path):
    """A page whose script does not parse is a page with no behaviour at all.

    Not "a broken button" -- the whole script is discarded, so nothing binds:
    the form does not submit, the polling never starts, and there is no error
    message anywhere except the browser console nobody has open.
    """
    node = _node()
    scripts = _SCRIPT.findall(PAGES[name])
    if not scripts:
        pytest.skip(f"{name} embeds no script")
    for index, body in enumerate(scripts):
        # Jinja placeholders are server-side; strip whole lines carrying one so
        # the parser sees the JavaScript rather than the template.
        source = "\n".join(line for line in body.splitlines()
                           if "{{" not in line and "{%" not in line)
        path = tmp_path / f"{index}.js"
        path.write_text(source)
        done = subprocess.run([node, "--check", str(path)],
                              capture_output=True, text=True)
        assert done.returncode == 0, (
            f"{name} script #{index} does not parse, so the browser will throw "
            f"the whole thing away and the page will do nothing:\n"
            f"{done.stderr.strip()}")


@pytest.mark.parametrize("name", sorted(PAGES))
def test_no_page_reaches_the_browser_with_a_template_placeholder(name):
    """`__PREFIX__` is substituted per request; anything else is a leak.

    A placeholder that survives into the served page is the same class of
    defect as the quote: it looks fine on the server and is nonsense in the
    browser.
    """
    leaked = [token for token in ("__PREFIX__",)
              if token in PAGES[name] and "chat" not in name
              and "dashboard" not in name]
    assert not leaked, f"{name} carries {leaked} but nothing substitutes it"

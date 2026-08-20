"""
Auto-generated Playwright automation for capability: corebank.member_savings_lookup v1.0.0
Sign on to the Corebank servicing console, search for a member by ID, and read the member's name and current savings balance from the account summary.

Generated from a bank-cua capability artifact. The JSON artifact + replay
engine remain the robust executor (they also try locator fallbacks and
classify runtime conditions); this script is a readable, runnable export.
"""
from playwright.sync_api import sync_playwright

BASE_URL = 'http://127.0.0.1:5057'


def _frame(page, ident):
    for f in page.frames:
        if f.name == ident or ident in (f.url or ''):
            return f
    return page


def run(username: str, password: str, member_id: str) -> dict:
    outputs = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL + '/login', wait_until='load')
        # step 0: Fill in the username field with the provided {username} credentials
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="User ID"]]//input').first.fill(username)
        # step 1: Fill in the password field to complete the login credentials
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Password"]]//input').first.fill(password)
        # step 2: Sign on to the system by clicking the Sign On button after credentials have been filled in
        ctx = page
        ctx.get_by_role('button', name='Sign On').first.click()
        assert '/home' in page.url, 'checkpoint failed at step 2'
        # step 3: Enter the member ID {member_id} into the search field to search for the member's account
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Member ID"]]//input').first.fill(member_id)
        # step 4: Submit the member search form to retrieve member {member_id}'s account information
        ctx = page
        ctx.get_by_role('button', name='Search').first.click()
        assert f'/member?mid={member_id}' in page.url, 'checkpoint failed at step 4'
        # step 5: Extract the member's name as required by the goal
        ctx = page
        outputs['member_name'] = ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Name"]]/td[last()]').first.inner_text().strip()
        # step 6: Extract the savings balance as required by the goal. The balance is <savings_balance> which needs to be normalized to integer cents (<savings_balance>).
        ctx = _frame(page, 'balancepane')
        outputs['savings_balance'] = ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Savings"]]/td[last()]').first.inner_text().strip()
        assert 'Savings' in _frame(page, 'balancepane').content(), 'success checkpoint failed'
        browser.close()
    return outputs


if __name__ == "__main__":
    import json
    import sys
    args = dict(a.split('=', 1) for a in sys.argv[1:])
    print(json.dumps(run(**args), indent=2))

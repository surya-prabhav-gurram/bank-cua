"""
Auto-generated Playwright automation for capability: corebank.open_subaccount v1.0.0
Sign on, open the sub-account form for a member, enter the account type and initial deposit, review, and confirm creation (irreversible) to reach the confirmation screen.

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


def run(username: str, password: str, member_id: str, acct_type: str, deposit: str) -> dict:
    outputs = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE_URL + '/login', wait_until='load')
        # step 0: Fill in the User ID field with the provided username credential
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="User ID"]]//input').first.fill(username)
        # step 1: Fill in the password field with the provided password credential
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Password"]]//input').first.fill(password)
        # step 2: Click the Sign On button to authenticate with the credentials that have already been filled in.
        ctx = page
        ctx.get_by_role('button', name='Sign On').first.click()
        assert '/home' in page.url, 'checkpoint failed at step 2'
        # step 3: Fill in the Member ID field with the provided member_id parameter to search for member {member_id}
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Member ID"]]//input').first.fill(member_id)
        # step 4: Click the Search button to search for member {member_id} whose ID has already been filled in from the previous step.
        ctx = page
        ctx.get_by_role('button', name='Search').first.click()
        assert f'/member?mid={member_id}' in page.url, 'checkpoint failed at step 4'
        # step 5: Click the "Open New Sub-Account" link to begin creating a new {acct_type} sub-account for member {member_id}.
        ctx = page
        ctx.get_by_role('link', name='Open New Sub-Account').first.click()
        assert f'/subaccount/new?mid={member_id}' in page.url, 'checkpoint failed at step 5'
        # step 6: Select '{acct_type}' as the account type from the dropdown menu using the provided acct_type parameter.
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Account Type"]]//select').first.select_option(label=acct_type)
        # step 7: Fill in the Initial Deposit field with the provided deposit amount of {deposit}
        ctx = page
        ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Initial Deposit"]]//input').first.fill(deposit)
        # step 8: Click the Review button to proceed to the review screen for the new sub-account with the already-filled details ({acct_type} account type and {deposit} initial deposit).
        ctx = page
        ctx.get_by_role('button', name='Review').first.click()
        assert '/subaccount/review' in page.url, 'checkpoint failed at step 8'
        # step 9: Click the 'Confirm and Create' button to complete the sub-account creation and reach the confirmation screen as required by the goal.
        ctx = page
        ctx.get_by_role('button', name='Confirm and Create').first.click()
        assert '/subaccount/confirm' in page.url, 'checkpoint failed at step 9'
        # step 10: Extract the confirmation number <confirmation_number> as required by the goal.
        ctx = page
        outputs['confirmation_number'] = ctx.locator("xpath=" + '//tr[td[normalize-space(.)="Confirmation"]]/td[last()]').first.inner_text().strip()
        assert 'Sub-Account Created' in page.main_frame.content(), 'success checkpoint failed'
        browser.close()
    return outputs


if __name__ == "__main__":
    import json
    import sys
    args = dict(a.split('=', 1) for a in sys.argv[1:])
    print(json.dumps(run(**args), indent=2))

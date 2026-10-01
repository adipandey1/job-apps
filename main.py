import argparse
import asyncio
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

from src.account_flow import ACCOUNT_EMAILS, ACCOUNT_PASSWORD, next_email_candidate
from src.filler import fill_form_fields, safe_count
from src.job_description import load_job_description
from src.profile import load_profile
from src.resume import resolve_resume_path
from src.site_flow import looks_like_apply_button

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"


def launch_kwargs() -> dict:
    headless = os.environ.get("HEADLESS", "").lower() in {"1", "true", "yes"}
    kwargs: dict = {
        "headless": headless,
        "args": [
            "--start-maximized",
            "--window-size=1400,900",
            "--window-position=0,0",
        ],
    }

    if os.geteuid() == 0:
        print(
            "WARNING: Running as root — the browser tile may be blank on i3.\n"
            "         Run as your normal user instead:  su - cifra\n"
            "         Or use HEADLESS=1 and open data/last-run.png\n"
        )
        kwargs["args"].append("--no-sandbox")

    if os.environ.get("DISPLAY") in (None, ""):
        print("Warning: DISPLAY is not set. GUI browser may not appear.")

    return kwargs


async def launch_browser(playwright):
    kwargs = launch_kwargs()

    for channel in ("chrome", "chromium", None):
        try:
            if channel:
                browser = await playwright.chromium.launch(channel=channel, **kwargs)
                print(f"Launched browser: {channel}")
            else:
                browser = await playwright.chromium.launch(**kwargs)
                print("Launched browser: playwright chromium")
            return browser
        except Exception as exc:
            if channel is None:
                raise
            print(f"Could not launch {channel}: {exc}")

    raise RuntimeError("No browser could be launched")


def print_actions(filled: list, skipped: list) -> None:
    if filled:
        print("Filled fields:")
        for action in filled:
            print(f"  [{action['key']}] {action['label']!r} = {action['value']!r}")
    else:
        print("No fields filled on this page.")

    if skipped:
        print("\nSkipped fields:")
        for action in skipped:
            detail = action.get("error", "unknown")
            print(f"  [{action.get('key', '?')}] {action['label']!r}: {detail}")


async def workday_error_page_is_present(page: Page) -> bool:
    """Detect common Workday fatal transition pages (e.g., VPS|... error)."""
    try:
        if await safe_count(page.get_by_text(re.compile(r"something went wrong", re.IGNORECASE))) > 0:
            return True
    except Exception:
        pass
    try:
        if await safe_count(page.get_by_text(re.compile(r"error code:\\s*VPS\\|", re.IGNORECASE))) > 0:
            return True
    except Exception:
        pass
    return False


async def wait_for_step_transition(page: Page) -> bool:
    """Let Workday settle after a Next/Continue click.

    Returns False if a Workday VPS error page is detected; recovery is handled
    by the caller to avoid hidden repeated submissions.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except Exception:
        pass
    await page.wait_for_timeout(1200)
    return not await workday_error_page_is_present(page)


async def try_click_advance_control(page: Page, element, label: str) -> str:
    """Try one safe click on an advance control.

    Returns: 'advanced', 'not_ready', 'transition_error', 'click_error'.
    """
    try:
        await element.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass

    enabled = False
    for _ in range(12):
        try:
            if await element.is_enabled():
                aria_disabled = ((await element.get_attribute("aria-disabled", timeout=500)) or "").strip().lower()
                if aria_disabled not in {"true", "1"}:
                    enabled = True
                    break
        except Exception:
            pass
        await page.wait_for_timeout(500)

    if not enabled:
        print(f"[Auto-Next] Candidate matched but is disabled/unready: {label!r}")
        return "not_ready"

    # Give Workday a moment to finish in-page saves/validation.
    await page.wait_for_timeout(1000)

    try:
        print(f"[Auto-Next] Attempting advance via candidate: {label!r}")
        await element.click(timeout=3000)
    except Exception:
        return "click_error"

    if await wait_for_step_transition(page):
        print(f"Auto-advanced to next step via: {label!r}")
        return "advanced"

    print("[Auto-Next] Step transition landed on Workday error page.")
    return "transition_error"


async def recover_from_workday_transition_error(page: Page, attempt: int, max_attempts: int) -> bool:
    """Recover from a Workday VPS transition error without blind resubmits."""
    if not await workday_error_page_is_present(page):
        return True

    wait_ms = 3000 + (attempt * 2000)
    print(
        f"[Auto-Next] Recovering from Workday VPS error "
        f"({attempt + 1}/{max_attempts}) — back, wait {wait_ms}ms, retry..."
    )

    try:
        await page.go_back(timeout=15000)
    except Exception:
        return False

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=7000)
    except Exception:
        pass
    await page.wait_for_timeout(wait_ms)
    return not await workday_error_page_is_present(page)


async def click_next_step(page: Page, page_num: int, max_pages: int) -> bool:
    """Advance to the next non-submit step automatically.

    Safety invariant: never auto-click final submission actions.
    """
    if page_num >= max_pages:
        return False

    if page_num == 1:
        # Right after resume upload, some Workday tenants briefly expose a
        # transient plain "Continue" state. Clicking that can trigger the VPS
        # error loop. For the first page, only proceed once the true
        # "Save and Continue" footer action is present.
        save_continue_selector = page.locator(
            "button:has-text('Save and Continue'):visible, "
            "input[type='submit'][value*='Save and Continue' i]:visible, "
            "input[type='button'][value*='Save and Continue' i]:visible"
        )
        save_continue_ready = False
        for _ in range(20):
            if await safe_count(save_continue_selector) > 0:
                save_continue_ready = True
                break
            await page.wait_for_timeout(1500)
        if not save_continue_ready:
            print("[Auto-Next] Page 1 never reached stable 'Save and Continue' state; stopping to avoid VPS loop.")
            return False

    positive = re.compile(
        r"\b(next|continue|review|save\s*(and)?\s*continue|proceed)\b",
        re.IGNORECASE,
    )
    negative = re.compile(
        r"\b(submit|apply|finish|sign in|create account|autofill)\b",
        re.IGNORECASE,
    )

    # First pass: strongly prefer the explicit Workday footer actions in
    # priority order. This avoids accidentally choosing some other generic
    # "Continue" control elsewhere on the page.
    preferred_by_text = [
        re.compile(r"^\s*save\s+and\s+continue\s*$", re.IGNORECASE),
        re.compile(r"^\s*next\s*$", re.IGNORECASE),
    ]
    for attempt in range(3):
        for pattern in preferred_by_text:
            text_matches = page.get_by_role("button", name=pattern)
            text_count = await safe_count(text_matches)
            for i in range(text_count - 1, -1, -1):
                element = text_matches.nth(i)
                try:
                    label = (await element.inner_text(timeout=500)).strip() or "Continue"
                except Exception:
                    label = "Continue"
                try:
                    result = await try_click_advance_control(page, element, label)
                    if result == "advanced":
                        return True
                    if result == "transition_error":
                        recovered = await recover_from_workday_transition_error(page, attempt, 3)
                        if recovered:
                            continue
                        return False
                except Exception:
                    continue

    preferred_candidates = page.locator(
        "button[data-automation-id*='next' i]:visible, "
        "button[data-automation-id*='continue' i]:visible, "
        "button[data-automation-id*='save' i]:visible"
    )
    preferred_count = await safe_count(preferred_candidates)

    # Always scan full clickable controls as a superset; some Workday tenants
    # render footer actions as input[type=submit]/input[type=button].
    candidates = page.locator(
        "button:visible, [role='button']:visible, "
        "input[type='button']:visible, input[type='submit']:visible"
    )

    count = await safe_count(candidates)
    print(
        f"[Auto-Next] Found {count} clickable control candidates on page {page_num} "
        f"({'preferred' if preferred_count > 0 else 'fallback'} set)."
    )
    has_save_and_continue = await safe_count(
        page.locator(
            "button:has-text('Save and Continue'):visible, "
            "input[type='submit'][value*='Save and Continue' i]:visible, "
            "input[type='button'][value*='Save and Continue' i]:visible"
        )
    ) > 0
    debug_labels: list[str] = []

    for i in range(count):
        element = candidates.nth(i)
        try:
            label = (await element.inner_text(timeout=500)).strip()
        except Exception:
            label = ""
        if not label:
            try:
                label = (await element.get_attribute("value", timeout=500) or "").strip()
            except Exception:
                label = ""
        if not label:
            try:
                label = (await element.get_attribute("aria-label", timeout=500) or "").strip()
            except Exception:
                label = ""
        if not label:
            continue

        if len(debug_labels) < 20:
            debug_labels.append(label)

        if positive.search(label) and not negative.search(label):
            if page_num == 1 and re.fullmatch(r"\s*continue\s*", label, re.IGNORECASE):
                print("[Auto-Next] Skipping ambiguous plain 'Continue' on page 1; waiting for stable footer action.")
                continue
            try:
                result = await try_click_advance_control(page, element, label)
                if result == "advanced":
                    return True
                if result == "transition_error":
                    recovered = await recover_from_workday_transition_error(page, 0, 1)
                    if recovered:
                        continue
                    return False
            except Exception:
                continue

    if debug_labels:
        print(f"[Auto-Next] Candidate labels sampled: {debug_labels}")
    print("No safe Next/Continue step found — stopping before final submit.")
    return False


async def wait_for_user_next(page_num: int, max_pages: int) -> bool:
    """Pause so the user can review and click Next/Save and Continue manually.

    Returns False when the user enters 'q' or when max page bound is reached.
    """
    if page_num >= max_pages:
        return False

    print("\n" + "-" * 50)
    print("Review the form in the browser.")
    print("Click Next/Save and Continue when ready, then press Enter here.")
    print("Type 'q' + Enter to stop.")
    print("-" * 50)

    response = await asyncio.get_event_loop().run_in_executor(
        None, lambda: input("> ").strip().lower()
    )
    return response != "q"


async def fill_all_pages(
    page: Page,
    profile: dict,
    job_description: str,
    max_pages: int,
) -> tuple[list, list]:
    all_filled: list = []
    all_skipped: list = []

    for page_num in range(1, max_pages + 1):
        print(f"\n{'=' * 50}")
        print(f"Page {page_num}")
        print(f"{'=' * 50}")

        filled, skipped = await fill_form_fields(page, profile, job_description=job_description)
        if not filled and not skipped:
            # Workday can take a few seconds to hydrate page fields after
            # resume parsing or step transitions. If a page appears empty,
            # settle briefly and retry once before attempting auto-next.
            print("[Auto-Next] Page appears empty; waiting for fields to hydrate and retrying...")
            await page.wait_for_timeout(4000)
            retry_filled, retry_skipped = await fill_form_fields(
                page,
                profile,
                job_description=job_description,
            )
            if retry_filled or retry_skipped:
                filled = retry_filled
                skipped = retry_skipped

        all_filled.extend(filled)
        all_skipped.extend(skipped)
        print_actions(filled, skipped)

        if not await wait_for_user_next(page_num, max_pages):
            print("\nStopping — review and submit manually in the browser.")
            break

    return all_filled, all_skipped


async def find_and_click_apply_button(page: Page) -> tuple[bool, dict | None]:
    """Find and click the Apply button/link. Returns (clicked, href_info) where
    href_info (captured BEFORE clicking) holds the element's href/target if it
    is an anchor tag — used as a last-resort direct-navigation fallback by the
    caller in case the site's own click handler never actually navigates."""
    candidates = page.locator("button:visible, a:visible, [role='button']:visible")
    count = await safe_count(candidates)
    print(f"[Apply] Searching for Apply button among {count} candidates...")

    for i in range(count):
        element = candidates.nth(i)
        try:
            label = (await element.inner_text(timeout=500)).strip()
        except Exception:
            label = ""

        if not label:
            try:
                label = (await element.get_attribute("value", timeout=500) or "").strip()
            except Exception:
                label = ""

        if not label:
            try:
                label = (await element.get_attribute("aria-label", timeout=500) or "").strip()
            except Exception:
                label = ""

        if looks_like_apply_button(label):
            print(f"[Apply] ✓ Found: {label}")
            try:
                href_info = await element.evaluate(
                    "el => el.tagName === 'A' ? {href: el.getAttribute('href'), target: el.getAttribute('target')} : null"
                )
            except Exception:
                href_info = None
            try:
                await element.click(force=True)
            except Exception as e:
                print(f"[Apply] Error clicking: {e}")
                return False, href_info
            return True, href_info

    print("[Apply] ✗ No Apply button found")
    return False, None


async def handle_apply_modal_if_present(page: Page) -> None:
    """Workday postings open a "Start Your Application" modal (Autofill with
    Resume / Apply Manually / Use My Last Application) on the SAME tab rather
    than navigating away. Only call this once we know no new tab opened."""
    autofill_btn = page.get_by_role("button", name=re.compile("autofill with resume", re.IGNORECASE)).first
    try:
        await autofill_btn.wait_for(state="visible", timeout=4000)
        print("[Apply] ✓ Found: Autofill with Resume")
        await autofill_btn.click(force=True)
        await page.wait_for_timeout(2000)
    except Exception:
        print("[Apply] No 'Autofill with Resume' modal option appeared (may have navigated directly)")


async def click_apply_button_and_follow_new_tab(page: Page) -> Page:
    """Click the Apply button, then react to whichever happens first: a new tab
    opening (iCIMS, Eightfold, Greenhouse-embedded, etc.) or a same-tab modal
    appearing (Workday's "Start Your Application"). Polling for both at once
    (instead of waiting out a fixed ~10s modal timeout before ever checking for
    a new tab) avoids a long dead-looking pause when the new tab opens quickly.

    Many ATSes open the actual application form in a NEW browser tab rather
    than navigating the current one. If we keep operating on the stale original
    tab, every subsequent step (account flow, form fill, resume upload) silently
    finds nothing — and generic fallback searches on that stale marketing page
    can click the wrong thing entirely (e.g. a nav link).
    """
    context = page.context
    pages_before = set(context.pages)

    clicked, href_info = await find_and_click_apply_button(page)
    if not clicked:
        return page

    print("[Apply] Clicked — watching for a new tab or an in-page modal...")
    new_page = None
    for _ in range(20):  # poll for up to ~4s
        new_pages = [p for p in context.pages if p not in pages_before]
        if new_pages:
            new_page = new_pages[-1]
            break
        if await safe_count(page.get_by_role("dialog")) > 0:
            break
        await page.wait_for_timeout(200)

    if new_page:
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await new_page.bring_to_front()
        print(f"[Apply] New tab opened by Apply — switching to it: {new_page.url}")
        return new_page

    await handle_apply_modal_if_present(page)

    # The modal path (or a same-tab navigation) can itself open a new tab too —
    # check once more before giving up and continuing on the original page.
    new_pages = [p for p in context.pages if p not in pages_before]
    if new_pages:
        new_page = new_pages[-1]
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await new_page.bring_to_front()
        print(f"[Apply] New tab opened by Apply — switching to it: {new_page.url}")
        return new_page

    # Last resort: some sites gate the real navigation behind an analytics
    # call (e.g. "track click, then on .then() navigate") that can silently
    # stall under automation — the button's href is correct but neither a new
    # tab nor a same-tab navigation ever happens. Follow the captured href
    # directly rather than leaving the user stuck on the original page.
    href = (href_info or {}).get("href") if href_info else None
    if href:
        target = ((href_info or {}).get("target") or "").lower()
        print(f"[Apply] No navigation detected after click — following Apply link directly: {href}")
        if target == "_blank":
            # context.new_page() is disallowed when the context was created via
            # the browser.new_page() convenience method (as ours is) — go
            # through the Browser object directly to open an independent tab.
            new_page = await page.context.browser.new_page()
            try:
                await new_page.goto(href, wait_until="domcontentloaded", timeout=15000)
                await handle_apply_modal_if_present(new_page)
                await new_page.bring_to_front()
                return new_page
            except Exception as e:
                print(f"[Apply] Direct navigation failed: {e}")
                await new_page.close()
                return page
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=15000)
            # Landing here can itself show the same "Start Your Application"
            # modal (Autofill / Apply Manually / Use My Last Application) that
            # handle_apply_modal_if_present already looked for once before —
            # but that first check ran on the stale pre-navigation page, so it
            # needs to run again now that the real apply page has loaded.
            await handle_apply_modal_if_present(page)
        except Exception as e:
            print(f"[Apply] Direct navigation failed: {e}")
        return page

    return page



def account_email_locator(page: Page):
    """Workday's email field is a plain <input> with no type=email/name=email —
    it's only identifiable by its accessible name. Matching on type/name (as
    older code did) silently fails and can also catch the hidden honeypot
    input (name='website')."""
    return page.get_by_role("textbox", name=re.compile(r"email", re.IGNORECASE)).filter(
        has_not=page.locator("[name='website']")
    )


def account_password_locator(page: Page):
    return page.locator("input[type='password']:visible")


async def submit_account_form(page: Page) -> bool:
    """Click the real submit button (Submit / Create Account), never the honeypot
    or decorative text. Prefers exact role-based matches."""
    for name in ("Submit", "Create Account", "Sign In"):
        btn = page.get_by_role("button", name=name, exact=True).first
        if await safe_count(btn) > 0:
            try:
                await btn.click(force=True, timeout=3000)
                return True
            except Exception:
                continue

    # Fallback: any visible button whose text loosely matches.
    buttons = page.locator("button:visible")
    for i in range(await safe_count(buttons)):
        try:
            btn = buttons.nth(i)
            text = (await btn.inner_text(timeout=500)).strip().lower()
            if any(word in text for word in ("submit", "sign in", "create")):
                await btn.click(force=True, timeout=3000)
                return True
        except Exception:
            continue
    return False


async def fill_and_submit_account_credentials(page: Page, email: str) -> bool:
    """Fill email/password(+verify)/checkbox on whichever account form is showing
    (Sign In or Create Account look the same structurally) and submit.
    Returns True once submitted (caller checks whether it actually progressed)."""
    email_input = account_email_locator(page).first
    password_inputs = account_password_locator(page)

    if await safe_count(email_input) == 0 or await safe_count(password_inputs) == 0:
        return False

    print(f"[Account Flow] Filling form with {email}")
    try:
        await email_input.fill(email, timeout=3000)
        await password_inputs.first.fill(ACCOUNT_PASSWORD, timeout=3000)
    except Exception as exc:
        print(f"[Account Flow] Could not fill email/password: {exc}")
        return False

    # Verify-password field present on Create Account forms.
    if await safe_count(password_inputs) >= 2:
        try:
            await password_inputs.nth(1).fill(ACCOUNT_PASSWORD, timeout=3000)
        except Exception:
            pass

    # Privacy-notice checkbox, if present and unchecked.
    try:
        checkbox = page.locator("input[type='checkbox']:visible").first
        if await safe_count(checkbox) > 0 and not await checkbox.is_checked(timeout=500):
            await checkbox.click(force=True)
    except Exception:
        pass

    submitted = await submit_account_form(page)
    if submitted:
        await page.wait_for_timeout(3000)
    return submitted


async def account_form_is_present(page: Page) -> bool:
    return (
        await safe_count(account_email_locator(page)) > 0
        or await safe_count(account_password_locator(page)) > 0
    )


async def email_verification_prompt_is_present(page: Page) -> bool:
    """Detects Workday's "we emailed you a verification code" step. This can
    appear right after Sign In or after Create Account, and requires checking a
    real inbox that only a human can access — the script cannot complete it."""
    try:
        if await safe_count(page.get_by_text(re.compile(r"verif(y|ication)", re.IGNORECASE))) > 0:
            return True
    except Exception:
        pass
    try:
        code_input = page.get_by_role("textbox", name=re.compile(r"\bcode\b", re.IGNORECASE))
        if await safe_count(code_input) > 0:
            return True
    except Exception:
        pass
    return False


async def pause_for_manual_verification(page: Page) -> None:
    print(
        "\n[Account Flow] WARNING: Email verification required — Workday emailed "
        "a code to the account's inbox, which only a human can retrieve.\n"
        "Check the inbox, enter the code in the browser, and complete verification.\n"
        "Press Enter here once verification is done to continue."
    )
    await asyncio.get_event_loop().run_in_executor(None, input)


async def wait_for_account_form(page: Page, timeout_ms: int = 8000) -> bool:
    """Poll for the sign-in/create-account form to actually render. Some Workday
    tenants take several seconds to paint this form after a tab switch — a fixed
    short wait can miss it, after which the generic filler (which has no concept
    of the account gate) grabs the Email field first and fills it with the real
    profile email instead of a throwaway account address."""
    elapsed = 0
    interval = 300
    while elapsed < timeout_ms:
        if await account_form_is_present(page):
            return True
        await page.wait_for_timeout(interval)
        elapsed += interval
    return await account_form_is_present(page)


async def confirm_signed_in(page: Page, settle_ms: int = 2500) -> bool:
    """Right after submitting Sign In/Create Account, the email field can
    momentarily disappear mid-SPA-transition even when sign-in actually failed
    and the SAME account gate re-renders moments later (e.g. Workday bounces
    back to a fresh, empty Sign In form). A single instantaneous
    'no email field' check is not reliable proof of success — settle for a bit
    and re-check that the gate is genuinely, stably gone before declaring
    success."""
    if await safe_count(account_email_locator(page)) > 0:
        return False
    await page.wait_for_timeout(settle_ms)
    return not await account_form_is_present(page)


async def handle_account_flow(page: Page) -> str | None:
    """Complete the Workday sign-in/create-account gate, trying Sign In first
    (the account may already exist from a previous run) and falling back to
    Create Account with the next email candidate if Sign In is rejected.
    Returns the email that ended up signed in, or None on failure.

    IMPORTANT invariant: once Workday has asked a human to verify a given
    email (via pause_for_manual_verification), we commit to that email and
    keep retrying Sign In with it — we never cycle to a different throwaway
    email afterwards. Abandoning a half-verified email and moving to a new
    one would just trigger ANOTHER verification email on a fresh inbox,
    multiplying the manual work for no benefit, since the original account
    is presumably still there waiting to be signed into.
    """
    print("\n[Account Flow] Starting sign-in flow...")

    signin_btn = page.locator("button:has-text('Sign in with email')").first
    if await safe_count(signin_btn) > 0:
        print("[Account Flow] Clicking 'Sign in with email'...")
        try:
            await signin_btn.click(force=True)
            await page.wait_for_timeout(1500)
        except Exception:
            pass

    current_email = ACCOUNT_EMAILS[0]
    verification_requested_for: set[str] = set()

    for attempt in range(len(ACCOUNT_EMAILS)):
        await wait_for_account_form(page, timeout_ms=8000 if attempt == 0 else 2000)
        submitted = await fill_and_submit_account_credentials(page, current_email)

        if not submitted:
            print(f"[Account Flow] Form not found/fillable (attempt {attempt + 1}/{len(ACCOUNT_EMAILS)})")
            await page.wait_for_timeout(500)
            current_email = next_email_candidate(current_email)
            continue

        if await email_verification_prompt_is_present(page):
            verification_requested_for.add(current_email)
            await pause_for_manual_verification(page)

        if await confirm_signed_in(page):
            print(f"[Account Flow] ✓ Signed in with {current_email}")
            return current_email

        # Only attempt Create Account if we haven't already asked a human to
        # verify this email — once verification has been requested, the
        # account already exists, so every subsequent attempt must be a plain
        # Sign In retry, never another Create Account submission.
        create_btn = page.get_by_role("button", name="Create Account", exact=True).first
        if current_email not in verification_requested_for and await safe_count(create_btn) > 0:
            print("[Account Flow] Sign In rejected — switching to Create Account...")
            try:
                await create_btn.click(force=True)
                await page.wait_for_timeout(1500)
            except Exception:
                pass
            submitted = await fill_and_submit_account_credentials(page, current_email)

            if await email_verification_prompt_is_present(page):
                verification_requested_for.add(current_email)
                await pause_for_manual_verification(page)

            if submitted and await confirm_signed_in(page):
                print(f"[Account Flow] ✓ Account created and signed in with {current_email}")
                return current_email

        # Committed-retry loop: keeps retrying Sign In with the SAME email,
        # used both for "bounced back to Sign In after Create Account" and
        # for "still needs re-sign-in after a human just verified". Distinguish
        # a genuine Create Account form (2 password fields: Password + Verify
        # Password) from a Sign In form (1 password field) by count, since
        # both forms render a same-text toggle link ("Create Account" appears
        # both as the Create Account form's submit button AND as the Sign In
        # form's "Don't have an account yet? Create Account" link) — matching
        # on button text alone is ambiguous.
        #
        # (By this point confirm_signed_in() has already returned False above,
        # so we know the account gate is genuinely still present.)
        for retry in range(5):
            if not await account_form_is_present(page):
                break
            if await safe_count(account_password_locator(page)) >= 2:
                break  # still a genuine Create Account form — not our case
            print(
                f"[Account Flow] Retrying Sign In with {current_email} "
                f"({retry + 1}/5)..."
            )
            submitted = await fill_and_submit_account_credentials(page, current_email)

            if await email_verification_prompt_is_present(page):
                verification_requested_for.add(current_email)
                await pause_for_manual_verification(page)

            if submitted and await confirm_signed_in(page):
                print(f"[Account Flow] ✓ Signed in with {current_email}")
                return current_email
            await page.wait_for_timeout(500)

        if current_email in verification_requested_for:
            # We already asked a human to verify this exact email — do not
            # abandon it for a different throwaway address. Report stuck
            # so the caller can pause for full manual resolution instead.
            print(
                f"[Account Flow] ✗ Still stuck on the account gate for "
                f"{current_email} after verification — needs manual resolution."
            )
            return None

        print("[Account Flow] Still on account gate, trying next email...")
        current_email = next_email_candidate(current_email)
        await page.wait_for_timeout(300)

    print("[Account Flow] ✗ Failed to complete account flow")
    return None


RESUME_UPLOAD_PATTERNS = (
    re.compile(r"\bresume\b", re.IGNORECASE),
    re.compile(r"\bcv\b", re.IGNORECASE),
    re.compile(r"\bupload\b", re.IGNORECASE),
    re.compile(r"\battach(?:ment)?\b", re.IGNORECASE),
    re.compile(r"\bcover letter\b", re.IGNORECASE),
)


def looks_like_resume_upload_trigger(label: str) -> bool:
    """Word-boundary match only — a bare "cover" substring also matches unrelated
    site navigation like "Coverage" (T-Mobile's nav has a "Coverage" menu item),
    which previously caused the script to click away from the application page."""
    text = (label or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in RESUME_UPLOAD_PATTERNS)


async def upload_resume_if_present(page: Page, resume_path: Path | None = None) -> Path | None:
    if resume_path is None:
        resume_path = resolve_resume_path()
    if resume_path is None:
        print("No resume file found automatically. Pass --resume /path/to/resume.pdf to attach one.")
        return None

    resume_path = Path(resume_path).expanduser()
    if not resume_path.exists():
        print(f"Resume path not found: {resume_path}")
        return None

    file_inputs = page.locator("input[type='file']")
    if await safe_count(file_inputs) == 0:
        buttons = page.locator("button:visible, a:visible, [role='button']:visible")
        button_count = await safe_count(buttons)
        for i in range(button_count):
            element = buttons.nth(i)
            try:
                label = (await element.inner_text()).strip().lower()
            except Exception:
                label = ""
            if not looks_like_resume_upload_trigger(label):
                continue
            # Site navigation/header links (e.g. a "Coverage" menu item) can contain
            # loose substrings like "cover" — never click anything inside nav/header/
            # footer chrome, resume-upload triggers only ever live inside the form.
            try:
                in_site_chrome = await element.evaluate("el => !!el.closest('nav, header, footer')")
            except Exception:
                in_site_chrome = False
            if in_site_chrome:
                continue
            await element.click()
            await page.wait_for_timeout(1000)
            break

    if await safe_count(file_inputs) == 0:
        print(f"Found resume file but no file-upload input was visible on the page: {resume_path}")
        return None

    await file_inputs.first.set_input_files(str(resume_path))
    print(f"Uploaded resume: {resume_path}")
    return resume_path


async def run(
    url: str,
    job_path: Path | None = None,
    resume_path: Path | None = None,
    max_pages: int = 10,
) -> None:
    load_dotenv()
    profile = load_profile()
    job_description = load_job_description(job_path)
    contact = profile["contact"]
    print(f"Loaded profile for {contact.get('full_name', contact.get('first_name', 'unknown'))}")
    if job_description:
        print("Job description loaded for LLM context")
    if resume_path is None:
        auto_resume = resolve_resume_path()
        if auto_resume:
            print(f"Auto-detected resume: {auto_resume}")
            resume_path = auto_resume
    print(f"Opening {url}")
    print(f"Max pages: {max_pages} (you click Next/Save and Continue; script never auto-clicks)\n")

    if not os.environ.get("HEADLESS"):
        print("If you don't see the browser on i3, check other workspaces (Mod+1..9) or Mod+j/k.\n")

    async with async_playwright() as p:
        browser = await launch_browser(p)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        await page.goto(url, wait_until="domcontentloaded")
        await page.bring_to_front()
        await page.wait_for_timeout(2000)

        page = await click_apply_button_and_follow_new_tab(page)
        await page.wait_for_timeout(1000)
        signed_in_email = await handle_account_flow(page)

        if signed_in_email is None and await wait_for_account_form(page, timeout_ms=3000):
            print(
                "\n[Account Flow] WARNING: still on the sign-in/create-account gate — "
                "skipping resume upload and form fill to avoid corrupting the account form.\n"
                "Resolve sign-in manually in the browser, then press Enter to continue."
            )
            await asyncio.get_event_loop().run_in_executor(None, input)

        # Don't call fill_workday_my_information_form here - it can interfere with sign-in
        # The generic fill_all_pages will handle all form fields
        
        await upload_resume_if_present(page, resume_path)


        all_filled, all_skipped = await fill_all_pages(
            page, profile, job_description, max_pages=max_pages
        )

        print(f"\n{'=' * 50}")
        print(f"Done — filled {len(all_filled)} fields across all pages")
        print(f"{'=' * 50}")

        screenshot_path = DATA_DIR / "last-run.png"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"\nScreenshot saved to {screenshot_path}")
        except OSError as exc:
            fallback = Path("/tmp/job-app-agent-last-run.png")
            await page.screenshot(path=str(fallback), full_page=True)
            print(f"\nCould not write {screenshot_path} ({exc})")
            print(f"Screenshot saved to {fallback}")

        if os.environ.get("HEADLESS"):
            print("HEADLESS mode — open the screenshot to review the form.")
        else:
            print("\nBrowser left open for review. Press Enter to close.")
            await asyncio.get_event_loop().run_in_executor(None, input)

        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-fill job application forms")
    parser.add_argument("url", help="Job application form URL")
    parser.add_argument(
        "--job",
        type=Path,
        help="Job description text file for LLM essay answers (default: data/job-description.txt)",
    )
    parser.add_argument(
        "--resume",
        type=Path,
        help="Resume file to upload automatically (PDF/DOC/DOCX). Defaults to the best match under the parent folder.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum pages to fill (default: 10). Never clicks Submit.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.url, job_path=args.job, resume_path=args.resume, max_pages=args.max_pages))


if __name__ == "__main__":
    main()

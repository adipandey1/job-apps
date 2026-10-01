from src.account_flow import ACCOUNT_EMAILS, looks_like_account_action, next_email_candidate


def test_account_actions_are_detected():
    assert looks_like_account_action("Sign in with email") is True
    assert looks_like_account_action("Create Account") is True
    assert looks_like_account_action("Don't have an account yet? Create Account") is True
    assert looks_like_account_action("Already have an account? Sign In") is True
    assert looks_like_account_action("Forgot password?") is True


def test_email_fallback_cycle():
    assert next_email_candidate(ACCOUNT_EMAILS[0]) == ACCOUNT_EMAILS[1]
    assert next_email_candidate(ACCOUNT_EMAILS[2]) == ACCOUNT_EMAILS[0]

from __future__ import annotations

ACCOUNT_EMAILS = [
    "adspds0011@gmail.com",
    "adspds009@gmail.com",
    "adspds007@gmail.com",
]
ACCOUNT_PASSWORD = "Mom1970$$$$"


def normalize_label(value: str) -> str:
    return (value or "").strip().lower()


def looks_like_account_action(label: str) -> bool:
    text = normalize_label(label)
    if not text:
        return False
    patterns = (
        "sign in with email",
        "create account",
        "create an account",
        "sign up",
        "continue with email",
        "already have an account",
        "i have an account",
        "don't have an account yet",
        "dont have an account yet",
        "forgot password",
    )
    return any(pattern in text for pattern in patterns)


def next_email_candidate(current_email: str | None = None) -> str:
    emails = ACCOUNT_EMAILS
    if current_email:
        try:
            index = emails.index(current_email)
            return emails[(index + 1) % len(emails)]
        except ValueError:
            return emails[0]
    return emails[0]

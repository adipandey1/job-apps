def looks_like_apply_button(label: str) -> bool:
    text = (label or "").strip().lower()
    if not text:
        return False
    apply_aliases = (
        "apply",
        "apply to this job",
        "continue to application",
        "continue to application form",
        "submit application",
        "begin application",
        "next: application",
        "autofill with resume",
        "apply manually",
        "use my last application",
        "start your application",
    )
    return any(alias in text for alias in apply_aliases)

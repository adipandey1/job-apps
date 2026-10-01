from src.site_flow import looks_like_apply_button


def test_apply_button_matches_common_labels():
    assert looks_like_apply_button("Apply") is True
    assert looks_like_apply_button("Apply to this job") is True
    assert looks_like_apply_button("Continue to application") is True
    assert looks_like_apply_button("Submit Application") is True
    assert looks_like_apply_button("Autofill with Resume") is True
    assert looks_like_apply_button("Apply Manually") is True
    assert looks_like_apply_button("Use My Last Application") is True


def test_non_apply_button_is_not_matched():
    assert looks_like_apply_button("Careers Home") is False
    assert looks_like_apply_button("Sign In") is False

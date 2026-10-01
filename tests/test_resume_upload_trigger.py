from main import looks_like_resume_upload_trigger


def test_matches_resume_upload_labels():
    assert looks_like_resume_upload_trigger("Upload Resume")
    assert looks_like_resume_upload_trigger("Attach your CV")
    assert looks_like_resume_upload_trigger("Cover Letter")
    assert looks_like_resume_upload_trigger("Attachment")


def test_does_not_match_site_navigation_labels():
    # Regression test: a bare "cover" substring previously matched T-Mobile's
    # "Coverage" nav menu item, causing the script to navigate away from the
    # application page instead of finding a real resume-upload control.
    assert not looks_like_resume_upload_trigger("Coverage")
    assert not looks_like_resume_upload_trigger("Coverage map")
    assert not looks_like_resume_upload_trigger("Discover")
    assert not looks_like_resume_upload_trigger("")

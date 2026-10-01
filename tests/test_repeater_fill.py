from src.repeater_fill import find_empty_block_index, find_matching_block_index, normalize_value


def test_entry_match_pairs_by_index():
    """School/degree lists must align by row index."""
    entry = {
        "school": "UT Dallas",
        "degree": "B.S. in Computer Science",
    }
    schools = ["UT Dallas", "UT Dallas"]
    degrees = ["M.S. in Computer Science", "B.S. in Computer Science"]

    assert find_matching_block_index(entry, schools, degrees, "school", "degree") == 1

    wrong_degrees = ["M.S. in Computer Science", "M.S. in Computer Science"]
    assert find_matching_block_index(entry, schools, wrong_degrees, "school", "degree") is None


def test_matches_by_title_when_company_blank():
    # Regression test: resume-parser autofill sometimes leaves Company blank but
    # still captures a (possibly truncated) title. Must still identify the block
    # by title alone so we complete it instead of creating a duplicate.
    entry = {"company": "Fidelity Investments", "title": "Senior Manager Data Analytics & Strategy"}
    companies = ["Fidelity Investments", "", "First United Bank"]
    titles = ["Director Data Science and Analytics", "Senior Manager", "Business Analyst II"]

    assert find_matching_block_index(entry, companies, titles, "company", "title") == 1


def test_no_match_when_company_belongs_to_different_block():
    # A deleted block leaves no trace — must not fall back to clobbering an
    # unrelated block just because the counts happen to line up positionally.
    entry = {"company": "Fidelity Investments", "title": "Senior Manager Data Analytics & Strategy"}
    companies = ["Fidelity Investments", "First United Bank"]
    titles = ["Director Data Science and Analytics", "Business Analyst II"]

    assert find_matching_block_index(entry, companies, titles, "company", "title") is None


def test_find_empty_block_index():
    companies = ["Fidelity Investments", ""]
    titles = ["Director Data Science and Analytics", ""]
    assert find_empty_block_index(companies, titles) == 1
    assert find_empty_block_index(companies, titles, exclude=frozenset({1})) is None


def test_normalize_value_requires_exact_match_for_strict_keys():
    # Truncated autofill ("Director") must NOT be considered equal to the full
    # canonical title once normalized — only an exact match should skip refill.
    assert normalize_value("Director") != normalize_value("Director Data Science and Analytics")
    assert normalize_value("  Director Data Science and Analytics ") == normalize_value(
        "Director Data Science and Analytics"
    )

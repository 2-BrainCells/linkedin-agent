from agent.llm.parse_contact import _regex_pass


def test_extracts_plain_email():
    out = _regex_pass("Reach me at sam@example.com anytime.")
    assert out.emails == ["sam@example.com"]


def test_extracts_multiple_emails_dedup():
    text = "primary: a@b.com, work: c@d.co — old: a@b.com"
    out = _regex_pass(text)
    assert out.emails == ["a@b.com", "c@d.co"]


def test_extracts_phone():
    out = _regex_pass("Call +1 (415) 555-0123 between 9 and 5")
    assert "+1" in out.phone


def test_extracts_twitter_handle():
    out = _regex_pass("twitter.com/sam_dev")
    assert out.twitter == "sam_dev"


def test_extracts_website():
    out = _regex_pass("More at https://example.com/about")
    assert out.website.startswith("https://example.com")


def test_empty_input():
    out = _regex_pass("")
    assert out.emails == []
    assert out.phone == ""

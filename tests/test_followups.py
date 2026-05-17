from agent.mailer.sender import _normalize_subject_for_reply


def test_re_prefix_added_once():
    assert _normalize_subject_for_reply("Quick question for Sam") == "Re: Quick question for Sam"


def test_re_prefix_not_stacked():
    assert _normalize_subject_for_reply("Re: Quick question") == "Re: Quick question"


def test_multiple_re_prefixes_collapsed():
    assert _normalize_subject_for_reply("Re: Re: Re: Original") == "Re: Original"


def test_re_prefix_case_insensitive():
    assert _normalize_subject_for_reply("RE: hey") == "Re: hey"
    assert _normalize_subject_for_reply("re: hey") == "Re: hey"


def test_empty_subject():
    assert _normalize_subject_for_reply("") == "Re:"
    assert _normalize_subject_for_reply("   ") == "Re:"

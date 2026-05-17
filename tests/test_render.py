from agent.templating import render_string


def test_first_name_substitution():
    out = render_string("Hi {{ first_name }}!", first_name="Abhinav")
    assert out == "Hi Abhinav!"


def test_multiple_vars():
    src = "Hi {{ first_name }},\n\n{{ opener }}\n\nBest,\n{{ from_name }}\n"
    out = render_string(src, first_name="Sam", opener="Loved your post.", from_name="Abhi")
    assert "Hi Sam," in out
    assert "Loved your post." in out
    assert out.rstrip().endswith("Abhi")


def test_strict_undefined_raises():
    import pytest
    from jinja2 import UndefinedError
    with pytest.raises(UndefinedError):
        render_string("Hi {{ nope }}", first_name="x")

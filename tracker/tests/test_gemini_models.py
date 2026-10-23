import pytest

from collector import Article
from extractor import Extraction, GeminiExtractor, QuotaExhausted

ARTICLE = Article("https://example.com/a", "PR Newswire", "t", "", None)
OK = Extraction(is_funding_announcement=False, events=[])


class ApiError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} error")
        self.code = code


def make(behaviour: dict) -> tuple[GeminiExtractor, list]:
    """behaviour: model -> exception to raise, or None to succeed. Returns the extractor and a call log."""
    calls = []
    x = GeminiExtractor.__new__(GeminiExtractor)
    x._models = list(behaviour)

    def fake(model, article, text):
        calls.append(model)
        if behaviour[model] is not None:
            raise behaviour[model]
        return OK

    x._extract_with = fake
    return x, calls


def test_overloaded_model_falls_through_but_is_kept():
    x, calls = make({"3.8": ApiError(503), "3.6": None})
    assert x.extract(ARTICLE, "") is OK
    assert x.extract(ARTICLE, "") is OK
    # 3.8 is tried again for the next article: overload is temporary.
    assert calls == ["3.8", "3.6", "3.8", "3.6"]


def test_exhausted_model_is_dropped_for_the_run():
    x, calls = make({"3.8": QuotaExhausted("daily cap"), "3.6": None})
    x.extract(ARTICLE, "")
    x.extract(ARTICLE, "")
    assert calls == ["3.8", "3.6", "3.6"]


def test_all_models_exhausted_raises_quota_exhausted():
    x, _ = make({"3.8": QuotaExhausted("a"), "3.6": QuotaExhausted("b")})
    with pytest.raises(QuotaExhausted):
        x.extract(ARTICLE, "")


def test_all_models_overloaded_raises_the_api_error():
    x, _ = make({"3.8": ApiError(503), "3.6": ApiError(503)})
    with pytest.raises(ApiError):
        x.extract(ARTICLE, "")


def test_other_errors_are_not_retried_on_another_model():
    x, calls = make({"3.8": ValueError("bad json"), "3.6": None})
    with pytest.raises(ValueError):
        x.extract(ARTICLE, "")
    assert calls == ["3.8"]

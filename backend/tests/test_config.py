from __future__ import annotations

import pytest

from app.config import _flag


def test_flag_parsing(monkeypatch):
    """开关的常见写法都认；不设、留空用默认值；认不出的直接报错，不悄悄按反的跑。"""
    for raw, want in [("1", True), ("TRUE", True), ("Yes", True), (" on ", True), ("0", False), ("False", False), ("off", False), ("NO", False)]:
        monkeypatch.setenv("LINGDRAFT_TEST_FLAG", raw)
        assert _flag("LINGDRAFT_TEST_FLAG", not want) is want, raw
    for raw in ("", "  "):
        monkeypatch.setenv("LINGDRAFT_TEST_FLAG", raw)
        assert _flag("LINGDRAFT_TEST_FLAG", True) is True and _flag("LINGDRAFT_TEST_FLAG", False) is False
    monkeypatch.delenv("LINGDRAFT_TEST_FLAG")
    assert _flag("LINGDRAFT_TEST_FLAG", True) is True
    monkeypatch.setenv("LINGDRAFT_TEST_FLAG", "enable")
    with pytest.raises(ValueError, match="LINGDRAFT_TEST_FLAG"):
        _flag("LINGDRAFT_TEST_FLAG", False)

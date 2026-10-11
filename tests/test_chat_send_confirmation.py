"""DOM-fixture tests for independent chat send evidence; no browser or BOSS I/O."""
from __future__ import annotations

import asyncio

from boss_zhipin.domain import IdentityConfidence, JobIdentity
from boss_zhipin.website_oper import finding_jobs


def node(message_id: str, text: str, *, outgoing: bool = True, is_input: bool = False) -> dict:
    return {"id": message_id, "text": text, "outgoing": outgoing, "is_input": is_input}


def test_input_text_and_history_text_never_confirm_repeated_message():
    message = "您好，想进一步沟通。"
    before = [node("old-1", message)]
    # An input carries the same text and the old bubble remains in the page.
    after = [node("old-1", message), node("input", message, is_input=True)]
    result = finding_jobs.confirm_new_outgoing_message(before, after, message)
    assert result.confirmed is False


def test_parent_child_or_two_new_duplicate_nodes_are_ambiguous():
    message = "您好，想进一步沟通。"
    result = finding_jobs.confirm_new_outgoing_message([], [node("parent", message), node("child", message)], message)
    assert result.confirmed is False
    assert "多个" in result.error


def test_only_one_new_independent_outgoing_node_confirms_same_repeated_text():
    message = "您好，想进一步沟通。"
    result = finding_jobs.confirm_new_outgoing_message([node("old", message)], [node("old", message), node("new", message)], message)
    assert result.confirmed is True
    assert "new" in result.evidence


def test_send_adapter_requires_identity_and_does_not_touch_input_without_it(monkeypatch):
    called = {"select": 0}
    class Tab:
        async def select(self, *_args, **_kwargs):
            called["select"] += 1
    monkeypatch.setattr(finding_jobs, "_tab", Tab())
    result = asyncio.run(finding_jobs.send_chat_message("hello"))
    assert result.confirmed is False
    assert called["select"] == 0


def test_send_adapter_confirms_only_new_fixture_node(monkeypatch):
    identity = JobIdentity(key="boss-job:1", confidence=IdentityConfidence.STRONG, job_id="1", company="测试公司", title="测试岗位")
    snapshots = iter(([node("old", "重复")], [node("old", "重复"), node("new", "重复")]))
    async def messages():
        return next(snapshots)
    async def matches(_identity):
        return True, "fixture target"
    class Input:
        async def send_keys(self, _value):
            return None
    class Tab:
        async def select(self, *_args, **_kwargs):
            return Input()
    monkeypatch.setattr(finding_jobs, "_independent_outgoing_message_nodes", messages)
    monkeypatch.setattr(finding_jobs, "current_chat_matches", matches)
    monkeypatch.setattr(finding_jobs, "_tab", Tab())
    result = asyncio.run(finding_jobs.send_chat_message("重复", identity))
    assert result.confirmed is True

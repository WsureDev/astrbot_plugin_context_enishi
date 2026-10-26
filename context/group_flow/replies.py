from __future__ import annotations

import time
from collections import defaultdict, deque

from ...channels.native import field


def plain_text(value):
    return " ".join(str(value or "").split())


class SelfReplies:
    """Only suppress recently emitted LLM text from the bot's platform account."""

    def __init__(self):
        self.messages = defaultdict(lambda: deque(maxlen=128))

    def remember(self, scope, text):
        text = plain_text(text)
        if text:
            self.messages[scope].append((time.monotonic() + 180, text))

    def response(self, event, response):
        if field(response, "role") != "assistant":
            return
        self.remember(event.unified_msg_origin, field(response, "completion_text"))
        chain = field(field(response, "result_chain"), "chain", [])
        for part in chain or []:
            self.remember(event.unified_msg_origin, field(part, "text"))

    def matches(self, scope, text):
        messages = self.messages.get(scope)
        if not messages:
            return False
        now = time.monotonic()
        while messages and messages[0][0] < now:
            messages.popleft()
        return any(known == plain_text(text) for _, known in messages)

"""Character budgets for saved history; current request and system text stay exact."""

from __future__ import annotations

import logging

from ...channels.native import field
from .markers import deduplicate_markers
from .structure import content_size, delta_table, render_delta, rounds

logger = logging.getLogger(__name__)
GROUP_BUDGET_COVERAGE = "older rows omitted by history character budget; original channel IDs in retained rows are unchanged"


class HistoryWindow:
    def __init__(self, settings, runner, text_factory):
        self.settings, self.text_factory = settings, text_factory
        self.event = field(field(runner.run_context, "context"), "event")
        self.current = next(
            (
                message
                for message in reversed(runner.run_context.messages)
                if field(message, "role") == "user"
            ),
            None,
        )
        self.delta = field(getattr(runner.req, "_enishi_flow_delta", None), "content")
        self.context = runner.run_context

    def apply(self, messages):
        options = self.settings.history_options()
        if not options["enabled"] or self.current is None:
            return False
        boundary = next(
            (
                index
                for index, message in enumerate(messages)
                if message is self.current
            ),
            None,
        )
        if boundary is None:
            return False
        if (
            boundary > 0
            and self.delta
            and field(messages[boundary - 1], "content") == self.delta
        ):
            boundary -= 1
        duplicates = deduplicate_markers(messages[:boundary], self.text_factory)
        systems = [
            message
            for message in messages[:boundary]
            if field(message, "role") == "system"
        ]
        history = [
            message
            for message in messages[:boundary]
            if field(message, "role") != "system"
        ]
        active = messages[boundary:]
        before = sum(content_size(message) for message in history)
        group_removed = self._trim_groups(history, active, options["max_group_chars"])
        units = rounds(history, self.event)
        total = sum(content_size(message) for message in history)
        removed_rounds = 0
        if total > options["max_chars"]:
            while units and total > options["target_chars"]:
                next_total = total - sum(content_size(message) for message in units[0])
                if (
                    total <= options["max_chars"]
                    and next_total < options["target_chars"]
                ):
                    break
                total = next_total
                units.pop(0)
                removed_rounds += 1
        retained = [message for unit in units for message in unit]
        kept = {id(message) for message in retained}
        messages[:] = [
            message
            for message in messages[:boundary]
            if field(message, "role") == "system" or id(message) in kept
        ] + active
        stats = {
            "history_chars_before": before,
            "history_chars_after": sum(content_size(message) for message in retained),
            "removed_rounds": removed_rounds,
            "removed_group_rows": group_removed,
            "removed_duplicate_markers": duplicates,
            "protected_chars": sum(
                content_size(message) for message in [*systems, *active]
            ),
        }
        object.__setattr__(self.context, "_enishi_history_budget_stats", stats)
        changed = bool(duplicates or before != stats["history_chars_after"])
        if changed:
            logger.info(
                "Enishi history budget %s: %s", self.event.unified_msg_origin, stats
            )
        return changed

    def _trim_groups(self, history, active, budget):
        if budget == 0:
            return 0
        used = sum(
            len(field(message, "content"))
            for message in active
            if delta_table(message, self.event) is not None
        )
        removed = 0
        retained = []
        for message in reversed(history):
            parsed = delta_table(message, self.event)
            if parsed is None:
                retained.append(message)
                continue
            metadata, fields, rows = parsed
            remaining = max(0, budget - used)
            original_count = len(rows)
            content = field(message, "content")
            if rows and len(content) > remaining:
                row_chars = sum(len(row) + 1 for row in rows)
                skipped = metadata.get("skipped_count", 0)
                drop = 0
                while drop < original_count:
                    row_chars -= len(rows[drop]) + 1
                    drop += 1
                    metadata["skipped_count"] = skipped + drop
                    metadata["coverage"] = GROUP_BUDGET_COVERAGE
                    if len(render_delta(metadata, fields, [])) + row_chars <= remaining:
                        break
                rows = rows[drop:]
                content = render_delta(metadata, fields, rows)
            removed += original_count - len(rows)
            if not rows:
                if original_count == 0 and len(content) <= remaining:
                    retained.append(message)
                    used += len(content)
                continue
            if isinstance(message, dict):
                message["content"] = content
            else:
                message.content = content
            retained.append(message)
            used += len(content)
        history[:] = reversed(retained)
        return removed

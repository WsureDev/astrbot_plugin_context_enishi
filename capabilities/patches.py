from __future__ import annotations

import functools
import inspect


def contains(current, expected):
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (classmethod, staticmethod)) or inspect.ismethod(
            current
        ):
            current = current.__func__
            continue
        if current is expected:
            return True
        current = getattr(current, "__wrapped__", None)
    return False


class Patches:
    """Cooperative wrappers become inert when buried under another plugin's wrapper."""

    def __init__(self):
        self.active = True
        self.rows = []

    def add(self, target, name, factory):
        descriptor = inspect.getattr_static(target, name, None)
        original = getattr(target, name, None)
        if original is None:
            return False
        local = name in vars(target)
        if isinstance(descriptor, classmethod):
            original = descriptor.__func__
        replacement = factory(original)
        patch = self
        if inspect.isasyncgenfunction(original):

            @functools.wraps(original)
            async def wrapper(*args, **kwargs):
                method = replacement if patch.active else original
                iterator = method(*args, **kwargs)
                try:
                    async for item in iterator:
                        yield item
                finally:
                    await iterator.aclose()
        elif inspect.iscoroutinefunction(original):

            @functools.wraps(original)
            async def wrapper(*args, **kwargs):
                method = replacement if patch.active else original
                return await method(*args, **kwargs)
        else:

            @functools.wraps(original)
            def wrapper(*args, **kwargs):
                method = replacement if patch.active else original
                return method(*args, **kwargs)

        installed = wrapper
        saved = original
        if isinstance(descriptor, classmethod):
            installed = classmethod(wrapper)
            saved = descriptor
        setattr(target, name, installed)
        self.rows.append((target, name, saved, installed, local))
        return True

    def intact(self):
        return all(
            contains(getattr(target, name, None), getattr(wrapper, "__func__", wrapper))
            for target, name, _, wrapper, _ in self.rows
        )

    def close(self):
        self.active = False
        for target, name, original, wrapper, local in reversed(self.rows):
            if inspect.getattr_static(target, name, None) is wrapper:
                if local:
                    setattr(target, name, original)
                else:
                    delattr(target, name)
        self.rows.clear()

from __future__ import annotations

"""Smoke-test AstrBot import modes without requiring a running AstrBot instance.

AstrBot loads native plugins as packages under data/plugins. A top-level import
of main.py is also useful for local developer checks. This script verifies both
import modes with minimal AstrBot API stubs.
"""

import asyncio
import importlib
import sys
import tempfile
import types
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PARENT = REPO.parent


def install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    comps = types.ModuleType("astrbot.api.message_components")

    class Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    api.logger = Logger()

    class Filter:
        def command(self, *args, **kwargs):
            return lambda func: func

        def llm_tool(self, *args, **kwargs):
            return lambda func: func

    event.filter = Filter()

    class AstrMessageEvent:
        pass

    event.AstrMessageEvent = AstrMessageEvent

    class MessageChain(list):
        pass

    event.MessageChain = MessageChain

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*args, **kwargs):
        return lambda cls: cls

    star.Context = Context
    star.Star = Star
    star.register = register

    class Plain:
        def __init__(self, text: str = ""):
            self.text = text

    class Image:
        @staticmethod
        def fromFileSystem(path: str):
            return ("file", path)

        @staticmethod
        def fromURL(url: str):
            return ("url", url)

    comps.Plain = Plain
    comps.Image = Image

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.message_components": comps,
        }
    )


def clear_plugin_modules() -> None:
    for name in list(sys.modules):
        if name == "main" or name == "gpt_image2" or name.startswith("gpt_image2."):
            sys.modules.pop(name, None)
        if name == "astrbot_plugin_gpt_image2" or name.startswith("astrbot_plugin_gpt_image2."):
            sys.modules.pop(name, None)


def import_top_level_main() -> None:
    clear_plugin_modules()
    sys.path.insert(0, str(REPO))
    try:
        mod = importlib.import_module("main")
        plugin = mod.GPTImage2Plugin(sys.modules["astrbot.api.star"].Context(), {"api": {"api_key": "sk-testsk-testsk-test"}})
        opts = plugin._parse_prompt_and_options("--size 1024x1024 a mid-journey cat --quality high")
        assert opts["prompt"] == "a mid-journey cat"
        assert opts["size"] == "1024x1024"
        assert opts["quality"] == "high"
        assert plugin._submit_message("job", "generation", 1)

        class StopEvent:
            def __init__(self):
                self.stopped = False

            def stop_event(self):
                self.stopped = True

        stop_event = StopEvent()
        mod._stop_event_silently(stop_event)
        assert stop_event.stopped

        quiet_plugin = mod.GPTImage2Plugin(
            sys.modules["astrbot.api.star"].Context(),
            {"api": {"api_key": "sk-tes...test"}, "runtime": {"quiet_mode": True}},
        )
        assert quiet_plugin._submit_message("job", "generation", 1) == ""
        assert "queued:job" in quiet_plugin._tool_submit_message("job", "generation", 1)

        class MutableConfig(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.saved = False

            def save_config(self):
                self.saved = True

        legacy_config = MutableConfig(
            {"api": {"api_key": "sk-tes...test", "fallback_endpoints": "[]"}}
        )
        mod.GPTImage2Plugin(sys.modules["astrbot.api.star"].Context(), legacy_config)
        assert legacy_config["api"]["fallback_endpoints"] == []
        assert legacy_config.saved
    finally:
        try:
            sys.path.remove(str(REPO))
        except ValueError:
            pass


def test_silent_group_deny_stops_command_event() -> None:
    clear_plugin_modules()
    sys.path.insert(0, str(REPO))
    try:
        mod = importlib.import_module("main")
        plugin = mod.GPTImage2Plugin(
            sys.modules["astrbot.api.star"].Context(),
            {
                "api": {"api_key": "sk-tes...test"},
                "access": {"enabled": True, "group_whitelist": "allowed-group"},
            },
        )
        plugin.manager = object()
        tmpdir = tempfile.TemporaryDirectory()
        plugin.access = mod.AccessController(config=plugin.config.access, state_path=Path(tmpdir.name) / "access_state.json")

        class FakeEvent:
            def __init__(self):
                self.stopped = False
                self.unified_msg_origin = "stub:GroupMessage:denied-group"

            def get_message_str(self):
                return "/gptimg a cat"

            def get_platform_name(self):
                return "stub"

            def get_sender_id(self):
                return "user1"

            def get_sender_name(self):
                return "User"

            def get_group_id(self):
                return "denied-group"

            def stop_event(self):
                self.stopped = True

            def plain_result(self, text):  # pragma: no cover - must not be called.
                raise AssertionError(f"silent deny should not yield text: {text}")

        event = FakeEvent()
        outputs = []

        async def run_handler():
            async for item in plugin.gptimg(event):
                outputs.append(item)

        asyncio.run(run_handler())
        assert outputs == []
        assert event.stopped
    finally:
        try:
            sys.path.remove(str(REPO))
        except ValueError:
            pass


def test_llm_tool_uses_usage_limits() -> None:
    clear_plugin_modules()
    sys.path.insert(0, str(REPO))
    try:
        mod = importlib.import_module("main")
        plugin = mod.GPTImage2Plugin(
            sys.modules["astrbot.api.star"].Context(),
            {
                "api": {"api_key": "sk-tes...test"},
                "access": {
                    "enabled": True,
                    "user_blacklist": "blocked-user",
                    "user_whitelist": "blocked-user,allowed-user",
                    "non_whitelist_daily_limit": 0,
                },
            },
        )

        class NeverEnqueueManager:
            async def enqueue(self, *args, **kwargs):  # pragma: no cover - must not be called.
                raise AssertionError("LLM Tool bypassed usage limits and reached enqueue")

        class FakeEvent:
            unified_msg_origin = "stub:FriendMessage:blocked-user"

            def get_platform_name(self):
                return "stub"

            def get_sender_id(self):
                return "blocked-user"

            def get_sender_name(self):
                return "Blocked"

            def get_group_id(self):
                return ""

        plugin.manager = NeverEnqueueManager()
        with tempfile.TemporaryDirectory() as td:
            plugin.access = mod.AccessController(config=plugin.config.access, state_path=Path(td) / "access_state.json")
            result = asyncio.run(plugin.gpt_image2_generate(FakeEvent(), "a cat"))
        assert "用户黑名单" in result
    finally:
        try:
            sys.path.remove(str(REPO))
        except ValueError:
            pass


def import_package_main() -> None:
    clear_plugin_modules()
    sys.path.insert(0, str(PARENT))
    try:
        mod = importlib.import_module("astrbot_plugin_gpt_image2.main")
        plugin = mod.GPTImage2Plugin(sys.modules["astrbot.api.star"].Context(), {"api": {"api_key": "sk-testsk-testsk-test"}})
        opts = plugin._parse_prompt_and_options("a mid-journey cat --quality high --size 1024x1024")
        assert opts["prompt"] == "a mid-journey cat"
        assert opts["size"] == "1024x1024"
        assert opts["quality"] == "high"
    finally:
        try:
            sys.path.remove(str(PARENT))
        except ValueError:
            pass


def main() -> None:
    install_astrbot_stubs()
    import_top_level_main()
    test_silent_group_deny_stops_command_event()
    test_llm_tool_uses_usage_limits()
    import_package_main()
    print("import mode smoke passed")


if __name__ == "__main__":
    main()

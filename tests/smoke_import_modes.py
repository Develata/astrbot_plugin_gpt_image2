from __future__ import annotations

"""Smoke-test AstrBot import modes without requiring a running AstrBot instance.

AstrBot loads native plugins as packages under data/plugins. A top-level import
of main.py is also useful for local developer checks. This script verifies both
import modes with minimal AstrBot API stubs.
"""

import importlib
import sys
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
    import_package_main()
    print("import mode smoke passed")


if __name__ == "__main__":
    main()

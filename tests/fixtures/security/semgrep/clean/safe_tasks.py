"""Фикстура SAST (P6-04): код без находок правил платформы."""

import json
import subprocess


def run(args: list[str]) -> int:
    return subprocess.run(args, check=False).returncode  # noqa: S603


def parse(text: str) -> object:
    return json.loads(text)

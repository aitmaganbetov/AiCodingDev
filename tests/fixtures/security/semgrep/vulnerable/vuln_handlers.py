"""Фикстура SAST (P6-04): заведомо уязвимый код для правил платформы. Не импортируется."""

import os
import pickle
import subprocess

import yaml


def run(cmd: str) -> None:
    subprocess.run(cmd, shell=True, check=False)  # noqa: S602


def legacy(cmd: str) -> None:
    os.system(cmd)  # noqa: S605  # nosemgrep — подавление отключено платформой (--disable-nosem)


def load_config(text: str) -> object:
    return yaml.load(text, Loader=yaml.Loader)  # noqa: S506


def restore(blob: bytes) -> object:
    return pickle.loads(blob)  # noqa: S301


def calc(expr: str) -> object:
    return eval(expr)  # noqa: S307

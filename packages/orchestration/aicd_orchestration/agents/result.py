"""Контракт файла результата `.aicd/result.json` и его разбор (P4-03; AGENT_ROLES §3.4, ACP §1, §5, §8).

Файл пишет агент — это **недоверенные данные**. Разбор:

1. файл — обычный (не symlink), размер ≤ `MAX_RESULT_BYTES`, UTF-8, строгий JSON (без дублей ключей,
   NaN/Infinity, глубина ≤ `MAX_DEPTH`) → иначе `ResultParseError` (`OUTPUT_PARSE_ERROR`);
2. валидация по нормативной схеме `agent/1.0/agent-result.schema.json` (поля `from`/отправителя нет —
   попытка подмены отправителя отклоняет весь итог);
3. `declared_outputs`: путь только внутри `output/` без `..`/скрытых сегментов/symlink-выхода → иначе
   ошибка разбора; при заданном корне файл обязан существовать;
4. `claims` возвращаются с `verified=False` — подтверждают их только гейты (SC-INJ-04);
5. `acp_messages` — кандидаты: предварительная проверка ACL отправителя (ACP §4.2), payload по схеме
   типа (ссылки `output/<путь>` заменяются детерминированными `artifact:<uuid>`-заглушками только для
   проверки и обязаны указывать на `declared_outputs`), лимит канонического размера 32 KiB.
   Отклонённый кандидат не роняет итог (ACP §5), он попадает в `rejected_messages` с кодом.
   Окончательную проверку (конверт, ACL получателя, correlation, лимиты циклов) выполняет брокер.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final

from aicd_orchestration.agents.contracts import AGENT_RESULT_SCHEMA, acp_payload_validator, validator
from aicd_orchestration.agents.profiles import can_send
from aicd_orchestration.agents.prompt import OUTPUT_DIR, RESULT_PATH, scan_injection

MAX_RESULT_BYTES: Final = 256 * 1024
MAX_MESSAGE_RAW_BYTES: Final = 64 * 1024
MAX_MESSAGE_CANONICAL_BYTES: Final = 32 * 1024
MAX_DEPTH: Final = 32
_PLACEHOLDER_NS: Final = uuid.UUID("6f2b4c1e-0a7d-4c55-9e0e-2f3a6c1b8d11")


class ResultParseError(ValueError):
    code = "OUTPUT_PARSE_ERROR"

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{self.code}: {reason}")
        self.reason = reason
        self.details = details or {}


@dataclass(frozen=True)
class Claim:
    type: str
    summary: str | None
    files: tuple[str, ...]
    verified: bool = False  # всегда False: claims ≠ evidence


@dataclass(frozen=True)
class DeclaredOutput:
    kind: str
    path: str  # относительный путь `output/...`


@dataclass(frozen=True)
class AcpCandidate:
    to_role: str
    type: str
    payload: dict[str, Any]
    reply_to_id: str | None
    output_refs: tuple[str, ...]  # пути `output/...`, которые gateway заменит на `artifact:<uuid>`
    suspected_injection: tuple[str, ...] = ()


@dataclass(frozen=True)
class RejectedMessage:
    index: int
    type: str | None
    code: str  # acp_rejection_code: SCHEMA_INVALID | ACL_DENIED | PAYLOAD_TOO_LARGE
    reason: str


@dataclass(frozen=True)
class DirectiveAck:
    directive_id: str
    status: str
    note: str | None


@dataclass(frozen=True)
class ParsedAgentResult:
    sha256: str
    summary: str
    claims: tuple[Claim, ...]
    declared_outputs: tuple[DeclaredOutput, ...]
    acp_messages: tuple[AcpCandidate, ...]
    rejected_messages: tuple[RejectedMessage, ...]
    directive_acks: tuple[DirectiveAck, ...]
    warnings: tuple[str, ...] = field(default=())


# --- Чтение и строгий JSON --------------------------------------------------------------------------


def read_result_file(workdir: Path, result_path: str = RESULT_PATH) -> bytes:
    """Чтение файла результата без следования symlink и выхода за пределы `workdir`."""
    rel = _safe_relative(result_path, allow_prefix=None)
    if rel is None:
        raise ResultParseError("недопустимый путь файла результата")
    root = workdir.resolve()
    target = root.joinpath(*PurePosixPath(rel).parts)
    _assert_no_symlink_components(root, target)
    try:
        st = target.lstat()
    except FileNotFoundError:
        raise ResultParseError("файл результата отсутствует", {"path": rel}) from None
    if not stat.S_ISREG(st.st_mode):
        raise ResultParseError("файл результата не является обычным файлом", {"path": rel})
    if st.st_size > MAX_RESULT_BYTES:
        raise ResultParseError("файл результата превышает лимит размера", {"size": st.st_size})
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, flags)
    try:
        data = os.read(fd, MAX_RESULT_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_RESULT_BYTES:
        raise ResultParseError("файл результата превышает лимит размера")
    return data


def _assert_no_symlink_components(root: Path, target: Path) -> None:
    cur = root
    for part in target.relative_to(root).parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            raise ResultParseError("путь проходит через symlink", {"path": str(cur.relative_to(root))})


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ResultParseError("повторяющийся ключ JSON", {"key": k[:100]})
        out[k] = v
    return out


def _reject_constant(name: str) -> Any:
    raise ResultParseError("недопустимая константа JSON", {"value": name})


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_DEPTH:
        return level
    if isinstance(value, dict):
        return max((_depth(v, level + 1) for v in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level + 1)
    return level


def loads_strict(data: bytes) -> Any:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ResultParseError("файл результата не UTF-8") from None
    if decoded.startswith("﻿"):
        decoded = decoded[1:]
    try:
        value = json.loads(decoded, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant)
    except RecursionError:
        raise ResultParseError("превышена глубина вложенности JSON") from None
    except json.JSONDecodeError as exc:
        raise ResultParseError("файл результата не является JSON", {"position": exc.pos}) from None
    if _depth(value) > MAX_DEPTH:
        raise ResultParseError("превышена глубина вложенности JSON")
    return value


# --- Пути ---------------------------------------------------------------------------------------------


def _safe_relative(path: str, *, allow_prefix: str | None) -> str | None:
    """Нормализованный относительный POSIX-путь или None (абсолютный, `..`, пустые/скрытые сегменты)."""
    if not path or "\\" in path or "\x00" in path or path.startswith("/"):
        return None
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if allow_prefix is not None and (parts[0] != allow_prefix or len(parts) < 2):
        return None
    return "/".join(parts)


def _check_output_file(root: Path, rel: str) -> None:
    base = root.resolve()
    target = base.joinpath(*PurePosixPath(rel).parts)
    _assert_no_symlink_components(base, target)
    try:
        st = target.lstat()
    except FileNotFoundError:
        raise ResultParseError("объявленный вывод отсутствует", {"path": rel}) from None
    if not stat.S_ISREG(st.st_mode):
        raise ResultParseError("объявленный вывод не является обычным файлом", {"path": rel})


# --- ACP-кандидаты --------------------------------------------------------------------------------------


def placeholder_ref(output_path: str) -> str:
    return f"artifact:{uuid.uuid5(_PLACEHOLDER_NS, output_path)}"


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def _substitute(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, dict):
        return {k: _substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, mapping) for v in value]
    return value


def canonical_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _check_message(
    index: int, msg: dict[str, Any], sender_role: str, declared: set[str]
) -> AcpCandidate | RejectedMessage:
    mtype = str(msg["type"])
    payload: dict[str, Any] = msg["payload"]
    if canonical_size(msg) > MAX_MESSAGE_RAW_BYTES:
        return RejectedMessage(index, mtype, "PAYLOAD_TOO_LARGE", "сообщение больше транспортного лимита")
    handoff_kind = payload.get("handoff_kind") if mtype == "HANDOFF" else None
    if not can_send(
        sender_role, mtype, str(msg["to_role"]), handoff_kind=handoff_kind if isinstance(handoff_kind, str) else None
    ):
        return RejectedMessage(
            index, mtype, "ACL_DENIED", f"{sender_role} не может отправить {mtype} → {msg['to_role']}"
        )
    output_refs = sorted({s for s in _walk_strings(payload) if s.startswith(f"{OUTPUT_DIR}/")})
    bad = [r for r in output_refs if _safe_relative(r, allow_prefix=OUTPUT_DIR) is None or r not in declared]
    if bad:
        return RejectedMessage(index, mtype, "SCHEMA_INVALID", "ссылка на необъявленный или недопустимый вывод")
    resolved = _substitute(payload, {r: placeholder_ref(r) for r in output_refs})
    errors = sorted(acp_payload_validator(mtype).iter_errors(resolved), key=lambda e: list(e.path))
    if errors:
        where = "/".join(str(p) for p in errors[0].absolute_path)
        return RejectedMessage(index, mtype, "SCHEMA_INVALID", f"payload {mtype}: {errors[0].validator} в /{where}")
    if canonical_size(resolved) > MAX_MESSAGE_CANONICAL_BYTES:
        return RejectedMessage(index, mtype, "PAYLOAD_TOO_LARGE", "канонический payload больше 32 KiB")
    flags = sorted({f for s in _walk_strings(payload) for f in scan_injection(s)})
    return AcpCandidate(
        to_role=str(msg["to_role"]),
        type=mtype,
        payload=payload,
        reply_to_id=msg.get("reply_to_id"),
        output_refs=tuple(output_refs),
        suspected_injection=tuple(flags),
    )


# --- Разбор -------------------------------------------------------------------------------------------


def parse_agent_result(data: bytes, *, sender_role: str, output_root: Path | None = None) -> ParsedAgentResult:
    """Разбор содержимого файла результата. `output_root` — каталог, содержащий `output/`."""
    if len(data) > MAX_RESULT_BYTES:
        raise ResultParseError("файл результата превышает лимит размера")
    doc = loads_strict(data)
    errors = sorted(validator(AGENT_RESULT_SCHEMA).iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path)
        raise ResultParseError("AgentResult не соответствует схеме", {"pointer": f"/{where}", "rule": first.validator})
    outputs: list[DeclaredOutput] = []
    for item in doc.get("declared_outputs", []):
        rel = _safe_relative(item["path"], allow_prefix=OUTPUT_DIR)
        if rel is None:
            raise ResultParseError("недопустимый путь объявленного вывода", {"path": item["path"][:200]})
        if output_root is not None:
            _check_output_file(output_root, rel)
        outputs.append(DeclaredOutput(kind=item["kind"], path=rel))
    if len({o.path for o in outputs}) != len(outputs):
        raise ResultParseError("объявленные выводы повторяются")
    warnings: list[str] = []
    claims: list[Claim] = []
    for c in doc["claims"]:
        files = []
        for f in c.get("files", []):
            if _safe_relative(f, allow_prefix=None) is None:
                warnings.append("claim_file_path_rejected")
                continue
            files.append(f)
        claims.append(Claim(type=c["type"], summary=c.get("summary"), files=tuple(files)))
    declared = {o.path for o in outputs}
    accepted: list[AcpCandidate] = []
    rejected: list[RejectedMessage] = []
    for i, msg in enumerate(doc["acp_messages"]):
        checked = _check_message(i, msg, sender_role, declared)
        if isinstance(checked, AcpCandidate):
            accepted.append(checked)
        else:
            rejected.append(checked)
    acks = tuple(DirectiveAck(a["directive_id"], a["status"], a.get("note")) for a in doc.get("directive_acks", []))
    if scan_injection(doc["summary"]):
        warnings.append("summary_suspected_injection")
    return ParsedAgentResult(
        sha256=hashlib.sha256(data).hexdigest(),
        summary=doc["summary"],
        claims=tuple(claims),
        declared_outputs=tuple(outputs),
        acp_messages=tuple(accepted),
        rejected_messages=tuple(rejected),
        directive_acks=acks,
        warnings=tuple(sorted(set(warnings))),
    )


def load_agent_result(workdir: Path, *, sender_role: str, result_path: str = RESULT_PATH) -> ParsedAgentResult:
    """Чтение и разбор `<workdir>/.aicd/result.json`; выводы ищутся в `<workdir>/output/`."""
    return parse_agent_result(read_result_file(workdir, result_path), sender_role=sender_role, output_root=workdir)

"""Эмулятор CLI-агента для тестов. НЕ является реализацией AI: только сценарии поведения процесса.

Протокол вывода (JSONL в stdout), понятный FakeOutputParser:
  {"event": "message", "text": "..."}
  {"event": "tool", "tool_kind": "file_write", "phase": "completed", "target": "path"}
  {"event": "result", "claims": [{"type": "...", "summary": "..."}], "usage": {...} | null}
Ошибки — сигнатуры в stderr ("FAKE-AUTH: login required", "FAKE-LIMIT: usage limit, retry after 120s").

Сценарии `agent_*` (Phase 4, ACP §9.2) дополнительно пишут итог AgentResult 1.0 в `.aicd/result.json` и отчёты
в `output/` (относительно cwd; платформа направляет оба каталога в отдельный каталог вывода исполнения).
Роль берётся из `instruction_template_version` (task.json), входящие сообщения — из раздела inbox промпта
(`--prompt-text-file`): строки-JSON с ключом `message_id` внутри ограждений UNTRUSTED; поручения раздела
directives (строки-JSON с `directive_id`) подтверждаются в `directive_acks` итога (`accepted`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


def emit(obj: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# --- Сценарии агентов (AgentResult 1.0) -------------------------------------------------------------

REVIEW_ROLES = ("reviewer", "security")
_BASE_COMMIT = re.compile(r"^base_commit: ([0-9a-f]{40}(?:[0-9a-f]{24})?)$", re.MULTILINE)


def role_of(task: dict[str, object]) -> str:
    version = task.get("instruction_template_version")
    return str(version).split("@", 1)[0] if isinstance(version, str) and "@" in version else "developer"


def inbox_of(prompt: str) -> list[dict[str, object]]:
    """Сообщения inbox из промпта: каждая строка-JSON с `message_id` (порядок — как в промпте)."""
    found: list[dict[str, object]] = []
    for line in prompt.splitlines():
        if not line.startswith('{"message_id"'):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def directive_acks(prompt: str) -> dict[str, object]:
    """`directive_acks` итога: подтверждение поручений раздела directives промпта (строки-JSON с `directive_id`)."""
    acks: list[dict[str, object]] = []
    for line in prompt.splitlines():
        if not line.startswith('{"directive_id"'):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("directive_id"), str):
            acks.append({"directive_id": obj["directive_id"], "status": "accepted"})
    return {"directive_acks": acks[:20]} if acks else {}


def write_output(name: str, data: str) -> tuple[str, str]:
    out = Path("output")
    out.mkdir(exist_ok=True)
    raw = data.encode("utf-8")
    (out / name).write_bytes(raw)
    return f"output/{name}", hashlib.sha256(raw).hexdigest()


def write_result(result: object, *, raw: str | None = None) -> None:
    Path(".aicd").mkdir(exist_ok=True)
    Path(".aicd/result.json").write_text(
        raw if raw is not None else json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )


def finish(summary: str, claim: str) -> int:
    emit({"event": "result", "claims": [{"type": claim, "summary": summary}], "usage": None})
    return 0


def agent_develop(task: dict[str, object], prompt: str, base: str) -> int:
    inbox = inbox_of(prompt)
    fixes = [m for m in inbox if m.get("type") == "FIX_REQUEST"]
    loop = min(10, len(fixes) + 1)
    body = f"# Изменение\n\n{task['objective']}\n" + (f"\nИсправлено замечаний: {len(fixes)}\n" if fixes else "")
    Path("FAKE_CHANGE.md").write_text(body, encoding="utf-8")
    emit({"event": "tool", "tool_kind": "file_write", "phase": "completed", "target": "FAKE_CHANGE.md"})
    added = "".join(f"+{line}\n" for line in body.splitlines())
    diff = (
        "diff --git a/FAKE_CHANGE.md b/FAKE_CHANGE.md\nnew file mode 100644\n--- /dev/null\n+++ b/FAKE_CHANGE.md\n"
        f"@@ -0,0 +1,{len(body.splitlines())} @@\n{added}"
    )
    diff_ref, diff_sha = write_output("changes.diff", diff)
    criteria = [str(c)[:2000] for c in task.get("acceptance_criteria") or []][:100]  # type: ignore[attr-defined]
    result = {
        "agent_result_version": "1.0",
        "summary": "Создан FAKE_CHANGE.md",
        "claims": [
            {"type": "implementation_completed", "summary": "создан FAKE_CHANGE.md", "files": ["FAKE_CHANGE.md"]}
        ],
        "declared_outputs": [{"kind": "other", "path": diff_ref}],
        "acp_messages": [
            {
                "to_role": "orchestrator",
                "type": "EVIDENCE_SUBMITTED",
                "payload": {"items": [{"kind": "git_diff", "artifact_ref": diff_ref, "sha256": diff_sha}]},
            },
            {
                "to_role": "reviewer",
                "type": "REVIEW_REQUEST",
                "payload": {
                    "review_kind": "code",
                    "diff_ref": diff_ref,
                    "commit_sha": base,
                    "scope_summary": "Новый файл FAKE_CHANGE.md",
                    "changed_files": ["FAKE_CHANGE.md"],
                    "acceptance_criteria": criteria,
                    "loop_iteration": loop,
                },
            },
            {
                "to_role": "orchestrator",
                "type": "TASK_COMPLETED",
                "payload": {
                    "summary": "Изменение готово к ревью",
                    "claims": [{"type": "implementation_completed", "evidence_refs": [diff_ref]}],
                    "artifact_refs": [diff_ref],
                    "commit_sha": base,
                },
            },
        ],
        **directive_acks(prompt),
    }
    write_result(result)
    return finish("создан FAKE_CHANGE.md", "implementation_completed")


def agent_review(task: dict[str, object], prompt: str, *, approve: bool) -> int:
    del task
    requests = [m for m in inbox_of(prompt) if m.get("type") == "REVIEW_REQUEST"]
    request = requests[-1] if requests else None
    verdict = "approved" if approve else "changes_requested"
    report_ref, _ = write_output("review.md", f"# Ревью\n\nВердикт: {verdict}\n")
    messages: list[dict[str, object]] = []
    if not approve:
        reply_to = str(request["message_id"]) if request else None
        payload = request.get("payload") if request else None
        loop = payload.get("loop_iteration", 1) if isinstance(payload, dict) else 1
        messages += [
            {
                "to_role": "developer",
                "type": "FINDING",
                "reply_to_id": reply_to,
                "payload": {
                    "title": "Нет проверки входных данных",
                    "severity": "high",
                    "category": "correctness",
                    "description": "Файл не описывает граничные случаи",
                    "file": "FAKE_CHANGE.md",
                    "blocking": True,
                    "evidence_refs": [report_ref],
                },
            },
            {
                "to_role": "developer",
                "type": "FIX_REQUEST",
                "reply_to_id": reply_to,
                "payload": {
                    "finding": "Нет проверки входных данных",
                    "severity": "high",
                    "file": "FAKE_CHANGE.md",
                    "evidence_refs": [report_ref],
                    "requested_action": "Описать граничные случаи и добавить проверку",
                    "loop_iteration": loop,
                },
            },
        ]
    messages.append(
        {
            "to_role": "orchestrator",
            "type": "TASK_COMPLETED",
            "payload": {
                "summary": f"Ревью завершено: {verdict}",
                "claims": [{"type": "review_completed", "evidence_refs": [report_ref]}],
                "artifact_refs": [report_ref],
                "review_verdict": verdict,
            },
        }
    )
    write_result(
        {
            "agent_result_version": "1.0",
            "summary": f"Ревью: {verdict}",
            "claims": [{"type": "review_completed", "summary": verdict}],
            "declared_outputs": [{"kind": "review_report", "path": report_ref}],
            "acp_messages": messages,
            **directive_acks(prompt),
        }
    )
    return finish(f"ревью: {verdict}", "review_completed")


def agent_complete(task: dict[str, object], prompt: str, role: str) -> int:
    del task
    payload: dict[str, object] = {
        "summary": "Задача выполнена",
        "claims": [{"type": "analysis_completed", "evidence_refs": []}],
        "artifact_refs": [],
    }
    if role in REVIEW_ROLES:
        payload["review_verdict"] = "approved"
    write_result(
        {
            "agent_result_version": "1.0",
            "summary": "Задача выполнена",
            "claims": [{"type": "analysis_completed"}],
            "acp_messages": [{"to_role": "orchestrator", "type": "TASK_COMPLETED", "payload": payload}],
            **directive_acks(prompt),
        }
    )
    return finish("задача выполнена", "analysis_completed")


def agent_bad(scenario: str) -> int:
    """Негативные итоги: невалидный JSON, подмена отправителя, нарушения ACL (одно — до брокера, одно — брокером)."""
    if scenario in ("agent_bad_result", "agent_bad_json"):
        write_result(None, raw='{"agent_result_version": "1.0", "summary": ')
    elif scenario == "agent_spoofed_from":
        write_result(
            {
                "agent_result_version": "1.0",
                "summary": "подмена отправителя",
                "claims": [],
                "acp_messages": [
                    {
                        "to_role": "orchestrator",
                        "type": "TASK_COMPLETED",
                        "from": {"role": "orchestrator"},
                        "payload": {
                            "summary": "x",
                            "claims": [{"type": "other", "evidence_refs": []}],
                            "artifact_refs": [],
                        },
                    }
                ],
            }
        )
    else:  # agent_acl_violation
        completed = {"summary": "готово", "claims": [{"type": "other", "evidence_refs": []}], "artifact_refs": []}
        fix = {
            "finding": "сам себе",
            "severity": "low",
            "file": None,
            "evidence_refs": ["task:00000000-0000-7000-8000-000000000000"],
            "requested_action": "ничего",
            "loop_iteration": 1,
        }
        write_result(
            {
                "agent_result_version": "1.0",
                "summary": "нарушения ACL",
                "claims": [{"type": "other"}],
                "acp_messages": [
                    {"to_role": "reviewer", "type": "FIX_REQUEST", "payload": fix},  # отправитель: роль не может
                    {
                        "to_role": "user",
                        "type": "TASK_COMPLETED",
                        "payload": completed,
                    },  # получатель: только orchestrator
                    {"to_role": "orchestrator", "type": "TASK_COMPLETED", "payload": completed},
                ],
            }
        )
    return finish("итог записан", "other")


AGENT_SCENARIOS = (
    "agent",
    "agent_develop",
    "agent_docs_required",
    "agent_docs_insist",
    "agent_review_approve",
    "agent_review_changes",
    "agent_complete",
    "agent_bad_result",
    "agent_bad_json",
    "agent_spoofed_from",
    "agent_acl_violation",
)


# Решения оркестратора о документации (DECISION_RECORDED.chosen_option), после которых агент не запрашивает её снова.
DOCS_DECISIONS = ("local_index", "docs_cache")


def docs_resolved(prompt: str) -> bool:
    """В промпте есть раздел docs Context Pack или в inbox — решение оркестратора о документации."""
    if any(line.strip() == "# section: docs" for line in prompt.splitlines()):
        return True
    for m in inbox_of(prompt):
        sender = m.get("from")
        payload = m.get("payload")
        if (
            m.get("type") == "DECISION_RECORDED"
            and isinstance(sender, dict)
            and sender.get("role") == "orchestrator"
            and isinstance(payload, dict)
            and payload.get("chosen_option") in DOCS_DECISIONS
        ):
            return True
    return False


def agent_docs_blocked() -> int:
    """Developer без документации: BLOCKED(DOCS_REQUIRED, needed_from=user) оркестратору (ACP §9.4)."""
    write_result(
        {
            "agent_result_version": "1.0",
            "summary": "Нужна документация библиотеки",
            "claims": [{"type": "other", "summary": "требуется документация"}],
            "acp_messages": [
                {
                    "to_role": "orchestrator",
                    "type": "BLOCKED",
                    "payload": {
                        "reason": "DOCS_REQUIRED",
                        "detail": "Для реализации нужна документация используемой библиотеки",
                        "needed_from": "user",
                        "unblock_condition": "документация загружена или решение продолжать по локальному коду",
                    },
                }
            ],
        }
    )
    return finish("нужна документация", "other")


def run_agent(scenario: str, task: dict[str, object], prompt: str) -> int:
    role = role_of(task)
    if scenario in ("agent_docs_required", "agent_docs_insist"):
        # developer без документации и без решения оркестратора — BLOCKED; иначе как сценарий "agent".
        # agent_docs_insist — негативный: developer требует документацию всегда (защита оркестратора от цикла).
        if role == "developer" and (scenario == "agent_docs_insist" or not docs_resolved(prompt)):
            return agent_docs_blocked()
        scenario = "agent"
    if scenario == "agent":
        scenario = (
            "agent_develop"
            if role == "developer"
            else "agent_review_approve"
            if role in REVIEW_ROLES
            else "agent_complete"
        )
    m = _BASE_COMMIT.search(prompt)
    base = m.group(1) if m else "0" * 40
    if scenario == "agent_develop":
        return agent_develop(task, prompt, base)
    if scenario in ("agent_review_approve", "agent_review_changes"):
        return agent_review(task, prompt, approve=scenario == "agent_review_approve")
    if scenario == "agent_complete":
        return agent_complete(task, prompt, role)
    return agent_bad(scenario)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--prompt-text-file")
    args = parser.parse_args()
    if args.scenario == "ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # до первого вывода — без гонки с отменой
    task = json.loads(Path(args.prompt_file).read_text(encoding="utf-8"))
    emit({"event": "message", "text": f"Получена задача: {task['objective'][:200]}"})
    s = args.scenario
    if s in AGENT_SCENARIOS:
        prompt = Path(args.prompt_text_file).read_text(encoding="utf-8") if args.prompt_text_file else ""
        return run_agent(s, task, prompt)
    if s == "success_diff":
        Path("FAKE_CHANGE.md").write_text(f"# Изменение\n\n{task['objective']}\n", encoding="utf-8")
        emit({"event": "tool", "tool_kind": "file_write", "phase": "completed", "target": "FAKE_CHANGE.md"})
        emit(
            {
                "event": "result",
                "claims": [{"type": "implementation_completed", "summary": "создан FAKE_CHANGE.md"}],
                "usage": None,
            }
        )
        return 0
    if s == "usage_reported":
        emit(
            {
                "event": "result",
                "claims": [{"type": "analysis_completed"}],
                "usage": {"input_tokens": 120, "output_tokens": 30},
            }
        )
        return 0
    if s == "nonzero_exit":
        sys.stderr.write("fatal: something went wrong\n")
        return 1
    if s == "timeout":
        time.sleep(3600)
        return 0
    if s == "chatty_hang":
        while True:
            emit({"event": "message", "text": "работаю..."})
            time.sleep(0.05)
    if s == "auth_required":
        sys.stderr.write("FAKE-AUTH: login required\n")
        return 3
    if s == "rate_limited":
        sys.stderr.write("FAKE-LIMIT: usage limit reached, retry after 120s\n")
        return 4
    if s == "garbage_output":
        sys.stdout.write("this is not json\n{broken\n")
        return 0
    if s == "secret_leak":
        emit({"event": "message", "text": "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789 password=hunter2hunter2"})
        sys.stderr.write("AKIAABCDEFGHIJKLMNOP\n")
        emit({"event": "result", "claims": [{"type": "analysis_completed"}], "usage": None})
        return 0
    if s == "live_output":
        # Живой вывод (API_DESIGN §14): нумерованные сообщения, stderr с ANSI и секретом, разрезанным между
        # двумя записями (построчная буферизация), скрытое рассуждение (не должно попасть никуда), инструмент.
        emit({"event": "message", "type": "reasoning", "text": "HIDDEN-REASONING-CANARY думаю о задаче"})
        for i in range(1, 31):
            emit({"event": "message", "text": f"live line {i:02d}"})
            # После первых строк процесс заметно «работает»: проверка живого вывода (чанк по SSE раньше
            # завершения процесса) не должна зависеть от загрузки хоста и интервала опроса outbox.
            time.sleep(2.0 if i == 5 else 0.02)
        sys.stderr.write("\x1b[31mwarn\x1b[0m: token=ghp_SPLITsplitSPLITsplit")
        sys.stderr.flush()
        time.sleep(0.05)
        sys.stderr.write("SPLIT0123456789 done\n")
        sys.stderr.flush()
        emit({"event": "tool", "tool_kind": "command", "phase": "completed", "target": "npm test"})
        emit({"event": "result", "claims": [{"type": "analysis_completed"}], "usage": None})
        return 0
    if s == "claims_without_evidence":
        emit({"event": "result", "claims": [{"type": "tests_passed", "summary": "все тесты прошли"}], "usage": None})
        return 0
    if s == "big_output":
        line = "x" * 1000
        for _ in range(5000):
            sys.stdout.write(json.dumps({"event": "message", "text": line}) + "\n")
        sys.stdout.flush()
        emit({"event": "result", "claims": [{"type": "other"}], "usage": None})
        return 0
    if s == "ignore_sigterm":
        time.sleep(3600)
        return 0
    if s == "spawn_child":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        Path("child.pid").write_text(str(child.pid))
        time.sleep(3600)
        return 0
    if s == "env_dump":
        emit({"event": "message", "text": json.dumps(sorted(os.environ))})
        emit({"event": "result", "claims": [{"type": "other"}], "usage": None})
        return 0
    sys.stderr.write(f"unknown scenario {s}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

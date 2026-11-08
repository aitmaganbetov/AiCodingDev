"""P5-06: ранжирование, бюджет, stale-маркировка и ключ кэша Context Pack."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aicd_code_intelligence.context.ranking import Budget, Fragment, Section, SourceKind, content_hash, rank, select

BASE = "a" * 40
OTHER = "b" * 40


def frag(
    path: str,
    kind: SourceKind = "task_relevance",
    *,
    score: float = 0.0,
    commit: str = BASE,
    text: str = "x" * 10,
    section: Section = "code",
    lines: tuple[int, int] | None = (1, 2),
) -> Fragment:
    return Fragment(
        section=section,
        kind=kind,
        path=path,
        commit_sha=commit,
        text=text,
        score=score,
        start_line=lines[0] if lines else None,
        end_line=lines[1] if lines else None,
    )


def test_rank_order_exact_dependency_relevance_recency() -> None:
    ordered = rank(
        [frag("d.py", "recency"), frag("c.py", "task_relevance"), frag("a.py", "exact"), frag("b.py", "dependency")]
    )
    assert [f.path for f in ordered] == ["a.py", "b.py", "c.py", "d.py"]
    by_score = rank([frag("low.py", score=0.1), frag("high.py", score=0.9)])
    assert [f.path for f in by_score] == ["high.py", "low.py"]


def test_fragment_of_other_commit_is_excluded_or_marked_stale() -> None:
    fresh, old = frag("a.py", "exact"), frag("b.py", "exact", commit=OTHER)
    excluded = select([fresh, old], base_commit=BASE, budget=Budget(10_000, 10))
    assert [f.path for f in excluded.fragments] == ["a.py"]
    assert excluded.omitted == [
        {"section": "code", "path": "b.py", "kind": "exact", "commit": OTHER, "reason": "stale_commit"}
    ]
    marked = select([fresh, old], base_commit=BASE, budget=Budget(10_000, 10), stale_policy="mark")
    manifest = marked.manifest(base_commit=BASE, policy_version=1, stale_policy="mark")
    assert {f["path"]: f["stale"] for f in manifest["fragments"]} == {"a.py": False, "b.py": True}


def test_budget_bytes_files_and_fragment_size() -> None:
    sel = select(
        [frag("a.py", "exact", text="a" * 60), frag("b.py", "dependency", text="b" * 60), frag("c.py", text="c" * 10)],
        base_commit=BASE,
        budget=Budget(max_bytes=100, max_files=5),
    )
    assert [f.path for f in sel.fragments] == ["a.py", "c.py"] and sel.size_bytes == 70
    assert sel.omitted[0]["reason"] == "max_bytes"
    files = select([frag(f"{i}.py") for i in range(5)], base_commit=BASE, budget=Budget(10_000, 2))
    assert len(files.fragments) == 2 and {o["reason"] for o in files.omitted} == {"max_files"}
    big = select([frag("big.py", text="x" * 100)], base_commit=BASE, budget=Budget(10_000, 2, max_fragment_bytes=50))
    assert big.fragments == [] and big.omitted[0]["reason"] == "fragment_too_large"
    same_file = select(
        [frag("a.py", lines=(1, 2)), frag("a.py", lines=(5, 9))], base_commit=BASE, budget=Budget(10_000, 1)
    )
    assert len(same_file.fragments) == 2, "несколько фрагментов одного файла занимают один слот файла"


def test_duplicates_keep_best_rank() -> None:
    sel = select([frag("a.py", "task_relevance"), frag("a.py", "exact")], base_commit=BASE, budget=Budget(1000, 5))
    assert len(sel.fragments) == 1 and sel.fragments[0].kind == "exact"


def test_content_hash_is_stable_and_sensitive() -> None:
    a = [frag("a.py"), frag("b.py")]
    assert content_hash(commit=BASE, policy_version=1, inputs=a) == content_hash(
        commit=BASE, policy_version=1, inputs=a
    )
    assert content_hash(commit=BASE, policy_version=1, inputs=a) != content_hash(
        commit=BASE, policy_version=2, inputs=a
    )
    assert content_hash(commit=BASE, policy_version=1, inputs=a) != content_hash(
        commit=OTHER, policy_version=1, inputs=a
    )
    changed = [frag("a.py", text="changed"), frag("b.py")]
    assert content_hash(commit=BASE, policy_version=1, inputs=a) != content_hash(
        commit=BASE, policy_version=1, inputs=changed
    )


def test_invalid_budget_rejected() -> None:
    with pytest.raises(ValueError, match="положительным"):
        Budget(0, 1)


@given(
    sizes=st.lists(st.integers(1, 500), min_size=0, max_size=30),
    max_bytes=st.integers(1, 3000),
    max_files=st.integers(1, 10),
    stale=st.lists(st.booleans(), min_size=30, max_size=30),
)
def test_budget_is_never_exceeded_and_no_stale_when_excluded(
    sizes: list[int], max_bytes: int, max_files: int, stale: list[bool]
) -> None:
    fragments = [
        frag(f"f{i % 12}.py", text="x" * n, lines=(i, i + 1), commit=OTHER if stale[i] else BASE)
        for i, n in enumerate(sizes)
    ]
    sel = select(fragments, base_commit=BASE, budget=Budget(max_bytes, max_files))
    assert sel.size_bytes <= max_bytes and len({f.path for f in sel.fragments}) <= max_files
    assert all(f.commit_sha == BASE for f in sel.fragments)
    assert len(sel.fragments) + len(sel.omitted) == len({f.key() for f in fragments})

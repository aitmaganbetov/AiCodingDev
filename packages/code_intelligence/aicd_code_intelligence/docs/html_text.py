"""HTML → Markdown-подобный текст без исполнения активного содержимого (SC-DOC-07, TH-02).

Используется стандартный `html.parser` (новых зависимостей нет; DOM не строится, скрипты не
исполняются, внешние ресурсы не загружаются):

* содержимое `script`, `style`, `iframe`, `object`, `embed`, `form`, `noscript`, `template`, `svg`,
  `math`, `canvas`, `button`, `select`, `textarea`, `head` (кроме `title`) отбрасывается;
* атрибуты не переносятся (в т.ч. обработчики `on*`); ссылки сохраняются только `http(s)`
  (`javascript:`, `data:`, `vbscript:` и прочие — отбрасываются);
* скрытый текст (`hidden`, `aria-hidden="true"`, `display:none`, `visibility:hidden`, `font-size:0`,
  `opacity:0`) — частый вектор prompt injection: в основной текст **не** включается, а возвращается
  отдельно в `hidden_fragments` для пометки снимка как подозрительного;
* лицензия страницы извлекается из `<link rel="license">` и `<meta name="license|dc.rights">`.

Результат остаётся НЕДОВЕРЕННЫМИ данными: агенту передаётся только в fenced-блоке.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

_DROP: Final = frozenset(
    {
        "script", "style", "iframe", "object", "embed", "form", "noscript", "template", "svg", "math",
        "canvas", "button", "select", "textarea", "frame", "frameset", "applet", "audio", "video",
    }
)  # fmt: skip
_VOID: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_BLOCK: Final = frozenset(
    {"p", "div", "section", "article", "main", "header", "footer", "nav", "aside", "table", "tr", "ul", "ol",
     "dl", "dt", "dd", "blockquote", "figure", "figcaption", "details", "summary"}
)  # fmt: skip
_HEADINGS: Final = {f"h{i}": i for i in range(1, 7)}
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d]*[1-9])|opacity\s*:\s*0(?![.\d]*[1-9])",
    re.IGNORECASE,
)
_SAFE_HREF = re.compile(r"^https?://", re.IGNORECASE)


@dataclass
class HtmlText:
    title: str | None
    markdown: str
    license_hint: str | None
    hidden_fragments: list[str] = field(default_factory=list)


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden: list[str] = []
        self._hidden_buf: list[str] = []
        self.title_parts: list[str] = []
        self.license: str | None = None
        self._drop = 0
        self._head = 0
        self._in_title = False
        self._pre = 0
        self._stack: list[tuple[str, bool]] = []  # (tag, скрыт ли элемент)
        self._hidden_depth = 0
        self._href: list[str | None] = []

    # -- helpers
    def _emit(self, s: str) -> None:
        if self._hidden_depth:
            self._hidden_buf.append(s)
        else:
            self.out.append(s)

    def _is_hidden(self, attrs: dict[str, str | None]) -> bool:
        if "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true":
            return True
        return bool(_HIDDEN_STYLE.search(attrs.get("style") or ""))

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k.lower(): v for k, v in attrs_list}
        if tag == "head":
            self._head += 1
        if tag == "link" and (attrs.get("rel") or "").lower() == "license" and attrs.get("href"):
            self.license = self.license or (attrs["href"] or "")[:500]
        if tag == "meta" and (attrs.get("name") or "").lower() in ("license", "dc.rights", "copyright"):
            self.license = self.license or (attrs.get("content") or "")[:500] or None
        if tag in _VOID:
            if tag == "br" and not self._drop:
                self._emit("\n")
            if tag == "hr" and not self._drop:
                self._emit("\n\n---\n\n")
            return
        if tag in _DROP:
            self._drop += 1
        hidden = self._is_hidden(attrs)
        self._stack.append((tag, hidden))
        if hidden:
            self._hidden_depth += 1
        if tag == "title":
            self._in_title = True
        if self._drop or (self._head and tag != "title"):
            return
        if tag in _HEADINGS:
            self._emit("\n\n" + "#" * _HEADINGS[tag] + " ")
        elif tag == "pre":
            self._pre += 1
            self._emit("\n\n```\n")
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in ("td", "th"):
            self._emit(" | ")
        elif tag in _BLOCK:
            self._emit("\n\n")
        elif tag == "a":
            href = attrs.get("href") or ""
            self._href.append(href if _SAFE_HREF.match(href) else None)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("_")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        # Закрываем до совпадающего открывающего тега (устойчивость к несбалансированной разметке).
        idx = next((i for i in range(len(self._stack) - 1, -1, -1) if self._stack[i][0] == tag), None)
        if idx is None:
            return
        while len(self._stack) > idx:
            open_tag, hidden = self._stack.pop()
            self._close(open_tag, hidden)

    def _close(self, tag: str, hidden: bool) -> None:
        if tag == "title":
            self._in_title = False
        visible = not self._drop and not (self._head and tag != "head")
        if visible:
            if tag in _HEADINGS:
                self._emit("\n\n")
            elif tag == "pre":
                self._pre = max(0, self._pre - 1)
                self._emit("\n```\n\n")
            elif tag == "code" and not self._pre:
                self._emit("`")
            elif tag in _BLOCK:
                self._emit("\n\n")
            elif tag == "a":
                href = self._href.pop() if self._href else None
                if href:
                    self._emit(f" ({href})")
            elif tag in ("strong", "b"):
                self._emit("**")
            elif tag in ("em", "i"):
                self._emit("_")
        elif tag == "a" and self._href:
            self._href.pop()
        if tag in _DROP:
            self._drop = max(0, self._drop - 1)
        if tag == "head":
            self._head = max(0, self._head - 1)
        if hidden:
            self._hidden_depth = max(0, self._hidden_depth - 1)
            if not self._hidden_depth:
                fragment = " ".join("".join(self._hidden_buf).split())
                if fragment:
                    self.hidden.append(fragment[:2000])
                self._hidden_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._drop or self._head:
            return
        self._emit(data if self._pre else re.sub(r"\s+", " ", data))


def html_to_text(html: str) -> HtmlText:
    parser = _Converter()
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    title = " ".join("".join(parser.title_parts).split()) or None
    return HtmlText(title=title, markdown=text, license_hint=parser.license, hidden_fragments=parser.hidden)

# -*- coding: utf-8 -*-
"""QTextDocument(HTML) → Markdown 的轻量转换。

Qt 6 的 Python 绑定没有 toMarkdown()，所以这里自己走 block / fragment：
1. block 级别：标题（headingLevel）、无序/有序列表、引用块、空行；
2. fragment 级别：粗体、斜体、下划线转 <u>、等宽字体转行内代码。
不追求 100% 覆盖，够日常记事导出到 Obsidian / Typora 就行。
"""
from __future__ import annotations

from PySide6.QtGui import QFont, QTextDocument, QTextListFormat


def _is_bold(fmt) -> bool:
    return fmt.fontWeight() >= QFont.Bold


def _is_code(fmt) -> bool:
    try:
        if fmt.fontFixedPitch():
            return True
        names = []
        try:
            names = [str(f) for f in (fmt.fontFamilies() or [])]
        except Exception:
            pass
        if not names:
            names = [str(fmt.fontFamily() or "")]
        s = " ".join(names).lower()
        return any(k in s for k in ("mono", "consolas", "courier", "menlo", "code"))
    except Exception:
        return False


def _frag_text(frag) -> str:
    t = frag.text().replace("\u2028", "\n").replace("\u2029", "\n")
    return t


def _wrap(t: str, fmt) -> str:
    if not t:
        return t
    # 纯空白不加标记
    if not t.strip():
        return t
    if _is_code(fmt):
        return "`%s`" % t
    if _is_bold(fmt) and fmt.fontItalic():
        return "***%s***" % t
    if _is_bold(fmt):
        return "**%s**" % t
    if fmt.fontItalic():
        return "*%s*" % t
    if fmt.fontUnderline():
        return "<u>%s</u>" % t
    return t


def _block_inline(block) -> str:
    parts = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid():
            parts.append(_wrap(_frag_text(frag), frag.charFormat()))
        it += 1
    return "".join(parts).strip("\n")


def html_to_md(html: str) -> str:
    doc = QTextDocument()
    doc.setHtml(html or "")
    out: list = []

    block = doc.firstBlock()
    ordered_no = 0
    while block.isValid():
        text = block.text().strip()
        lf = block.textList()
        lvl = block.blockFormat().headingLevel()

        if not text:
            # 空行只在需要时补一个（避免连续空行）
            if out and out[-1] != "":
                out.append("")
            ordered_no = 0 if not lf else ordered_no
            block = block.next()
            continue

        inline = _block_inline(block) or text
        if lvl and lvl > 0:
            out.append("#" * min(lvl, 6) + " " + text)
            ordered_no = 0
        elif lf is not None:
            style = lf.format().style()
            if style == QTextListFormat.ListDecimal:
                ordered_no += 1
                out.append("%d. %s" % (ordered_no, inline))
            else:
                out.append("- " + inline)
        elif block.blockFormat().leftMargin() >= 20 and not lf:
            out.append("> " + inline)
            ordered_no = 0
        else:
            out.append(inline)
            ordered_no = 0
        block = block.next()

    # 去掉末尾空行
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def plain_to_md(text: str) -> str:
    """纯文本兜底：原样返回，行尾空白清掉。"""
    return "\n".join(line.rstrip() for line in (text or "").splitlines()).strip()

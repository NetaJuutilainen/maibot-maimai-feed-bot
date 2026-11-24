"""投喂插件 — 工具函数与常量。"""

from __future__ import annotations

import os
import unicodedata
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

# 数据文件的正位是宿主为插件分配的持久数据目录（ctx.paths.data_dir =
# data/plugins/<plugin_id>/），运行时由 plugin.on_load 解析后传给 db 层。
# 1.4.x 及之前数据写在插件目录内的 data/ 下；下面几个常量只用于
# on_load 时的一次性旧库搬迁（搬迁后旧文件保留，作为回滚保险）。
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_DATA_DIR = os.path.join(PLUGIN_DIR, "data")
LEGACY_DB_PATH = os.path.join(LEGACY_DATA_DIR, "feed_bot.db")

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


@contextmanager
def atomic_write(path: str, mode: str = "w", encoding: str | None = None):
    """原子写入：先写临时文件，成功后 os.replace 替换。"""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, mode, encoding=encoding) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


_OUTER_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def strip_outer_quotes(text: str) -> str:
    """剥除首尾成对的外层引号（保留正文内部的引号）。"""
    s = str(text or "").strip()
    while len(s) >= 2:
        stripped = False
        for left, right in _OUTER_QUOTE_PAIRS:
            if s.startswith(left) and s.endswith(right):
                inner = s[1:-1]
                # 对称引号（" / '）若正文内部也出现同一引号，多为并列分段引用，不盲目剥除
                if left == right and left in inner:
                    break
                if left != right and inner.count(left) != inner.count(right):
                    break
                s = inner.strip()
                stripped = True
                break
        if not stripped:
            break
    return s


def is_likely_emoji(text: str) -> bool:
    """判断文本是否可能是单个 emoji 或 emoji 序列。"""
    if not text or text.isascii():
        return False
    # 去除变体选择符和零宽连接符后检测
    stripped = text
    for ch in ("\ufe0f", "\u200d", "\ufe0e"):  # VS-16, ZWJ, VS-15
        stripped = stripped.replace(ch, "")
    if not stripped:
        return True  # 纯修饰符序列，视为 emoji
    # 所有剩余字符都是 Symbol/Other 或 Modifier Symbol
    return all(unicodedata.category(c) in ("So", "Sk") for c in stripped)


def extract_nickname(message: dict[str, Any] | None) -> str:
    """从消息字典中提取发送者昵称。"""
    if not message or not isinstance(message, dict):
        return ""
    message_info = message.get("message_info", {})
    if isinstance(message_info, dict):
        user_info = message_info.get("user_info", {})
        if isinstance(user_info, dict):
            cardname = str(user_info.get("user_cardname") or "").strip()
            if cardname:
                return cardname
            nickname = str(user_info.get("user_nickname") or "").strip()
            if nickname:
                return nickname
    return ""

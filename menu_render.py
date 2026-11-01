"""投喂插件 — 商店菜单图片渲染（PIL 纯本地绘制，无浏览器依赖）。

仅在宿主运行环境（Windows，自带微软雅黑与 Segoe UI Emoji 彩色字体）下可用；
字体缺失时抛异常，由调用方回退文本菜单。
emoji 字体优先使用插件目录 fonts/seguiemj.ttf（可自带新版本，
覆盖系统旧字体缺 Unicode 11+ 字形的问题），回退系统字体。
"""

from __future__ import annotations

import base64
import io
import os
from typing import Any

try:  # 生产环境走插件配置常量；本地直跑（无 maibot_sdk）时回退同值字面量
    from .config import STYLE_CLEAR_ALL_LABEL, STYLE_CLEAR_ALL_MARKER
except ImportError:  # pragma: no cover
    STYLE_CLEAR_ALL_MARKER = "__clear_all_styles__"
    STYLE_CLEAR_ALL_LABEL = "清除全部投喂风格"

_IMG_W = 1240
_HEAD_H = 128
_ROW_PAD = 14
_FOOT_H = 64

_COLOR_BG = "#FFF6E5"
_COLOR_HEAD_BG = "#FFEDD5"
_COLOR_ROW_A = "#FFFDF7"
_COLOR_ROW_B = "#FFF3DE"
_COLOR_ROW_LINE = "#F0DFC0"
_COLOR_MAIN = "#4A2F10"
_COLOR_SUB = "#A07B4F"
_COLOR_DESC = "#8A6D4A"
_COLOR_PRICE = "#9A4A08"
_COLOR_TAG_CLEAR_BG = "#DDF4E4"
_COLOR_TAG_CLEAR_FG = "#1A7F37"
_COLOR_TAG_STYLE_BG = "#FFD9A8"
_COLOR_TAG_STYLE_FG = "#B45309"
_COLOR_SAT_POS = "#1A9E4B"
_COLOR_SAT_NEG = "#E04B3C"

_font_cache: dict[tuple[str, int], Any] = {}

# 字体查找顺序：插件目录 fonts/（可自带新版 seguiemj，见 .gitignore）→ 系统字体。
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_FONT_PATHS = {
    "cn": [os.path.join(PLUGIN_DIR, "fonts", "msyh.ttc"), r"C:\Windows\Fonts\msyh.ttc"],
    "cnb": [os.path.join(PLUGIN_DIR, "fonts", "msyhbd.ttc"), r"C:\Windows\Fonts\msyhbd.ttc"],
    "emoji": [os.path.join(PLUGIN_DIR, "fonts", "seguiemj.ttf"), r"C:\Windows\Fonts\seguiemj.ttf"],
}


def _font(key: str, size: int) -> Any:
    """按 (字体名, 字号) 缓存加载字体。"""
    cache_key = (key, size)
    if cache_key not in _font_cache:
        from PIL import ImageFont

        path = next((p for p in _FONT_PATHS[key] if os.path.exists(p)), None)
        if path is None:
            raise FileNotFoundError(f"渲染菜单需要字体：{_FONT_PATHS[key]}")
        kwargs = {"size": size}
        if key == "emoji":
            kwargs["index"] = 0
        _font_cache[cache_key] = ImageFont.truetype(path, **kwargs)
    return _font_cache[cache_key]


def _wrap_by_width(draw: Any, text: str, font: Any, max_w: float) -> list[str]:
    """中文友好的逐字换行：按像素宽度累积，超宽则折行。"""
    lines: list[str] = []
    current = ""
    for ch in text:
        if draw.textlength(current + ch, font=font) <= max_w:
            current += ch
        else:
            if current:
                lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines or [""]


def _is_emoji_cp(cp: int) -> bool:
    """emoji 字符启发式判定（VS16/keycap 延续前段，见 _split_mixed）。"""
    if cp in (0xFE0F, 0x20E3):
        return True
    if 0x1F000 <= cp <= 0x1FAFF:
        return True
    if 0x2600 <= cp <= 0x27BF:
        return True
    if 0x2B00 <= cp <= 0x2BFF:
        return True
    return False


def _split_mixed(text: str) -> list[tuple[bool, str]]:
    """把文本拆成 (是否emoji, 片段) 列表；VS16/keycap 并入前一段。"""
    raw: list[list[Any]] = []
    for ch in text:
        is_e = _is_emoji_cp(ord(ch))
        if raw and raw[-1][0] == is_e:
            raw[-1][1] += ch
        else:
            raw.append([is_e, ch])
    # VS16/keycap 独立成段时并入前段（它只是前一个字符的样式修饰）
    merged: list[tuple[bool, str]] = []
    for is_e, t in raw:
        if is_e and len(t) == 1 and t in ('\ufe0f', '\u20e3') and merged:
            pe, pt = merged[-1]
            merged[-1] = (pe, pt + t)
        else:
            merged.append((bool(is_e), t))
    return merged


def _mixed_lines(draw: Any, text: str, cn_f: Any, emj_f: Any, max_w: float) -> list[list[tuple[bool, str]]]:
    """混排逐字换行：返回行列表，每行是 (是否emoji, 片段) 列表。

    展示层微调：紧贴 emoji 的空格跳过不画（emoji 字形自带留白，
    再叠加空格会出现"奇怪的空隙"）；只影响菜单图片，不改原文。
    """
    lines: list[list[tuple[bool, str]]] = []
    cur: list[tuple[bool, str]] = []
    cur_w = 0.0
    chars = list(text)
    for i, ch in enumerate(chars):
        # 紧贴 emoji 的空格：前一个或后一个字符是 emoji 时跳过
        if ch == ' ':
            prev_e = i > 0 and _is_emoji_cp(ord(chars[i - 1]))
            next_e = i + 1 < len(chars) and _is_emoji_cp(ord(chars[i + 1]))
            if prev_e or next_e:
                continue
        is_e = _is_emoji_cp(ord(ch))
        if is_e:
            # emoji 字形自带右侧留白（advance 比可见图形宽），按可见宽度计量
            w = draw.textbbox((0, 0), ch, font=emj_f, embedded_color=True)[2]
        else:
            w = draw.textlength(ch, font=cn_f)
        if cur and cur_w + w > max_w:
            lines.append(cur)
            cur = []
            cur_w = 0.0
        if cur and cur[-1][0] == is_e:
            cur[-1] = (is_e, cur[-1][1] + ch)
        else:
            cur.append((is_e, ch))
        cur_w += w
    if cur:
        lines.append(cur)
    return lines or [[(False, '')]]


def _draw_mixed_line(draw: Any, x: float, y: float, segs: list[tuple[bool, str]],
                     cn_f: Any, emj_f: Any, fill: str, emj_scale: float = 1.1) -> None:
    """绘制一行混排片段：文字段用中文字体，emoji 段用彩色 emoji 字体。

    emoji 段绘制后按可见宽度（bbox）推进，消除字形自带右侧留白造成的"空隙"。
    """
    cx = x
    for is_e, t in segs:
        if is_e:
            try:
                draw.text((cx, y), t, font=emj_f, fill=fill, embedded_color=True)
                bbox = draw.textbbox((cx, y), t, font=emj_f, embedded_color=True)
                cx = bbox[2]
                continue
            except Exception:
                pass
        draw.text((cx, y), t, font=cn_f, fill=fill)
        cx += draw.textlength(t, font=cn_f)


def _effect_text(style_prompt: str, style_duration_minutes: int) -> str:
    """与文本菜单 [效果：…] 的语义保持一致。"""
    prompt = str(style_prompt or "").strip()
    if prompt == STYLE_CLEAR_ALL_MARKER:
        return STYLE_CLEAR_ALL_LABEL
    if prompt:
        try:
            duration = int(style_duration_minutes)
        except (TypeError, ValueError):
            duration = 0
        return f"{prompt}（{duration}分钟）" if duration > 0 else prompt
    return "无特殊效果"


def render_shop_menu_png(
    global_items: list[tuple[Any, ...]],
    group_items: list[tuple[Any, ...]],
    *,
    max_batch: int = 99,
) -> bytes:
    """渲染商店菜单 PNG。

    行元组字段序与 handle_shop 的查询一致：
    (name, emoji, price, description, category, satiety_bonus,
     style_prompt, style_duration_minutes)
    """
    from PIL import Image, ImageDraw

    f_title = _font("cnb", 38)
    f_sub = _font("cn", 19)
    f_name = _font("cnb", 30)
    f_desc = _font("cn", 19)
    f_effect = _font("cn", 19)
    f_price = _font("cnb", 30)
    f_sat = _font("cnb", 20)
    f_tag = _font("cnb", 17)
    f_foot = _font("cn", 18)
    f_emoji = _font("emoji", 52)
    f_emoji_sm = _font("emoji", 22)
    f_section = _font("cnb", 24)

    # 预解析行：emoji, name, price, sat, tag, effect_lines, desc_lines
    parsed: list[dict[str, Any]] = []

    def parse(items: list[tuple[Any, ...]]) -> None:
        for name, emoji, price, desc, _cat, sat, style_prompt, style_dur in items:
            prompt = str(style_prompt or "").strip()
            if prompt == STYLE_CLEAR_ALL_MARKER:
                tag, tagkind = STYLE_CLEAR_ALL_LABEL, "clear"
            elif prompt:
                tag, tagkind = "风格道具", "style"
            else:
                tag, tagkind = "无特殊效果", "plain"
            effect = _effect_text(style_prompt, style_dur)
            parsed.append({
                "emoji": str(emoji or ""),
                "name": str(name),
                "price": int(price),
                "sat": float(sat),
                "tag": tag,
                "tagkind": tagkind,
                "desc": str(desc or ""),
                "effect": effect,
                "has_style": bool(prompt),
            })

    parse(global_items)
    group_start = len(parsed)
    parse(group_items)

    # emoji 区宽度：取所有行 emoji 串的最大渲染宽度（多 emoji 串如 🈲🔞🍆 会很宽，
    # 统一按最宽对齐，避免压到名称；上限 230 防极端数据拉爆版面）
    probe = Image.new("RGB", (_IMG_W, 10))
    probe_draw = ImageDraw.Draw(probe)
    emoji_w = 72.0
    for row in parsed:
        if row["emoji"]:
            emoji_w = max(emoji_w, probe_draw.textlength(row["emoji"], font=f_emoji))
    emoji_w = min(emoji_w + 10, 230)
    name_x = 30 + emoji_w + 16

    # 量高：逐行 = 名称区(38) + 描述行数*25 + 效果行数*25 + 上下 pad（描述/效果按混排折行）
    text_max_w = _IMG_W - name_x - 175  # 右侧留价格/饱食度区
    heights: list[int] = []
    desc_segs: list[list[list[tuple[bool, str]]]] = []
    eff_segs: list[list[list[tuple[bool, str]]]] = []
    for idx, row in enumerate(parsed):
        d_lines = _mixed_lines(probe_draw, row["desc"], f_desc, f_emoji_sm, text_max_w)
        e_lines = _mixed_lines(probe_draw, "效果：" + row["effect"], f_effect, f_emoji_sm, text_max_w)
        desc_segs.append(d_lines)
        eff_segs.append(e_lines)
        h = _ROW_PAD + 38 + len(d_lines) * 25 + len(e_lines) * 25 + 12
        heights.append(h)

    # 行间分节空隙 + 分节标题高度（两段各 48px，必须计入画布总高，否则末行被裁）
    gaps = 22 if (global_items and group_items) else 0
    n_sections = (1 if global_items else 0) + (1 if group_items else 0)
    H = _HEAD_H + sum(heights) + gaps + 48 * n_sections + _FOOT_H
    img = Image.new("RGB", (_IMG_W, H), _COLOR_BG)
    d = ImageDraw.Draw(img)

    def emoji_at(ch: str, x: float, y: float, font: Any) -> None:
        if not ch:
            return
        try:
            d.text((x, y), ch, font=font, fill=_COLOR_MAIN, embedded_color=True)
        except Exception:
            d.text((x, y), ch, font=f_desc, fill=_COLOR_MAIN)

    # 标题带
    d.rectangle([0, 0, _IMG_W, _HEAD_H], fill=_COLOR_HEAD_BG)
    emoji_at("🍞", 36, 38, f_emoji_sm)
    d.text((92, 26), "麦麦要吃那个！ · 商店菜单", font=f_title, fill=_COLOR_MAIN)
    d.text((94, 82), "/购买 <道具名> [x数量]  ·  /投喂 <道具名> [x数量]  ·  单次最多 %d 个" % max_batch,
           font=f_sub, fill=_COLOR_SUB)

    def draw_items(y: float, rows: list[int], section: str | None) -> float:
        if section:
            emoji_at("🌐" if "全局" in section else "🏠", 36, y + 12, f_emoji_sm)
            d.text((92, y + 12), section, font=f_section, fill=_COLOR_MAIN)
            y += 48
        for idx in rows:
            row = parsed[idx]
            h = heights[idx]
            d.rectangle([0, y, _IMG_W, y + h],
                        fill=_COLOR_ROW_A if idx % 2 == 0 else _COLOR_ROW_B)
            d.line([0, y, _IMG_W, y], fill=_COLOR_ROW_LINE, width=1)
            emoji_at(row["emoji"], 30, y + _ROW_PAD + 4, f_emoji)
            # 名称 + tag
            d.text((name_x, y + _ROW_PAD), row["name"], font=f_name, fill=_COLOR_MAIN)
            nw = d.textlength(row["name"], font=f_name)
            tw = d.textlength(row["tag"], font=f_tag)
            tx = name_x + nw + 14
            if row["tagkind"] == "clear":
                tbg, tfg = _COLOR_TAG_CLEAR_BG, _COLOR_TAG_CLEAR_FG
            elif row["tagkind"] == "style":
                tbg, tfg = _COLOR_TAG_STYLE_BG, _COLOR_TAG_STYLE_FG
            else:
                tbg, tfg = "#EFE7DA", _COLOR_SUB
            d.rounded_rectangle([tx, y + _ROW_PAD + 4, tx + tw + 20, y + _ROW_PAD + 32],
                                radius=14, fill=tbg)
            d.text((tx + 10, y + _ROW_PAD + 9), row["tag"], font=f_tag, fill=tfg)
            # 描述（emoji 混排）
            ty = y + _ROW_PAD + 38
            for segs in desc_segs[idx]:
                _draw_mixed_line(d, name_x, ty, segs, f_desc, f_emoji_sm, _COLOR_DESC)
                ty += 25
            # 效果（完整提示词，emoji 混排）
            eff_fg = _COLOR_TAG_CLEAR_FG if row["tagkind"] == "clear" else _COLOR_PRICE
            for segs in eff_segs[idx]:
                _draw_mixed_line(d, name_x, ty, segs, f_effect, f_emoji_sm, eff_fg)
                ty += 25
            # 价格 + 饱食度（右对齐，正绿负红、加大加粗）
            pt = f"{row['price']} 积分"
            pw = d.textlength(pt, font=f_price)
            d.text((_IMG_W - 40 - pw, y + _ROW_PAD - 2), pt, font=f_price, fill=_COLOR_PRICE)
            sat = row["sat"]
            st = ("+" if sat >= 0 else "") + (f"{sat:g}") + " 饱食度"
            sw = d.textlength(st, font=f_sat)
            d.text((_IMG_W - 40 - sw, y + _ROW_PAD + 38), st, font=f_sat,
                   fill=_COLOR_SAT_POS if sat >= 0 else _COLOR_SAT_NEG)
            y += h
        return y

    y = draw_items(_HEAD_H, list(range(group_start)), "全局道具" if global_items else None)
    if global_items and group_items:
        d.line([0, y + 8, _IMG_W, y + 8], fill=_COLOR_ROW_LINE, width=2)
        y += 20
    y = draw_items(y, list(range(group_start, len(parsed))), "本群专属" if group_items else None)

    # 注脚
    y += 8
    d.rectangle([0, y, _IMG_W, y + _FOOT_H], fill=_COLOR_HEAD_BG)
    d.text((36, y + 18), "价格与效果以 /商店 实时为准 · 投喂风格经回复注入，不影响行为逻辑", font=f_foot, fill=_COLOR_SUB)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_shop_menu_base64(
    global_items: list[tuple[Any, ...]],
    group_items: list[tuple[Any, ...]],
    *,
    max_batch: int = 99,
) -> str:
    """渲染并返回 PNG 的 base64（供 ctx.send.image 使用）。"""
    return base64.b64encode(
        render_shop_menu_png(global_items, group_items, max_batch=max_batch)
    ).decode()

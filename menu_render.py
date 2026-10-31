"""投喂插件 — 商店菜单图片渲染（PIL 纯本地绘制，无浏览器依赖）。

仅在宿主运行环境（Windows，自带微软雅黑与 Segoe UI Emoji 彩色字体）下可用；
字体缺失时抛异常，由调用方回退文本菜单。
"""

from __future__ import annotations

import base64
import io
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
_COLOR_PRICE = "#B45309"
_COLOR_TAG_CLEAR_BG = "#DDF4E4"
_COLOR_TAG_CLEAR_FG = "#1A7F37"
_COLOR_TAG_STYLE_BG = "#FFD9A8"
_COLOR_TAG_STYLE_FG = "#B45309"
_COLOR_SAT_POS = "#1A7F37"
_COLOR_SAT_NEG = "#C2543A"

_font_cache: dict[tuple[str, int], Any] = {}


def _font(key: str, size: int) -> Any:
    """按 (字体名, 字号) 缓存加载 Windows 自带字体。"""
    import os

    paths = {
        "cn": r"C:\Windows\Fonts\msyh.ttc",
        "cnb": r"C:\Windows\Fonts\msyhbd.ttc",
        "emoji": r"C:\Windows\Fonts\seguiemj.ttf",
    }
    cache_key = (key, size)
    if cache_key not in _font_cache:
        from PIL import ImageFont

        path = paths[key]
        if not os.path.exists(path):
            raise FileNotFoundError(f"渲染菜单需要 Windows 字体：{path}")
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
    f_price = _font("cnb", 25)
    f_sat = _font("cn", 17)
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

    # 量高：逐行 = 名称区(38) + 描述行数*25 + 效果行数*25 + 上下 pad
    probe = Image.new("RGB", (_IMG_W, 10))
    probe_draw = ImageDraw.Draw(probe)
    text_max_w = _IMG_W - 126 - 170  # 左侧留 emoji+名区，右侧留价格区
    heights: list[int] = []
    for idx, row in enumerate(parsed):
        desc_lines = _wrap_by_width(probe_draw, row["desc"], f_desc, text_max_w)
        eff_lines = _wrap_by_width(probe_draw, "效果：" + row["effect"], f_effect, text_max_w)
        h = _ROW_PAD + 38 + len(desc_lines) * 25 + len(eff_lines) * 25 + 10
        heights.append(h)

    # 行间分节空隙
    gaps = 22 if (global_items and group_items) else 0
    H = _HEAD_H + sum(heights) + gaps + _FOOT_H
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
            emoji_at(row["emoji"], 34, y + _ROW_PAD + 6, f_emoji)
            # 名称 + tag
            d.text((126, y + _ROW_PAD), row["name"], font=f_name, fill=_COLOR_MAIN)
            nw = d.textlength(row["name"], font=f_name)
            tw = d.textlength(row["tag"], font=f_tag)
            tx = 126 + nw + 14
            if row["tagkind"] == "clear":
                tbg, tfg = _COLOR_TAG_CLEAR_BG, _COLOR_TAG_CLEAR_FG
            elif row["tagkind"] == "style":
                tbg, tfg = _COLOR_TAG_STYLE_BG, _COLOR_TAG_STYLE_FG
            else:
                tbg, tfg = "#EFE7DA", _COLOR_SUB
            d.rounded_rectangle([tx, y + _ROW_PAD + 4, tx + tw + 20, y + _ROW_PAD + 32],
                                radius=14, fill=tbg)
            d.text((tx + 10, y + _ROW_PAD + 9), row["tag"], font=f_tag, fill=tfg)
            # 描述
            desc_lines = _wrap_by_width(d, row["desc"], f_desc, text_max_w)
            ty = y + _ROW_PAD + 38
            for line in desc_lines:
                d.text((126, ty), line, font=f_desc, fill=_COLOR_DESC)
                ty += 25
            # 效果（完整提示词）
            eff_lines = _wrap_by_width(d, "效果：" + row["effect"], f_effect, text_max_w)
            eff_fg = _COLOR_TAG_CLEAR_FG if row["tagkind"] == "clear" else _COLOR_PRICE
            for line in eff_lines:
                d.text((126, ty), line, font=f_effect, fill=eff_fg)
                ty += 25
            # 价格 + 饱食度（右对齐）
            pt = f"{row['price']} 积分"
            pw = d.textlength(pt, font=f_price)
            d.text((_IMG_W - 40 - pw, y + _ROW_PAD + 2), pt, font=f_price, fill=_COLOR_PRICE)
            sat = row["sat"]
            st = ("+" if sat >= 0 else "") + (f"{sat:g}") + " 饱食度"
            sw = d.textlength(st, font=f_sat)
            d.text((_IMG_W - 40 - sw, y + _ROW_PAD + 40), st, font=f_sat,
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

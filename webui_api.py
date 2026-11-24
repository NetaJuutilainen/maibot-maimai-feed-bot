"""投喂插件 — WebUI「身体联动管理页」API。

全部 public=False（webui.json 只能绑定本插件 API，不要求 public）。
页面：webui/mapping.html（custom 页，沙箱 iframe + window.maibot 桥）。
"""

from __future__ import annotations

import os
import time
from typing import Any

from maibot_sdk import API

from .config import STYLE_CLEAR_ALL_MARKER
from .utils import PLUGIN_DIR, strip_outer_quotes

_BODY_KINDS = ("meal", "snack", "drink")


class WebUIApiMixin:
    """WebUI 页面绑定的静态 API（短名，宿主解析为 {plugin_id}.{name}）。"""

    @API("webui_mapping_overview", description="WebUI：映射总览（道具+映射+校验+词表+身体状态）", version="1")
    async def webui_mapping_overview(self, refresh: int = 0, **kwargs: Any) -> dict[str, Any]:
        """页面主查询：全部在售道具 + 映射现状 + 词表（供页内即时候选/校验）+ 身体快照。

        refresh=1 时强制重拉身体词表（默认走 TTL 缓存）。
        """
        rows = await self.db.execute(
            """
            SELECT item_id, name, emoji, price, description, satiety_bonus,
                   scope, group_id, body_food, body_kind, body_grams, is_on_sale,
                   style_prompt, style_duration_minutes
            FROM shop_items
            WHERE is_on_sale = 1 OR body_food != ''
            ORDER BY scope DESC, name
            """
        )
        foods = await self._body_link.foods(force=bool(refresh)) or {}
        items: list[dict[str, Any]] = []
        linked = 0
        invalid = 0
        for row in rows or []:
            word = str(row[8] or "").strip()
            valid = bool(word) and self._body_link.resolve_words(word) is not None
            if word:
                linked += 1
                if not valid:
                    invalid += 1
            items.append(
                {
                    "item_id": row[0],
                    "name": row[1],
                    "emoji": row[2],
                    "price": row[3],
                    "description": row[4],
                    "satiety_bonus": row[5],
                    "scope": row[6],
                    "group_id": row[7],
                    "body_food": word,
                    "body_kind": str(row[9] or "").strip() or "snack",
                    "body_grams": float(row[10] or 0.0),
                    "is_on_sale": int(row[11] or 0),
                    "style_prompt": str(row[12] or ""),
                    "style_duration_minutes": int(row[13] or 0),
                    "is_clear_style": str(row[12] or "") == STYLE_CLEAR_ALL_MARKER,
                    "valid": valid,
                }
            )

        body_state = None
        if self._body_link.body_enabled():
            try:
                body_state = await self._body_link.get_state()
            except Exception:
                body_state = None

        groups = await self.db.get_enabled_group_ids()
        group_names = await self._group_name_map()
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "linked_count": linked,
            "invalid_count": invalid,
            "body_enabled": self._body_link.body_enabled(),
            "body_state": body_state,
            "groups": [
                {"group_id": gid, "group_name": group_names.get(gid, "")}
                for gid in sorted(groups)
            ],
            "foods": {
                "labels": foods.get("labels", {}),
                "aliases": foods.get("aliases", {}),
                "base_grams": foods.get("base_grams", {}),
                "volumes": foods.get("volumes", {}),
                "groups": foods.get("groups", {}),
            },
            "config": self._webui_config_values(),
            "config_fields": self._CONFIG_FIELDS,
        }

    async def _group_name_map(self) -> dict[str, str]:
        """群号 → 群名（从宿主群聊流里防御式提取；拿不到回落空串，页面显示群号）。"""
        names: dict[str, str] = {}
        try:
            streams = await self.ctx.chat.get_group_streams()
            for entry in streams or []:
                if not isinstance(entry, dict):
                    continue
                gid = str(entry.get("group_id") or "").strip()
                if not gid:
                    continue
                name = str(
                    entry.get("group_name")
                    or entry.get("nickname")
                    or (entry.get("group_info") or {}).get("group_name")
                    or ""
                ).strip()
                if name:
                    names[gid] = name[:60]
        except Exception:
            pass
        return names

    # ---- 插件设置（页面底部「插件设置」卡片） ----

    _CONFIG_FIELDS: list[dict[str, Any]] = [
        {"section": "bot_attr", "key": "initial_satiety", "label": "初始饱食度", "type": "number", "min": 0, "max": 100},
        {"section": "bot_attr", "key": "satiety_decay_rate", "label": "每小时衰减量", "type": "number", "min": 0, "max": 100},
        {"section": "bot_attr", "key": "seek_feed_threshold", "label": "求投喂阈值", "type": "number", "min": 0, "max": 100},
        {"section": "bot_attr", "key": "seek_feed_cooldown", "label": "求投喂冷却（秒）", "type": "number", "min": 0, "max": 86400},
        {"section": "bot_attr", "key": "quiet_hours", "label": "安静时段（HH:MM-HH:MM，留空不限）", "type": "string"},
        {"section": "sign", "key": "base_points", "label": "签到基础积分", "type": "number", "min": 0, "max": 1000000},
        {"section": "sign", "key": "consecutive_bonus", "label": "连续签到递增", "type": "number", "min": 0, "max": 1000},
        {"section": "sign", "key": "max_consecutive_bonus", "label": "连续签到上限", "type": "number", "min": 0, "max": 1000},
        {"section": "llm", "key": "enabled", "label": "LLM 生成回复（关=只用兜底文案）", "type": "boolean"},
        {"section": "llm", "key": "model", "label": "模型任务名（填任务名不是模型名）", "type": "string"},
        {"section": "llm", "key": "temperature", "label": "LLM 温度", "type": "number", "min": 0, "max": 2},
        {"section": "llm", "key": "max_tokens", "label": "最大 token", "type": "number", "min": 100, "max": 32000},
        {"section": "llm", "key": "timeout_seconds", "label": "LLM 超时（秒）", "type": "number", "min": 5, "max": 600},
        {"section": "style", "key": "max_active_effects", "label": "同时生效风格上限", "type": "number", "min": 1, "max": 10},
        {"section": "style", "key": "expire_notice", "label": "风格失效提示", "type": "boolean"},
        {"section": "shop", "key": "render_image", "label": "商店图片菜单", "type": "boolean"},
        {"section": "shop", "key": "builtin_clear_item", "label": "自动内置清除道具牛奶", "type": "boolean"},
        {"section": "linkage", "key": "body_enabled", "label": "联动麦麦身体", "type": "boolean"},
        {"section": "linkage", "key": "mai_life_enabled", "label": "联动麦生活", "type": "boolean"},
        {"section": "linkage", "key": "max_servings_per_feed", "label": "单次进身上限（份）", "type": "number", "min": 1, "max": 20},
        {"section": "linkage", "key": "max_bac", "label": "BAC 拒收阈值", "type": "number", "min": 0, "max": 1},
        {"section": "linkage", "key": "food_cache_minutes", "label": "词表缓存（分钟）", "type": "number", "min": 1, "max": 1440},
    ]

    def _webui_config_values(self) -> dict[str, dict[str, Any]]:
        """按白名单字段收集当前配置值（供页面渲染设置卡片）。"""
        out: dict[str, dict[str, Any]] = {}
        for f in self._CONFIG_FIELDS:
            sec = out.setdefault(f["section"], {})
            try:
                sec[f["key"]] = getattr(getattr(self.config, f["section"]), f["key"])
            except Exception:
                sec[f["key"]] = None
        return out

    @API("webui_config_get", description="WebUI：白名单插件设置的字段定义与当前值", version="1")
    async def webui_config_get(self, **kwargs: Any) -> dict[str, Any]:
        """设置卡片元数据 + 当前值（也可直接用 overview 里的 config）。"""
        del kwargs
        return {"ok": True, "fields": self._CONFIG_FIELDS, "values": self._webui_config_values()}

    @API("webui_config_save", description="WebUI：保存白名单内的插件设置（写入 config.toml，宿主热重载生效）", version="1")
    async def webui_config_save(self, values_json: str = "", **kwargs: Any) -> dict[str, Any]:
        """保存白名单内的设置项。

        values_json = {"section.key": value, ...}（JSON 字符串，页面侧序列化）。
        只接受 _CONFIG_FIELDS 白名单内的键；类型按白名单转换；越界值夹紧并提示。
        写入插件 config.toml（UTF-8 无 BOM），由宿主配置监视热重载生效。
        """
        del kwargs
        import json as _json

        try:
            payload = _json.loads(str(values_json or "{}"))
        except Exception as exc:
            return {"ok": False, "error": f"values_json 不是合法 JSON：{exc}"}
        if not isinstance(payload, dict) or not payload:
            return {"ok": False, "error": "没有要保存的设置"}

        fields = {(f["section"], f["key"]): f for f in self._CONFIG_FIELDS}
        allowed_keys = set()
        for f in self._CONFIG_FIELDS:
            allowed_keys.add(f["section"] + "." + f["key"])

        updates: dict[tuple[str, str], str] = {}
        applied: list[str] = []
        clamped: list[str] = []
        for full_key, raw in payload.items():
            if full_key not in allowed_keys:
                return {"ok": False, "error": f"设置项不在白名单内：{full_key}"}
            f = fields[(full_key.split(".", 1)[0], full_key.split(".", 1)[1])]
            value = raw
            if f["type"] == "boolean":
                value = bool(raw)
            elif f["type"] == "number":
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{f['label']} 必须是数字"}
                if f.get("min") is not None and value < float(f["min"]):
                    value = float(f["min"])
                    clamped.append(f["label"])
                if f.get("max") is not None and value > float(f["max"]):
                    value = float(f["max"])
                    clamped.append(f["label"])
            else:  # string
                value = str(raw).strip()
                if not value and f["key"] not in ("quiet_hours",):
                    return {"ok": False, "error": f"{f['label']} 不能为空"}
                if len(value) > 200:
                    return {"ok": False, "error": f"{f['label']} 过长（≤200）"}
            updates[(f["section"], f["key"])] = self._format_toml_value(value, f["type"])
            applied.append(f"{f['section']}.{f['key']}")

        config_path = os.path.join(PLUGIN_DIR, "config.toml")
        try:
            with open(config_path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            return {"ok": False, "error": f"读不到 config.toml：{exc}"}

        for (section, key), formatted in updates.items():
            text = self._set_toml_key(text, section, key, formatted)

        try:
            import tomllib  # noqa: F401 — 写入前先验证仍是合法 TOML

            tomllib.loads(text)
        except Exception as exc:
            return {"ok": False, "error": f"写回前校验失败（未写入，配置未被改动）：{exc}"}

        with open(config_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

        note = "已写入 config.toml，宿主配置监视会自动热重载（几秒内生效）"
        if clamped:
            note += "；以下项超出范围已夹紧：" + "、".join(clamped)
        return {"ok": True, "applied": applied, "note": note}

    @staticmethod
    def _format_toml_value(value: Any, ftype: str) -> str:
        if ftype == "boolean":
            return "true" if value else "false"
        if ftype == "number":
            if float(value) == int(float(value)):
                return str(int(float(value))) if abs(float(value)) < 1e15 else repr(float(value))
            return repr(float(value))
        import json as _json

        return _json.dumps(str(value), ensure_ascii=False)

    @staticmethod
    def _set_toml_key(text: str, section: str, key: str, formatted: str) -> str:
        """在 [section] 段内替换 key 行；找不到键就插到段头之后（只处理单行标量键）。"""
        import re as _re

        lines = text.splitlines()
        in_section = False
        section_found = False
        key_re = _re.compile(rf"({_re.escape(key)})\s*=")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("["):
                if in_section:
                    break  # 进入了下一个段，本段结束
                in_section = stripped == f"[{section}]"
                section_found = section_found or in_section
                continue
            if in_section and key_re.match(stripped):
                indent = line[: len(line) - len(line.lstrip())]
                lines[i] = indent + f"{key} = {formatted}"
                return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        if not section_found:
            return text  # 段都不存在：config 由宿主合并默认值，不动文件
        # 段存在但键缺失：插到段头后
        for i, line in enumerate(lines):
            if line.strip() == f"[{section}]":
                lines.insert(i + 1, f"{key} = {formatted}")
                return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
        return text

    @API("webui_mapping_set", description="WebUI：设置/清除某个道具的身体映射", version="1")
    async def webui_mapping_set(
        self,
        item_id: int = 0,
        body_food: str = "",
        body_kind: str = "",
        body_grams: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """保存单行映射。body_food 留空 = 解除映射。全部参数为标量（custom 页动作）。"""
        del kwargs
        item_id = int(item_id or 0)
        row = await self.db.fetchone(
            "SELECT item_id FROM shop_items WHERE item_id = ?", (item_id,)
        )
        if row is None:
            return {"ok": False, "error": f"道具不存在（item_id={item_id}）"}

        word = str(body_food or "").strip()
        kind = str(body_kind or "").strip().lower() or "snack"
        if kind not in _BODY_KINDS:
            return {"ok": False, "error": "类型只支持 meal / snack / drink"}
        try:
            grams = max(0.0, min(float(body_grams or 0.0), 3000.0))
        except (TypeError, ValueError):
            grams = 0.0

        valid = None
        labels: list[str] = []
        if word:
            await self._body_link.foods()
            keys = self._body_link.resolve_words(word)
            valid = keys is not None
            if keys and self._body_link._foods:
                labels = [self._body_link._foods["labels"].get(k, k) for k in keys]

        await self.db.set_item_body_mapping(item_id, word, kind, grams)
        return {
            "ok": True,
            "item_id": item_id,
            "body_food": word,
            "body_kind": kind,
            "body_grams": grams,
            "valid": valid,
            "labels": labels,
        }

    @API("webui_item_save", description="WebUI：保存道具完整信息（名称/价格/emoji/描述/饱食度 + 身体映射）", version="1")
    async def webui_item_save(
        self,
        item_id: int = 0,
        item_name: str = "",
        price: int = 0,
        satiety_bonus: float = 0.0,
        emoji: str = "",
        description: str = "",
        body_food: str = "",
        body_kind: str = "",
        body_grams: float = 0.0,
        style_mode: str = "",
        style_prompt: str = "",
        style_duration_minutes: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """整行保存：道具完整信息 + 身体映射 + 投喂风格一次写入（改名带同名查重）。"""
        del kwargs
        item_id = int(item_id or 0)
        row = await self.db.fetchone(
            "SELECT name, scope, group_id, style_prompt, style_duration_minutes FROM shop_items WHERE item_id = ?",
            (item_id,),
        )
        if row is None:
            return {"ok": False, "error": f"道具不存在（item_id={item_id}）"}
        old_name, scope, group_id = str(row[0]), str(row[1]), str(row[2])
        old_style_prompt = str(row[3] or "")
        old_style_dur = int(row[4] or 0)

        name = str(item_name or "").strip() or old_name
        if len(name) > 40:
            return {"ok": False, "error": "道具名过长（≤40 字）"}
        if name != old_name:
            dup = await self.db.fetchone(
                "SELECT 1 FROM shop_items WHERE name = ? AND scope = ? AND group_id = ? AND is_on_sale = 1 AND item_id != ?",
                (name, scope, group_id, item_id),
            )
            if dup:
                return {"ok": False, "error": f"同作用域已有同名在售道具「{name}」"}

        try:
            price = int(price)
        except (TypeError, ValueError):
            return {"ok": False, "error": "价格必须是整数"}
        if price < 0 or price > 1_000_000:
            return {"ok": False, "error": "价格需在 0 ~ 1,000,000 之间"}
        try:
            sat = max(-1000.0, min(float(satiety_bonus), 1000.0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "饱食度必须是数字"}
        emoji = str(emoji or "").strip()[:8]
        desc = strip_outer_quotes(str(description or ""))[:100]
        word = str(body_food or "").strip()
        kind = str(body_kind or "").strip().lower() or "snack"
        if kind not in _BODY_KINDS:
            return {"ok": False, "error": "类型只支持 meal / snack / drink"}
        try:
            grams = max(0.0, min(float(body_grams or 0.0), 3000.0))
        except (TypeError, ValueError):
            grams = 0.0

        valid = None
        labels: list[str] = []
        if word:
            await self._body_link.foods()
            keys = self._body_link.resolve_words(word)
            valid = keys is not None
            if keys and self._body_link._foods:
                labels = [self._body_link._foods["labels"].get(k, k) for k in keys]

        new_style_prompt, new_style_dur, style_err = self._parse_style_fields(
            style_mode, style_prompt, style_duration_minutes, old_style_prompt, old_style_dur
        )
        if style_err:
            return {"ok": False, "error": style_err}

        await self.db.execute_commit(
            """
            UPDATE shop_items
            SET name = ?, price = ?, satiety_bonus = ?, emoji = ?, description = ?,
                body_food = ?, body_kind = ?, body_grams = ?,
                style_prompt = ?, style_duration_minutes = ?
            WHERE item_id = ?
            """,
            (name, price, sat, emoji, desc, word, kind, grams, new_style_prompt, new_style_dur, item_id),
        )
        return {
            "ok": True,
            "item_id": item_id,
            "name": name,
            "body_food": word,
            "body_kind": kind,
            "body_grams": grams,
            "valid": valid,
            "labels": labels,
        }

    @API("webui_item_toggle_sale", description="WebUI：上架/下架道具", version="1")
    async def webui_item_toggle_sale(
        self, item_id: int = 0, on_sale: int = 1, **kwargs: Any
    ) -> dict[str, Any]:
        """切换在售状态（1 上架 / 0 下架，软下架——数据保留）。"""
        del kwargs
        item_id = int(item_id or 0)
        on_sale = 1 if int(on_sale or 0) == 1 else 0
        row = await self.db.fetchone(
            "SELECT item_id FROM shop_items WHERE item_id = ?", (item_id,)
        )
        if row is None:
            return {"ok": False, "error": f"道具不存在（item_id={item_id}）"}
        await self.db.execute_commit(
            "UPDATE shop_items SET is_on_sale = ? WHERE item_id = ?",
            (on_sale, item_id),
        )
        return {"ok": True, "item_id": item_id, "is_on_sale": on_sale}

    @staticmethod
    def _parse_style_fields(
        style_mode: str,
        style_prompt: str,
        style_duration_minutes: int,
        fallback_prompt: str = "",
        fallback_dur: int = 0,
    ) -> tuple[str, int, str]:
        """统一解析并规范化道具的投喂风格参数。"""
        mode = str(style_mode or "").strip().lower()
        if mode == "none":
            return "", 0, ""
        if mode == "clear":
            return STYLE_CLEAR_ALL_MARKER, 0, ""
        if mode == "style":
            prompt = strip_outer_quotes(str(style_prompt or ""))[:500]
            if not prompt:
                return "", 0, "注入风格文案不能为空"
            try:
                dur = max(0, min(int(style_duration_minutes or 0), 525600))
            except (TypeError, ValueError):
                return "", 0, "风格持续时长必须是整数（分钟）"
            return prompt, dur, ""
        return fallback_prompt, fallback_dur, ""

    @API("webui_item_create", description="WebUI：一键新建联动道具（上架+映射一步完成，可指定群）", version="1")
    async def webui_item_create(
        self,
        item_name: str = "",
        price: int = 5,
        emoji: str = "",
        description: str = "",
        satiety_bonus: float = 5.0,
        body_food: str = "",
        body_kind: str = "",
        body_grams: float = 0.0,
        style_mode: str = "",
        style_prompt: str = "",
        style_duration_minutes: int = 0,
        scope: str = "global",
        group_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """新建道具并（可选）同时写入投喂风格与身体映射。scope='global' 或 'group'（需 group_id）。"""
        del kwargs
        name = str(item_name or "").strip()
        if not name:
            return {"ok": False, "error": "道具名不能为空"}
        if len(name) > 40:
            return {"ok": False, "error": "道具名过长（≤40 字）"}
        try:
            price = int(price)
        except (TypeError, ValueError):
            return {"ok": False, "error": "价格必须是整数"}
        if price < 0 or price > 1_000_000:
            return {"ok": False, "error": "价格需在 0 ~ 1,000,000 之间"}
        try:
            sat = max(-1000.0, min(float(satiety_bonus), 1000.0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "饱食度必须是数字"}
        emoji = str(emoji or "").strip()[:8]
        desc = strip_outer_quotes(str(description or ""))[:100]
        word = str(body_food or "").strip()
        kind = str(body_kind or "").strip().lower() or "snack"
        if kind not in _BODY_KINDS:
            return {"ok": False, "error": "类型只支持 meal / snack / drink"}
        try:
            grams = max(0.0, min(float(body_grams or 0.0), 3000.0))
        except (TypeError, ValueError):
            grams = 0.0

        new_style_prompt, new_style_dur, style_err = self._parse_style_fields(
            style_mode, style_prompt, style_duration_minutes
        )
        if style_err:
            return {"ok": False, "error": style_err}

        scope = str(scope or "global").strip()
        gid = str(group_id or "").strip()
        if scope == "group":
            if not gid:
                return {"ok": False, "error": "群商店必须提供 group_id"}
            enabled = await self.db.get_enabled_group_ids()
            if gid not in enabled:
                return {"ok": False, "error": f"群 {gid} 不在投喂白名单里"}
        else:
            scope = "global"
            gid = ""

        dup = await self.db.fetchone(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = ? AND group_id = ? AND is_on_sale = 1",
            (name, scope, gid),
        )
        if dup:
            where = "全局" if scope == "global" else f"群{gid}"
            return {"ok": False, "error": f"{where}道具「{name}」已存在"}

        await self.db.execute_commit(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                    is_on_sale, satiety_bonus, style_prompt, style_duration_minutes,
                                    created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'webui', ?)
            """,
            (name, emoji, desc, price, scope, gid, sat, new_style_prompt, new_style_dur, time.time()),
        )
        id_row = await self.db.fetchone(
            "SELECT item_id FROM shop_items WHERE name = ? AND scope = ? AND group_id = ? AND is_on_sale = 1",
            (name, scope, gid),
        )
        item_id = int(id_row[0]) if id_row else 0

        valid = None
        labels: list[str] = []
        if word:
            await self._body_link.foods()
            keys = self._body_link.resolve_words(word)
            valid = keys is not None
            if keys and self._body_link._foods:
                labels = [self._body_link._foods["labels"].get(k, k) for k in keys]
            await self.db.set_item_body_mapping(int(item_id), word, kind, grams)

        return {
            "ok": True,
            "item_id": int(item_id),
            "name": name,
            "scope": scope,
            "group_id": gid,
            "valid": valid,
            "labels": labels,
        }

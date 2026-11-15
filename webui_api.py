"""投喂插件 — WebUI「身体联动管理页」API。

全部 public=False（webui.json 只能绑定本插件 API，不要求 public）。
页面：webui/mapping.html（custom 页，沙箱 iframe + window.maibot 桥）。
"""

from __future__ import annotations

import time
from typing import Any

from maibot_sdk import API

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
                   scope, group_id, body_food, body_kind, body_grams
            FROM shop_items
            WHERE is_on_sale = 1
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
                    "valid": valid,
                }
            )

        body_state = None
        if self._body_link.body_enabled():
            try:
                body_state = await self._body_link.get_state()
            except Exception:
                body_state = None

        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "linked_count": linked,
            "invalid_count": invalid,
            "body_enabled": self._body_link.body_enabled(),
            "body_state": body_state,
            "foods": {
                "labels": foods.get("labels", {}),
                "aliases": foods.get("aliases", {}),
                "base_grams": foods.get("base_grams", {}),
                "volumes": foods.get("volumes", {}),
            },
        }

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

    @API("webui_item_create", description="WebUI：一键新建全局联动道具（上架+映射一步完成）", version="1")
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
        **kwargs: Any,
    ) -> dict[str, Any]:
        """新建全局道具并（可选）同时写入身体映射。"""
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
        desc = str(description or "").strip()[:100]
        word = str(body_food or "").strip()
        kind = str(body_kind or "").strip().lower() or "snack"
        if kind not in _BODY_KINDS:
            return {"ok": False, "error": "类型只支持 meal / snack / drink"}
        try:
            grams = max(0.0, min(float(body_grams or 0.0), 3000.0))
        except (TypeError, ValueError):
            grams = 0.0

        dup = await self.db.fetchone(
            "SELECT 1 FROM shop_items WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (name,),
        )
        if dup:
            return {"ok": False, "error": f"全局道具「{name}」已存在"}

        await self.db.execute_commit(
            """
            INSERT INTO shop_items (name, emoji, description, price, scope, group_id,
                                    is_on_sale, satiety_bonus, created_by, created_at)
            VALUES (?, ?, ?, ?, 'global', '', 1, ?, 'webui', ?)
            """,
            (name, emoji, desc, price, sat, time.time()),
        )
        id_row = await self.db.fetchone(
            "SELECT item_id FROM shop_items WHERE name = ? AND scope = 'global' AND is_on_sale = 1",
            (name,),
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
            "valid": valid,
            "labels": labels,
        }

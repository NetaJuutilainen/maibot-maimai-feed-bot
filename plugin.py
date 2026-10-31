"""投喂插件 — 签到积分+商店道具+投喂Bot+定时求投喂

支持全局/群内两层商店、管理员指令和黑白名单。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder

from .commands_admin import AdminCommandsMixin
from .commands_user import UserCommandsMixin
from .config import FeedBotConfig, STYLE_CLEAR_ALL_MARKER
from .db import AsyncDatabase
from .loops import LoopTasksMixin
from .utils import LEGACY_DB_PATH, PLUGIN_DIR


class FeedBotPlugin(
    MaiBotPlugin, UserCommandsMixin, AdminCommandsMixin, LoopTasksMixin
):
    """投喂插件：签到积分+商店道具+投喂Bot+定时求投喂。"""

    config_model = FeedBotConfig

    def __init__(self) -> None:
        super().__init__()
        self.db = AsyncDatabase()
        self._running: bool = False
        self._decay_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._seek_feed_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # 投喂临时风格：group_id → [effect, ...]；仅内存态，重启即失效
        # 每个 effect：{"name": str, "emoji": str, "prompt": str, "expire_ts": float}
        self._active_styles: dict[str, list[dict[str, Any]]] = {}
        # stream_id / session_id → group_id，供回复注入按群判定
        self._stream_to_group: dict[str, str] = {}
        # group_id → stream_id，供失效提示按群发送
        self._group_to_stream: dict[str, str] = {}
        self._style_expiry_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # 已启用群号集合：白名单模式下等于 config.toml 的 group_admins，热重载时重同步
        self._enabled_groups: set[str] = set()

    # ---- 生命周期 ----

    async def _sync_group_scope(self) -> None:
        """按配置同步群启用状态并刷新 _enabled_groups。

        filter.group_admins_whitelist = True（默认）时，config.toml 的
        [[filter.group_admins]] 就是唯一白名单：列表内的群开启，列表外的群关闭，
        从列表里删掉某群后热重载即关闭该群。
        关闭该开关则退回旧行为：配置只初始化新群，运行时授权以数据库为准。
        """
        whitelist_mode = True
        try:
            whitelist_mode = bool(self.config.filter.group_admins_whitelist)
        except Exception:
            whitelist_mode = True

        if whitelist_mode:
            try:
                stats = await self.db.sync_group_whitelist_from_config(self.config)
                if stats.get("disabled"):
                    self.ctx.logger.info(
                        "投喂插件按群隔离：关闭未列入白名单的群 %s",
                        ", ".join(stats["disabled"]),
                    )
                if stats.get("enabled"):
                    self.ctx.logger.info(
                        "投喂插件按群隔离：开启白名单群 %s",
                        ", ".join(stats["enabled"]),
                    )
            except Exception as exc:
                self.ctx.logger.error(f"投喂插件同步群白名单失败：{exc}")
        else:
            await self.db.initialize_group_admins_from_config(self.config)

        self._enabled_groups = await self.db.get_enabled_group_ids()
        # 白名单变化后，未启用群的生效风格一并丢弃，避免残留注入
        for gid in list(self._active_styles.keys()):
            if gid not in self._enabled_groups:
                self._active_styles.pop(gid, None)
                self._group_to_stream.pop(gid, None)
        self.ctx.logger.info(
            "投喂插件当前启用群（%s）：%s",
            "白名单" if whitelist_mode else "数据库",
            ", ".join(sorted(self._enabled_groups)) or "（无）",
        )

    # ---- 数据目录 ----

    def _resolve_data_dir(self) -> str:
        """解析宿主为本插件分配的持久数据目录（ctx.paths.data_dir）。

        数据按宿主规范存放在 data/plugins/<plugin_id>/（跨重启保留），
        不再写入插件目录自身。宿主过旧没有 ctx.paths 时回退插件目录内
        data/（1.4.x 旧行为）并打 warning。
        """
        paths = getattr(self.ctx, "paths", None)
        data_dir = getattr(paths, "data_dir", None)
        if data_dir:
            data_dir = str(data_dir)
        else:
            data_dir = os.path.join(PLUGIN_DIR, "data")
            self.ctx.logger.warning(
                "宿主未提供 ctx.paths.data_dir，数据目录回退到插件目录内 %s（建议升级宿主）",
                data_dir,
            )
        os.makedirs(data_dir, exist_ok=True)
        return data_dir

    def _migrate_legacy_database(self, data_dir: str) -> str:
        """把旧版写在插件目录 data/ 下的库搬迁到新数据目录（一次性）。

        仅当新位置没有库、旧位置有库时执行；-wal/-shm 一起复制保证一致性，
        旧文件保留不删（回滚到旧版插件时仍可读）。返回最终 db 路径。
        """
        db_path = os.path.join(data_dir, "feed_bot.db")
        if os.path.exists(db_path) or not os.path.exists(LEGACY_DB_PATH):
            return db_path
        for suffix in ("", "-wal", "-shm"):
            src = LEGACY_DB_PATH + suffix
            if os.path.exists(src):
                shutil.copy2(src, db_path + suffix)
        self.ctx.logger.info(
            "投喂插件数据目录已规范化：旧库 %s -> %s（旧文件保留作回滚保险）",
            LEGACY_DB_PATH,
            db_path,
        )
        return db_path

    async def on_load(self) -> None:
        """插件加载时初始化数据目录和数据库。"""
        data_dir = self._resolve_data_dir()
        db_path = self._migrate_legacy_database(data_dir)

        # 配置在 on_load 时才可用，此时传给 db
        self.db.config = self.config
        await asyncio.to_thread(self.db.open, db_path)

        # 按配置同步群启用状态（白名单模式下既加也减）
        await self._sync_group_scope()

        # 预热 stream_id → group_id 映射，供回复注入按群判定
        await self._refresh_stream_group_map()

        self._running = True
        self._decay_task = asyncio.create_task(self._attr_decay_loop())
        self._seek_feed_task = asyncio.create_task(self._seek_feed_loop())
        self._style_expiry_task = asyncio.create_task(self._style_expiry_loop())

        self.ctx.logger.info("投喂插件已加载")

    async def _cancel_tasks(self) -> None:
        """取消并等待后台任务结束。"""
        tasks = []
        if self._decay_task:
            self._decay_task.cancel()
            tasks.append(self._decay_task)
            self._decay_task = None
        if self._seek_feed_task:
            self._seek_feed_task.cancel()
            tasks.append(self._seek_feed_task)
            self._seek_feed_task = None
        if self._style_expiry_task:
            self._style_expiry_task.cancel()
            tasks.append(self._style_expiry_task)
            self._style_expiry_task = None
        # 等待任务实际结束，避免关闭数据库时仍在操作
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def on_unload(self) -> None:
        """插件卸载时关闭数据库连接和后台任务。"""
        self._running = False
        await self._cancel_tasks()
        await asyncio.to_thread(self.db.close)
        self.ctx.logger.info("投喂插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        """配置热重载时重启后台任务并按群同步启用状态。"""
        if scope != "self":
            return

        # 白名单模式下配置即唯一依据：新增群开启、移除群关闭
        await self._sync_group_scope()

        self.ctx.logger.info(f"投喂插件配置已更新 (v{version})，重启后台任务")

        await self._cancel_tasks()

        if self._running and self.db.is_open:
            self._decay_task = asyncio.create_task(self._attr_decay_loop())
            self._seek_feed_task = asyncio.create_task(self._seek_feed_loop())
            self._style_expiry_task = asyncio.create_task(self._style_expiry_loop())

    # ------------------------------------------------------------------
    # 投喂临时风格注入（吃下特定道具后暂时改变说话风格）
    # ------------------------------------------------------------------

    def _register_style_effect(
        self,
        group_id: str,
        stream_id: str,
        item_name: str,
        item_emoji: str,
        style_prompt: str,
        style_duration_minutes: int,
        quantity: int = 1,
    ) -> str:
        """登记某个群的临时说话风格（可多效果并存，时长按投喂数量叠加）。

        返回状态：""=无效果（未配置提示词）；"added"=新登记；"refreshed"=同名续期（时长叠加）；
        "full"=已达上限、本次未生效；"cleared"=清除型道具清空全部。仅存内存，bot 重启后失效。
        """
        prompt = str(style_prompt or "").strip()
        if not prompt or not group_id:
            return ""
        if stream_id:
            sid = str(stream_id).strip()
            self._stream_to_group[sid] = group_id
            self._group_to_stream[group_id] = sid

        # 清除型道具：吃下后清空该群全部生效风格（不产生新效果、不受上限约束）
        if prompt == STYLE_CLEAR_ALL_MARKER:
            removed = self._active_styles.pop(group_id, None)
            cleared = len(removed) if removed else 0
            self.ctx.logger.info(
                "投喂清除型道具生效：群 %s，清除 %s 个风格", group_id, cleared
            )
            return "cleared"

        try:
            duration_minutes = max(int(style_duration_minutes), 0)
        except (TypeError, ValueError):
            duration_minutes = 0
        try:
            qty = max(int(quantity), 1)
        except (TypeError, ValueError):
            qty = 1

        now = time.time()
        # 本次投喂带来的时长：单份时长 × 投喂数量（数量>1 时叠加）
        add_seconds = duration_minutes * 60 * qty

        effects = self._active_styles.setdefault(group_id, [])
        name_key = str(item_name or "").strip()

        # 同名道具：在剩余时间上继续累加（时长叠加），并覆盖提示词
        for ef in effects:
            if ef["name"] == name_key:
                ef["prompt"] = prompt
                ef["emoji"] = str(item_emoji or "")
                ef["expire_ts"] = self._stack_expiry(ef["expire_ts"], add_seconds, now)
                self.ctx.logger.info(
                    "投喂临时风格叠加：群 %s，道具 %s，本次 +%s",
                    group_id,
                    name_key,
                    f"{duration_minutes * qty} 分钟" if add_seconds > 0 else "永久",
                )
                return "refreshed"

        # 不同道具：检查上限
        try:
            max_n = max(int(self.config.style.max_active_effects), 1)
        except Exception:
            max_n = 3
        if len(effects) >= max_n:
            self.ctx.logger.info("投喂临时风格已达上限：群 %s（上限 %s）", group_id, max_n)
            return "full"

        effects.append(
            {
                "name": name_key,
                "emoji": str(item_emoji or ""),
                "prompt": prompt,
                "expire_ts": now + add_seconds if add_seconds > 0 else float("inf"),
            }
        )
        self.ctx.logger.info(
            "投喂临时风格生效：群 %s，道具 %s，时长 %s",
            group_id,
            name_key,
            f"{duration_minutes * qty} 分钟" if add_seconds > 0 else "永久",
        )
        return "added"

    @staticmethod
    def _stack_expiry(current_expire_ts: float, add_seconds: float, now: float) -> float:
        """把新增时长叠加到已有到期时间上。

        - 新增时长 <= 0（永久道具）→ 永久；
        - 原效果是永久 → 从当前时间重新计时；
        - 原效果仍生效 → 在剩余时间上继续累加；
        - 原效果已过期 → 从当前时间重新计时。
        """
        if add_seconds <= 0:
            return float("inf")
        if current_expire_ts == float("inf"):
            return now + add_seconds
        base_ts = current_expire_ts if current_expire_ts > now else now
        return base_ts + add_seconds

    def _active_effects_for_group(self, group_id: str) -> list[dict[str, Any]]:
        """返回指定群当前生效的效果列表（已剔除过期项）。"""
        effects = self._active_styles.get(group_id)
        if not effects:
            return []
        now = time.time()
        active = [e for e in effects if now < e["expire_ts"]]
        if len(active) != len(effects):
            if active:
                self._active_styles[group_id] = active
            else:
                self._active_styles.pop(group_id, None)
        return active

    def _active_style_for_stream(self, session_id: str) -> list[str]:
        """按回复流（session_id）解析当前群的全部生效风格提示词；过期项静默剔除。"""
        gid = self._stream_to_group.get(str(session_id or "").strip(), "")
        if not gid:
            return []
        # 未启用（未列入白名单）的群不注入任何投喂效果
        if not self._is_group_enabled(gid):
            return []
        effects = self._active_styles.get(gid)
        if not effects:
            return []
        now = time.time()
        active = [e for e in effects if now < e["expire_ts"]]
        if not active:
            self._active_styles.pop(gid, None)
            return []
        if len(active) != len(effects):
            self._active_styles[gid] = active
        return [e["prompt"] for e in active]

    async def _refresh_stream_group_map(self) -> None:
        """从宿主拉取全部群聊流，构建 stream_id → group_id 双向映射。"""
        try:
            streams = await self.ctx.chat.get_group_streams()
            for entry in streams or []:
                if not isinstance(entry, dict):
                    continue
                sid = str(entry.get("session_id") or "").strip()
                gid = str(entry.get("group_id") or "").strip()
                if sid and gid:
                    self._stream_to_group[sid] = gid
                    self._group_to_stream[gid] = sid
        except Exception as exc:
            self.ctx.logger.debug(f"预热 stream→群 映射失败：{exc}")

    async def _style_expiry_loop(self) -> None:
        """定期检查临时风格到期情况，到期时按群发送失效提示。"""
        while self._running:
            try:
                await asyncio.sleep(5)
                if not self._running:
                    break
                now = time.time()
                for group_id, effects in list(self._active_styles.items()):
                    still_active = [e for e in effects if now < e["expire_ts"]]
                    expired = [e for e in effects if now >= e["expire_ts"]]
                    for ef in expired:
                        await self._notify_expiry(group_id, ef)
                        self.ctx.logger.info("投喂临时风格到期：群 %s，道具 %s", group_id, ef["name"])
                    if still_active:
                        self._active_styles[group_id] = still_active
                    else:
                        self._active_styles.pop(group_id, None)
                        self._group_to_stream.pop(group_id, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"风格到期检查异常: {e}")
                await asyncio.sleep(30)

    async def _notify_expiry(self, group_id: str, effect: dict[str, Any]) -> None:
        """发送单个风格的失效提示（受 expire_notice 配置控制）。"""
        try:
            if not self.config.style.expire_notice:
                return
        except Exception:
            return
        stream_id = self._group_to_stream.get(group_id, "")
        if not stream_id:
            return
        emoji = str(effect.get("emoji") or "")
        name = str(effect.get("name") or "")
        display = f"{emoji}{name}" if emoji else name
        try:
            await self.ctx.send.text(f"「{display}」效果已失效", stream_id)
        except Exception as e:
            self.ctx.logger.debug(f"发送失效提示失败: {e}")

    @HookHandler(
        "maisaka.replyer.before_request",
        name="feed_style_replyer_inject",
        description="投喂临时风格：把生效中的说话风格提示词协作式追加到 replyer extra_prompt",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
    )
    async def on_replyer_before_request(
        self,
        hook_name: str = "",
        extra_prompt: str = "",
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """replyer extra_prompt 通道：追加投喂临时说话风格。"""
        prompts = self._active_style_for_stream(session_id)
        if not prompts:
            return {"action": "continue"}
        if len(prompts) == 1:
            block = f"【投喂效果·临时说话风格（生效中）】\n{prompts[0]}"
        else:
            block = "【投喂效果·临时说话风格（生效中，多个效果叠加）】\n" + "\n\n".join(prompts)
        merged = f"{extra_prompt.strip()}\n\n{block}".strip()
        self.ctx.logger.debug(f"投喂临时风格注入（session={session_id or '未知'}）")
        return {
            "action": "continue",
            "modified_kwargs": {
                **kwargs,
                "hook_name": hook_name,
                "session_id": session_id,
                "extra_prompt": merged,
            },
        }


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------


def create_plugin() -> FeedBotPlugin:
    """创建投喂插件实例。"""
    return FeedBotPlugin()

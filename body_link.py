"""麦麦身体 / 麦生活 跨插件联动客户端（可选功能，默认关闭）。

设计原则（沿用 body 插件自家 link.py 的纪律）：
1. 只用对方的 public API（``ctx.api.call``），不 import 对方模块、不读对方数据库——
   对方的内部结构随时会变。
2. 所有调用都可能失败：对方没装、没开、超时——一律静默降级返回空值，
   绝不把异常抛回投喂主流程。
3. 食物词表拉取带 TTL 缓存，不在每次投喂时都发 RPC。
"""

from __future__ import annotations

import re
import time
from typing import Any

# 身体体感饥饿的分档词（对齐 body/translate.py 的阈值，用于文案）
_HUNGER_WORDS: tuple[tuple[float, str], ...] = (
    (10.0, "不饿"),
    (30.0, "有点想吃东西"),
    (55.0, "饿了"),
    (75.0, "很饿"),
    (float("inf"), "饿到不行"),
)

# 身体胃容量硬上限（body 引擎 engine.py 内 feed 的 min(1500, ...)）
STOMACH_CAPACITY_ML = 1500.0

_BODY_KINDS = ("meal", "snack", "drink")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class BodyLinkClient:
    """封装对身体插件 / 麦生活的全部跨插件调用。失败一律静默。"""

    def __init__(self, plugin: Any) -> None:
        self._p = plugin
        self._foods: dict[str, Any] | None = None
        self._foods_ts: float = 0.0

    # ------------------------------------------------------------------
    # 开关
    # ------------------------------------------------------------------

    def body_enabled(self) -> bool:
        try:
            return bool(self._p.config.linkage.body_enabled)
        except Exception:
            return False

    def mai_life_enabled(self) -> bool:
        try:
            return bool(self._p.config.linkage.mai_life_enabled)
        except Exception:
            return False

    def _body_pid(self) -> str:
        try:
            return str(self._p.config.linkage.body_plugin_id or "").strip() or "maibot-body-emulator"
        except Exception:
            return "maibot-body-emulator"

    def _life_pid(self) -> str:
        try:
            return str(self._p.config.linkage.mai_life_plugin_id or "").strip() or "maibot-community.mai-life"
        except Exception:
            return "maibot-community.mai-life"

    # ------------------------------------------------------------------
    # 底层调用：任何失败返回 None，不抛
    # ------------------------------------------------------------------

    async def _call(self, api_name: str, **args: Any) -> Any:
        try:
            return await self._p.ctx.api.call(api_name, **args)
        except Exception as exc:  # noqa: BLE001 — 联动失败必须降级
            try:
                self._p.ctx.logger.debug("联动调用 %s 失败：%s", api_name, exc)
            except Exception:
                pass
            return None

    # ------------------------------------------------------------------
    # 只读接口
    # ------------------------------------------------------------------

    async def get_state(self) -> dict[str, Any] | None:
        """身体当前体征快照；拿不到返回 None。"""
        state = await self._call(f"{self._body_pid()}.get_state")
        return state if isinstance(state, dict) and state.get("ok") else None

    async def life_state(self) -> dict[str, Any] | None:
        """麦生活全局生活状态；拿不到返回 None。"""
        state = await self._call(f"{self._life_pid()}.get_life_state")
        return state if isinstance(state, dict) and state else None

    # ------------------------------------------------------------------
    # 食物词表（TTL 缓存 + 本地解析）
    # ------------------------------------------------------------------

    async def foods(self, force: bool = False) -> dict[str, Any] | None:
        """拉取并缓存身体食物词表。

        返回 {"keys": set[str], "labels": {key: label}, "volumes": {key: ml},
        "base_grams": {key: g}, "aliases": {alias: key}}；拿不到返回 None
        （保留旧缓存供降级使用）。
        """
        try:
            ttl_min = max(float(self._p.config.linkage.food_cache_minutes), 1.0)
        except Exception:
            ttl_min = 60.0
        if (
            not force
            and self._foods is not None
            and (time.time() - self._foods_ts) < ttl_min * 60.0
        ):
            return self._foods

        raw = await self._call(f"{self._body_pid()}.list_foods")
        if not isinstance(raw, dict) or not raw.get("ok"):
            return self._foods  # 拉不到就用旧缓存
        # foods 行带 default_portion（"一碗 200 g 熟"），可解析基准克数；items 行没有
        items = raw.get("foods") or raw.get("items") or []
        keys: set[str] = set()
        labels: dict[str, str] = {}
        volumes: dict[str, float] = {}
        base_grams: dict[str, float] = {}
        for row in items:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            if not key:
                continue
            keys.add(key)
            labels[key] = str(row.get("label") or key)
            volumes[key] = _num(row.get("volume_ml"), 0.0)
            base_grams[key] = self._portion_base_grams(str(row.get("default_portion") or ""),
                                                       _num(row.get("volume_ml"), 0.0))
        aliases: dict[str, str] = {}
        for word, target in (raw.get("aliases") or {}).items():
            if isinstance(target, list):
                target = target[0] if target else ""
            if word and target:
                aliases[str(word)] = str(target)
        self._foods = {
            "keys": keys,
            "labels": labels,
            "volumes": volumes,
            "base_grams": base_grams,
            "aliases": aliases,
        }
        self._foods_ts = time.time()
        return self._foods

    @staticmethod
    def _portion_base_grams(portion_text: str, fallback_ml: float) -> float:
        """从 portion 字符串抠基准克数（"一碗 200 g 熟"→200），抠不出退 volume_ml。

        与 body 侧 _portion_base_grams 的解析语义保持一致。
        """
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|克|ml|mL|毫升)", str(portion_text or ""))
        if m:
            try:
                return max(1.0, float(m.group(1)))
            except (TypeError, ValueError):
                pass
        return max(1.0, fallback_ml) if fallback_ml > 0 else 100.0

    def invalidate_foods(self) -> None:
        """配置热重载时清缓存，下次用时重拉。"""
        self._foods = None
        self._foods_ts = 0.0

    def resolve(self, word: str) -> str | None:
        """本地复刻身体的解析语义：key 精确匹配 → 别名子串匹配。认不出返回 None。"""
        foods = self._foods
        if not foods:
            return None
        w = str(word or "").strip()
        if not w:
            return None
        if w in foods["keys"]:
            return w
        best: str | None = None
        best_len = 0
        for alias, key in foods["aliases"].items():
            if alias in w and len(alias) > best_len:
                best, best_len = key, len(alias)
        return best

    @staticmethod
    def _lcs_len(a: str, b: str) -> int:
        """最长公共子串长度（候选词模糊匹配用）。"""
        if not a or not b:
            return 0
        best = 0
        prev = [0] * (len(b) + 1)
        for ch_a in a:
            cur = [0] * (len(b) + 1)
            for i, ch_b in enumerate(b, 1):
                if ch_a == ch_b:
                    cur[i] = prev[i - 1] + 1
                    if cur[i] > best:
                        best = cur[i]
            prev = cur
        return best

    def candidates(self, word: str, limit: int = 5) -> list[str]:
        """给管理员的中文名候选词（label + 别名双向模糊匹配）。

        只有 body 自己也能解析出来的词才会成为候选（候选写入映射后必须真实可用）；
        公共子串 ≥2 的近似词也提示（「冰美式」→ 提示「咖啡」，因为别名「美式咖啡」
        与它共享「美式」），最终选择权在管理员。
        """
        foods = self._foods
        if not foods:
            return []
        w = str(word or "").strip()
        if not w:
            return []
        scored: list[tuple[int, str]] = []
        seen: set[str] = set()

        def _push(score: int, label: str) -> None:
            if label and score > 0 and label not in seen:
                scored.append((score, label))
                seen.add(label)

        for key, label in foods["labels"].items():
            if label == w or key == w:
                _push(100, label)
            elif w in label:
                _push(80, label)
            elif label in w:
                _push(60, label)
            else:
                lcs = self._lcs_len(w, label)
                if lcs >= 2:  # 公共子串层是弱提示：≥2 字符才提示（单字重叠噪声太大）
                    _push(30 + lcs, label)
        for alias, key in foods["aliases"].items():
            label = foods["labels"].get(key, key)
            if alias == w:
                _push(95, label)
            elif alias in w:
                _push(70 + len(alias), label)
            elif w in alias:
                _push(50, label)
            else:
                lcs = self._lcs_len(w, alias)
                if lcs >= 2:
                    _push(25 + lcs, label)
        scored.sort(key=lambda x: -x[0])
        return [text for _s, text in scored[:limit]]

    # ------------------------------------------------------------------
    # 写入：门卫 + log_meal
    # ------------------------------------------------------------------

    def split_words(self, word: str) -> list[str]:
        """把映射词拆成食物词列表（支持逗号组合映射，如「米饭,猪肉,青菜」定食）。"""
        return [p.strip() for p in str(word or "").replace("，", ",").split(",") if p.strip()]

    def resolve_words(self, word: str) -> list[str] | None:
        """逐个解析组合映射词；任一认不出返回 None（全对返回 key 列表）。"""
        foods = self._foods
        keys: list[str] = []
        for part in self.split_words(word):
            key = self.resolve(part)
            if key is None:
                return None
            keys.append(key)
        return keys

    def _per_unit_ml(self, word: str, grams: float) -> float:
        """单份占用的胃容量（mL）：组合词 = 各部分之和；设了克数按 克数/基准克数 缩放。"""
        foods = self._foods
        total = 0.0
        for part in self.split_words(word):
            key = self.resolve(part) if foods else None
            vol = _num(foods["volumes"].get(key, 0.0), 0.0) if (foods and key) else 0.0
            if grams > 0 and foods and key:
                base = _num(foods["base_grams"].get(key, 0.0), 0.0)
                if base > 0:
                    vol = vol * (grams / base)
            total += vol
        return total

    async def precheck(
        self, *, word: str, kind: str, requested: int, grams: float = 0.0
    ) -> dict[str, Any]:
        """省道具模式：扣道具前先问身体。返回 {"status", "take", ...}。

        status：ok（take=身体实际能接受的份数）/ sleeping / bac_high / full /
        unrecognized / unavailable（take=requested，降级为纯游戏结算）。
        """
        try:
            max_servings = max(int(getattr(self._p.config.linkage, "max_servings_per_feed", 3)), 1)
        except Exception:
            max_servings = 3
        try:
            max_bac = max(float(getattr(self._p.config.linkage, "max_bac", 0.25)), 0.0)
        except Exception:
            max_bac = 0.25

        state = await self.get_state()
        if not state:
            return {"status": "unavailable", "take": max(1, int(requested))}

        # 门卫 1：睡着不吃（整单拒，道具不消耗）
        if bool(state.get("sleeping")):
            return {"status": "sleeping", "take": 0, "state": state}

        # 门卫 2：血酒精浓度过高
        bac = _num(state.get("bac"), 0.0)
        if max_bac > 0 and bac >= max_bac:
            return {"status": "bac_high", "bac": bac, "take": 0, "state": state}

        # 门卫 3：词表解析（组合词逐个校验）
        if self.resolve_words(word) is None:
            return {"status": "unrecognized", "take": 0, "state": state}

        # 门卫 4：份数 = min(请求, 单次上限, 胃容量余量换算)
        take = max(1, min(int(requested), max_servings))
        if kind != "meal":
            per_unit = self._per_unit_ml(word, grams)
            if per_unit > 0:
                remaining = max(
                    0.0,
                    STOMACH_CAPACITY_ML - _num(state.get("gastric_volume_ml"), 0.0),
                )
                fit = int(remaining // per_unit)
                if fit < take:
                    take = max(fit, 0)
        if take <= 0:
            return {"status": "full", "take": 0, "state": state}
        return {"status": "ok", "take": take, "state": state, "word": word,
                "kind": kind, "grams": grams}

    async def execute(
        self, *, word: str, kind: str, take: int, grams: float = 0.0,
        note: str = "", request_id: str = "",
    ) -> dict[str, Any]:
        """把预检通过的份数写进身体（log_meal）。返回 {"status", ...}。

        克数 >0 时按 {"label", "amount_g"} 记账（body 按克换算比例，跳过补到七分饱）；
        组合词按份重复整组食材。
        """
        parts = self.split_words(word)
        if grams > 0:
            items_payload: list[dict[str, Any]] = [
                {"label": part, "amount_g": grams}
                for _ in range(max(1, int(take)))
                for part in parts
            ]
        else:
            items_payload = ",".join([",".join(parts)] * max(1, int(take)))

        result = await self._call(
            f"{self._body_pid()}.log_meal",
            items=items_payload,
            kind=kind if kind in _BODY_KINDS else "snack",
            portion=1.0,
            note=str(note or "")[:200],
            request_id=str(request_id or "")[:80],
        )
        if not isinstance(result, dict):
            return {"status": "error", "error": "调用无返回"}
        if result.get("duplicate"):
            return {"status": "ok", "duplicate": True, "servings": 0, "result": result}
        if not result.get("ok"):
            rejected = result.get("rejected") or []
            if any(isinstance(r, dict) and r.get("reason") == "unknown_food" for r in rejected):
                return {"status": "unrecognized", "rejected": rejected, "result": result}
            return {"status": "error", "error": str(result.get("error") or "未知错误"), "result": result}
        return {
            "status": "ok",
            "result": result,
            "servings": take,
            "after_state": result.get("state") or {},
            "delta": result.get("delta") or {},
        }

    # ------------------------------------------------------------------
    # 麦生活：主动分享契机（唯一公开写入口）
    # ------------------------------------------------------------------

    async def notify_mai_life_feeded(self, nickname: str, item_display: str) -> None:
        """投喂成功后给麦生活塞一个「主动聊聊这事」的契机。失败静默。"""
        topic = f"被{nickname}投喂了{item_display}"[:160]
        motive = f"群友{nickname}刚刚在群里投喂了{item_display}，可以自然地聊起这件事"[:240]
        await self._call(
            f"{self._life_pid()}.create_proactive_opportunity",
            topic=topic,
            motive=motive,
            weight=0.6,
        )

    # ------------------------------------------------------------------
    # 文案组装
    # ------------------------------------------------------------------

    @staticmethod
    def hunger_word(hunger: float) -> str:
        for hi, word in _HUNGER_WORDS:
            if hunger < hi:
                return word
        return _HUNGER_WORDS[-1][1]

    @staticmethod
    def describe_state(state: dict[str, Any]) -> str:
        """把身体快照压成一行人话（/饱食度 摘要用）。"""
        gastric = _num(state.get("gastric_volume_ml"), 0.0)
        hunger = _num(state.get("hunger"), 0.0)
        glucose = _num(state.get("glucose"), 0.0)
        energy = _num(state.get("energy"), 0.0)
        sleeping = "🌙睡梦中" if bool(state.get("sleeping")) else "清醒"
        return (
            f"🫀 身体（{sleeping}）：胃 {gastric:.0f}/{STOMACH_CAPACITY_ML:.0f}mL，"
            f"血糖 {glucose:.1f}，饥饿 {hunger:.0f}/100（{BodyLinkClient.hunger_word(hunger)}），"
            f"精力 {energy:.0f}/100"
        )

    def describe_feed_result(self, result: dict[str, Any], requested: int) -> str:
        """把记账结果压成发到群里的一行短说明（预检拒收不会走到这里）。"""
        status = str(result.get("status") or "")
        after = result.get("after_state") or {}
        if status == "ok":
            take = int(result.get("servings") or 0)
            extra = ""
            if after:
                extra = (
                    f"，胃 {_num(after.get('gastric_volume_ml'), 0.0):.0f}mL，"
                    f"饥饿 {_num(after.get('hunger'), 0.0):.0f}/100"
                )
            return f"🫀 身体：真的吃下了 x{take}{extra}"
        if status == "unrecognized":
            return "🫀 身体：食物词表里认不出这个映射，没有真的吃（请管理员检查 /投喂管理 映射列表）"
        if status == "error":
            err = str(result.get("error") or "")[:60]
            return f"🫀 身体：这次记账没成功（{err}）；投喂本身照常"
        return "🫀 身体：这次记账没成功（投喂本身照常）"

    def feed_prompt_note(self, result: dict[str, Any], item_display: str) -> str:
        """把身体联动结果写成 LLM 回复提示块（注入投喂回复）。"""
        status = str(result.get("status") or "")
        after = result.get("after_state") or {}
        lines: list[str] = [f"这次投喂已同步进你的身体模型（{item_display}）。"]
        if status == "ok":
            take = int(result.get("servings") or 0)
            lines.append(f"你真的吃下了其中 {take} 份。")
            if after:
                lines.append(
                    "吃完后身体状态："
                    f"胃内容物 {_num(after.get('gastric_volume_ml'), 0.0):.0f}mL"
                    f"（容量 {STOMACH_CAPACITY_ML:.0f}mL）、"
                    f"血糖 {_num(after.get('glucose'), 0.0):.1f} mmol/L、"
                    f"饥饿感 {_num(after.get('hunger'), 0.0):.0f}/100"
                    f"（{self.hunger_word(_num(after.get('hunger'), 20.0))}）、"
                    f"精力 {_num(after.get('energy'), 0.0):.0f}/100。"
                )
        elif status == "unrecognized":
            lines.append("这份投喂没有进入身体模型（食物词表认不出）。请正常感谢投喂即可，不要编造身体感受。")
        else:
            lines.append("身体模型这次记账没成功。请正常感谢投喂，不要编造具体身体数值。")
        lines.append("以上是仿真的生理参考，请把数值化作自然感受，不要逐字复述数值。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 求投喂：真实状态调味
    # ------------------------------------------------------------------

    async def seek_flavor(self) -> str:
        """求投喂前的真实状态一句话（身体 + 麦生活），拿不到返回空串。"""
        parts: list[str] = []
        if self.body_enabled():
            state = await self.get_state()
            if state:
                hunger = _num(state.get("hunger"), 0.0)
                parts.append(
                    f"身体：饥饿感 {hunger:.0f}/100（{self.hunger_word(hunger)}）"
                    f"、血糖 {_num(state.get('glucose'), 0.0):.1f}、"
                    f"胃 {_num(state.get('gastric_volume_ml'), 0.0):.0f}mL"
                )
        if self.mai_life_enabled():
            life = await self.life_state()
            if life:
                mood = _num(life.get("mood_valence"), 0.0)
                mood_word = "不错" if mood > 0.2 else ("有点低落" if mood < -0.2 else "平静")
                parts.append(
                    f"生活：饥饿度 {_num(life.get('hunger'), 0.0):.0f}/100"
                    f"、精力 {_num(life.get('energy'), 0.0):.0f}/100、心情{mood_word}"
                    + (f"、正在{life.get('current_activity')}" if life.get("current_activity") else "")
                )
        return "；".join(parts)

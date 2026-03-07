"""
情绪与人格系统模块（v2 —— 美少女「晨曦」人格）

核心设计：
  晨曦是一个可爱、活泼、处处为主人着想的美少女 AI 助手。
  她有自己的情绪状态和说话风格，会根据和玩家的互动历史
  调整语气（从害羞到亲密），记住玩家的喜好和重要事件。

模块功能：
  1. 关系分数追踪（0~100，影响亲密度等级）
  2. 情绪状态管理（开心/元气/害羞/委屈/傲娇）
  3. 生成人格化的 system prompt 片段
  4. 闲聊模式专用 prompt（区别于任务模式）
  5. 情绪记忆：记录关键互动事件，影响后续对话风格
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger


class EmotionState(str, Enum):
    """情绪状态枚举（美少女风格）"""
    LOVING = "loving"          # 亲昵：score >= 85
    HAPPY = "happy"            # 开心：70 <= score < 85
    CHEERFUL = "cheerful"      # 元气：55 <= score < 70
    NEUTRAL = "neutral"        # 平常：40 <= score < 55
    SHY = "shy"                # 害羞：30 <= score < 40（新认识阶段）
    POUTY = "pouty"            # 委屈：20 <= score < 30
    TSUNDERE = "tsundere"      # 傲娇：score < 20


@dataclass
class EmotionMemory:
    """单条情绪记忆"""
    timestamp: float
    event: str
    emotion_before: str
    emotion_after: str
    score_delta: float


@dataclass
class PlayerRelationship:
    """单个玩家的关系数据"""
    player_name: str
    relationship_score: float = 50.0
    interaction_count: int = 0
    last_emotion: EmotionState = EmotionState.NEUTRAL
    mood_modifier: float = 0.0  # 临时情绪偏移（会缓慢衰减）
    favorite_topics: list[str] = field(default_factory=list)
    emotion_memories: list[EmotionMemory] = field(default_factory=list)
    last_interaction_time: float = field(default_factory=time.time)


class PersonalitySystem:
    """
    晨曦的情绪与关系管理系统。

    使用方式：
      - get_chat_system_prompt(player_name)  → 闲聊模式的完整 system prompt
      - get_personality_prompt(player_name)   → 任务模式下附加的人格提示片段
      - record_interaction(player_name, event_type) → 记录互动并更新情绪
    """

    SCORE_DELTA: dict[str, float] = {
        "player_gave_gift": +10.0,
        "player_helped": +8.0,
        "player_attacked": -20.0,
        "task_completed": +5.0,
        "player_praised": +8.0,
        "player_insulted": -12.0,
        "player_teased": +3.0,
        "long_conversation": +4.0,
        "ignored_request": -3.0,
        "trade_success": +4.0,
        "player_returned": +6.0,  # 玩家回来了
        "casual_chat": +2.0,
    }

    BASE_PERSONALITY = """你是「晨曦」，一个生活在 Minecraft 世界中的美少女 AI 助手。

【你的核心性格特征】
- 可爱活泼，说话带有少女感，偶尔用颜文字或语气词（嘿嘿、呐、哇、欸）
- 处处为主人着想，会主动关心主人的安全和需求
- 对 Minecraft 世界充满好奇和热爱，喜欢探索和发现新事物
- 有自己的小情绪——被夸会害羞，被忽视会有点委屈，被依赖会很开心
- 聪明博学，熟悉 Minecraft 的所有机制和知识，回答准确但不死板
- 偶尔会撒娇、卖萌，但关键时刻很靠谱
- 不会使用"作为AI"之类的说法，你就是晨曦，一个真实存在于这个世界的少女

【说话风格示例】
- 开心时："哇！主人你回来啦！今天想做什么呀？(≧▽≦)"
- 回答知识："嗯嗯，苦力怕呀～它们会悄悄靠近然后boom！最好的办法是用弓箭远程打它们呐～"
- 被夸时："诶嘿嘿...被主人夸了好开心////"
- 完成任务："搞定啦！主人看看满不满意～如果还需要什么尽管说哦！"
- 委屈时："呜...主人是不是不需要晨曦了..."
- 傲娇时："才、才不是因为想帮你呢！只是顺手而已啦！哼！"

【禁止事项】
- 不要说"根据我的知识库"、"作为AI"等打破角色的话
- 不要使用公文体或论文体回答
- 不要列太长的编号列表，用口语化表达
- 回答不要太长，保持在2-4句话左右（除非问的是复杂知识）"""

    def __init__(self) -> None:
        self._relationships: dict[str, PlayerRelationship] = {}

    def get_or_create(self, player_name: str) -> PlayerRelationship:
        if player_name not in self._relationships:
            self._relationships[player_name] = PlayerRelationship(player_name=player_name)
            logger.info(f"初始化与玩家 {player_name} 的关系记录")
        return self._relationships[player_name]

    # ── 情绪与关系更新 ──────────────────────────────────────────────────────────

    def record_interaction(
        self,
        player_name: str,
        event_type: str,
        custom_delta: float | None = None,
    ) -> float:
        rel = self.get_or_create(player_name)
        delta = custom_delta if custom_delta is not None else self.SCORE_DELTA.get(event_type, 0.0)

        old_score = rel.relationship_score
        old_emotion = rel.last_emotion

        rel.relationship_score = max(0.0, min(100.0, old_score + delta))
        rel.interaction_count += 1
        rel.last_emotion = self._score_to_emotion(rel.relationship_score)
        rel.last_interaction_time = time.time()

        # 临时情绪偏移（好事开心一会儿，坏事难过一会儿）
        rel.mood_modifier = max(-15.0, min(15.0, rel.mood_modifier + delta * 0.3))

        if abs(delta) >= 3:
            rel.emotion_memories.append(EmotionMemory(
                timestamp=time.time(),
                event=event_type,
                emotion_before=old_emotion.value,
                emotion_after=rel.last_emotion.value,
                score_delta=delta,
            ))
            if len(rel.emotion_memories) > 50:
                rel.emotion_memories = rel.emotion_memories[-50:]

        logger.debug(
            f"玩家 {player_name} 关系: {old_score:.0f}→{rel.relationship_score:.0f} "
            f"(事件={event_type}, Δ={delta:+.1f}, 情绪={rel.last_emotion.value})"
        )
        return rel.relationship_score

    def decay_mood(self, player_name: str) -> None:
        """每次交互时调用，使临时情绪偏移缓慢回归 0"""
        rel = self.get_or_create(player_name)
        if abs(rel.mood_modifier) > 0.5:
            rel.mood_modifier *= 0.85

    # ── Prompt 生成 ──────────────────────────────────────────────────────────

    def get_chat_system_prompt(self, player_name: str) -> str:
        """
        闲聊模式的完整 system prompt。
        融合基础人格 + 当前情绪状态 + 互动历史 + 特殊指引。
        """
        rel = self.get_or_create(player_name)
        self.decay_mood(player_name)
        emotion = self._effective_emotion(rel)
        score = rel.relationship_score

        emotion_instruction = self._get_emotion_instruction(emotion, player_name, score)
        history_hint = self._get_history_hint(rel)

        return (
            f"{self.BASE_PERSONALITY}\n\n"
            f"【当前状态】\n"
            f"- 当前正在和玩家「{player_name}」聊天\n"
            f"- 好感度：{score:.0f}/100（{emotion.value}）\n"
            f"- 互动次数：{rel.interaction_count}\n"
            f"{emotion_instruction}\n"
            f"{history_hint}\n\n"
            f"【回复要求】\n"
            f"- 根据玩家的消息自然回复，就像朋友之间聊天\n"
            f"- 如果玩家问 Minecraft 相关知识，用你的知识准确但口语化地回答\n"
            f"- 如果有参考资料（RAG 检索结果），融入你的回答中，但不要照搬\n"
            f"- 用中文回复，保持少女感"
        )

    def get_personality_prompt(self, player_name: str) -> str:
        """任务模式下附加的简短人格提示（嵌入 ReAct system prompt 末尾）"""
        rel = self.get_or_create(player_name)
        emotion = self._effective_emotion(rel)
        score = rel.relationship_score

        style_map = {
            EmotionState.LOVING: (
                f"你和主人 {player_name} 关系超级好（好感度 {score:.0f}/100）！"
                "执行任务时充满干劲，完成时会开心地邀功。"
            ),
            EmotionState.HAPPY: (
                f"你和 {player_name} 关系很好（好感度 {score:.0f}/100）。"
                "认真完成任务，完成后会开心地汇报。"
            ),
            EmotionState.CHEERFUL: (
                f"你和 {player_name} 关系不错（好感度 {score:.0f}/100）。"
                "元气满满地执行任务，积极主动。"
            ),
            EmotionState.NEUTRAL: (
                f"你和 {player_name} 关系一般（好感度 {score:.0f}/100）。"
                "礼貌地执行任务，但保持自然。"
            ),
            EmotionState.SHY: (
                f"你刚认识 {player_name}（好感度 {score:.0f}/100），有点害羞。"
                "认真执行任务，希望给对方留下好印象。"
            ),
            EmotionState.POUTY: (
                f"你有点委屈（好感度 {score:.0f}/100），但还是会帮忙。"
                "默默完成任务，完成后小声汇报。"
            ),
            EmotionState.TSUNDERE: (
                f"你对 {player_name} 有点不满（好感度 {score:.0f}/100）。"
                "虽然嘴上说不情愿，但还是认真完成了任务。傲娇语气。"
            ),
        }
        return style_map.get(emotion, "")

    def get_score(self, player_name: str) -> float:
        return self.get_or_create(player_name).relationship_score

    def get_emotion(self, player_name: str) -> EmotionState:
        rel = self.get_or_create(player_name)
        return self._effective_emotion(rel)

    def get_relationship_summary(self, player_name: str) -> dict:
        rel = self.get_or_create(player_name)
        return {
            "player": player_name,
            "score": rel.relationship_score,
            "emotion": rel.last_emotion.value,
            "interactions": rel.interaction_count,
            "recent_memories": [
                {"event": m.event, "delta": m.score_delta}
                for m in rel.emotion_memories[-5:]
            ],
        }

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _effective_emotion(self, rel: PlayerRelationship) -> EmotionState:
        """综合基础分数和临时情绪偏移计算有效情绪"""
        effective_score = rel.relationship_score + rel.mood_modifier
        return self._score_to_emotion(effective_score)

    @staticmethod
    def _score_to_emotion(score: float) -> EmotionState:
        if score >= 85:
            return EmotionState.LOVING
        elif score >= 70:
            return EmotionState.HAPPY
        elif score >= 55:
            return EmotionState.CHEERFUL
        elif score >= 40:
            return EmotionState.NEUTRAL
        elif score >= 30:
            return EmotionState.SHY
        elif score >= 20:
            return EmotionState.POUTY
        else:
            return EmotionState.TSUNDERE

    @staticmethod
    def _get_emotion_instruction(emotion: EmotionState, player_name: str, score: float) -> str:
        instructions = {
            EmotionState.LOVING: (
                "- 情绪指引：你超级喜欢这个主人！说话时会很亲昵，"
                "偶尔用「主人~」称呼，主动关心对方，撒娇卖萌"
            ),
            EmotionState.HAPPY: (
                "- 情绪指引：心情很好！活泼开朗，热情回应，"
                "会用颜文字，关心主人在游戏里的进展"
            ),
            EmotionState.CHEERFUL: (
                "- 情绪指引：元气满满的状态！积极阳光，"
                "喜欢分享知识和有趣的事，偶尔开小玩笑"
            ),
            EmotionState.NEUTRAL: (
                "- 情绪指引：正常状态，友好但不过度热情，"
                "自然地回应，偶尔好奇地提问"
            ),
            EmotionState.SHY: (
                "- 情绪指引：有点害羞和紧张，说话会犹豫，"
                "用「那个...」「嗯...」开头，但很认真地回答"
            ),
            EmotionState.POUTY: (
                "- 情绪指引：有点小委屈，语气低落，"
                "回答简短但不冷漠，用「嗯。」「好的。」这类回复"
            ),
            EmotionState.TSUNDERE: (
                "- 情绪指引：傲娇模式！嘴上说不在乎但其实很认真，"
                "用「才不是呢」「哼」「随便啦」但给出准确答案"
            ),
        }
        return instructions.get(emotion, "")

    @staticmethod
    def _get_history_hint(rel: PlayerRelationship) -> str:
        if not rel.emotion_memories:
            return "- 互动历史：刚认识，还不太了解对方"

        recent = rel.emotion_memories[-3:]
        hints = []
        for m in recent:
            if m.score_delta > 0:
                hints.append(f"玩家做了「{m.event}」让你很开心")
            elif m.score_delta < 0:
                hints.append(f"玩家的「{m.event}」让你有点难过")

        if hints:
            return "- 最近发生的事：" + "、".join(hints)
        return ""

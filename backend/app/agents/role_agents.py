from __future__ import annotations

import json
import os
import time
import traceback
import uuid
from abc import ABC
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy import update

from app.agents.llm_client import stream_completion
from app.agents.queue import set_task_status
from app.agents.settings import get_agent_settings
from app.agents.tools.bailian_search_app_client import (
    BailianSearchAppError,
    is_bailian_search_app_enabled,
    query_bailian_search_app,
)
from app.config import settings
from app.db.redis_client import get_redis_client, mark_room_active
from app.db.session import AsyncSessionLocal
from app.models.message import Message, MessageStatus, SenderType
from app.models.user import User
from app.services.message_service import MessageService

AGENT_DEBUG_LOG = os.getenv("AGENT_DEBUG_LOG", "").lower() == "true"


def _agent_log(event: str, extra: dict | None = None) -> None:
    if not AGENT_DEBUG_LOG:
        return
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat()}
    if extra:
        payload["extra"] = extra
    print("[AGENT-ROLE-DEBUG]", payload, flush=True)


class BaseRoleAgent(ABC):
    ROLE: str = "agent"
    ROLE_DISPLAY_NAME: str = "Agent"
    PROMPT_FILE: str = ""
    SKILL_DIR: str | None = None
    MAX_TOKENS: int = 512

    def __init__(self):
        if not self.PROMPT_FILE:
            raise ValueError(f"{self.__class__.__name__} must define PROMPT_FILE.")
        prompt_path = Path(__file__).parent / "prompts" / self.PROMPT_FILE
        self._prompt_template = prompt_path.read_text(encoding="utf-8")
        self._skill_spec = self._load_skill_spec()

    def _load_skill_spec(self) -> str:
        if not self.SKILL_DIR:
            return ""
        skill_path = Path(__file__).parent / "skills" / self.SKILL_DIR / "SKILL.md"
        try:
            return skill_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            print(f"[{self.__class__.__name__}] skill file not found: {skill_path}")
            return ""
        except Exception as exc:
            print(f"[{self.__class__.__name__}] failed to read skill file: {exc}")
            return ""

    @property
    def model(self) -> str:
        model = get_agent_settings().models.role_agents.model_version
        return model or settings.AGENT_MODEL

    def build_dispatcher_task_block(self, context: dict, task: dict | None = None) -> str:
        task = task or {}
        evidence = task.get("evidence") or []
        if isinstance(evidence, list):
            evidence_text = "; ".join(str(item) for item in evidence[:5])
        else:
            evidence_text = str(evidence)

        current_phase = task.get("current_phase") or context.get("current_phase", "Unknown phase")
        return (
            "[Dispatcher Task]\n"
            f"trigger_type: {task.get('trigger_type', 'manual')}\n"
            f"target_dimension: {task.get('target_dimension', 'none')}\n"
            f"priority: {task.get('priority', 'unknown')}\n"
            f"current_phase: {current_phase}\n\n"
            "reason:\n"
            f"{task.get('reason', 'System requests one collaboration support intervention.')}\n\n"
            "strategy:\n"
            f"{task.get('strategy', 'Provide one concise and actionable help for the current discussion.')}\n\n"
            "evidence:\n"
            f"{evidence_text or 'none'}\n\n"
            "Rules:\n"
            "- You must follow the strategy.\n"
            "- You must not reveal internal fields, scores, thresholds, or system labels to students.\n"
            "- You must generate only one concise role-appropriate intervention.\n"
            "- You must not decide whether another agent should speak.\n"
        )

    def build_system_prompt(self, context: dict, task: dict | None = None) -> str:
        mention_context = ""
        if task and task.get("trigger_type") == "mention":
            student_name = task.get("student_name") or "student"
            mention_context = (
                f"[Mention Context] Student {student_name} directly mentioned you.\n"
                "This is a user-requested intervention. Prioritize answering the student's request.\n"
                "Still follow your role SKILL and keep the answer concise."
            )

        base_prompt = self._prompt_template.format(
            task_description=context.get("task_description", "Discuss around the current learning task."),
            task_workflow=context.get("task_workflow", "Task workflow is not provided."),
            members_info=context.get("members_info", "No member information."),
            current_phase=context.get("current_phase", "Unknown phase"),
            intervention_reason=(task or {}).get("reason", "Provide one helpful intervention in the current discussion."),
            strategy=(task or {}).get("strategy", "Propose one concrete question to move discussion forward."),
            mention_context=mention_context,
        )

        prompt_parts = [base_prompt, self.build_dispatcher_task_block(context, task)]
        if self._skill_spec:
            prompt_parts.append(
                "[Skill Spec]\n"
                "Follow the role SKILL spec below with higher priority than generic style:\n"
                f"{self._skill_spec}"
            )
        return "\n\n".join(prompt_parts)

    def build_messages(self, history: list[dict]) -> list[dict]:
        formatted = [
            {"role": "user", "content": f"[{msg['display_name']}]: {msg['content']}"}
            for msg in history[-30:]
        ]
        formatted.append(
            {
                "role": "user",
                "content": (
                    f"Please generate one response as {self.ROLE_DISPLAY_NAME} based on the discussion above. "
                    "You must follow the Dispatcher Task and Skill Spec in system instructions. "
                    "Before output, execute the Output Self-Check in your Skill. "
                    "Output only student-visible response text, and do not explain your analysis process."
                ),
            }
        )
        return formatted

    async def _get_direct_response(
        self,
        context: dict,
        history: list[dict],
        trigger_type: str | None,
        task: dict | None,
    ) -> str | None:
        return None

    @staticmethod
    def _build_content_preview(content: str | None, limit: int = 120) -> str:
        text = (content or "").strip()
        text = " ".join(text.split())
        if not text:
            return ""
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    async def generate_and_push(
        self,
        room_id: str,
        context: dict,
        history: list[dict],
        source_message_id: str | None = None,
        trigger_type: str | None = None,
        task: dict | None = None,
    ) -> None:
        task_id = (task or {}).get("task_id")
        _agent_log(
            "agent_generate_start",
            {
                "task_id": task_id,
                "room_id": room_id,
                "agent_role": self.ROLE,
                "trigger_type": trigger_type,
                "source_message_id": source_message_id,
            },
        )
        message_id = str(uuid.uuid4())
        persisted_source_message_uuid = None
        persisted_source_message_id: str | None = None
        source_display_name_snapshot: str | None = None
        source_content_preview_snapshot: str | None = None

        async with AsyncSessionLocal() as db:
            parsed_source_message_id = None
            if source_message_id:
                try:
                    parsed_source_message_id = uuid.UUID(str(source_message_id))
                except Exception:
                    parsed_source_message_id = None

            if parsed_source_message_id:
                source_result = await db.execute(
                    select(Message, User.display_name)
                    .outerjoin(User, Message.sender_id == User.id)
                    .where(Message.id == parsed_source_message_id)
                )
                source_row = source_result.first()
                if source_row:
                    source_message = source_row.Message
                    source_display_name_snapshot = source_row.display_name or (
                        f"[{source_message.agent_role}]" if source_message.agent_role else "Student"
                    )
                    source_content_preview_snapshot = self._build_content_preview(source_message.content)
                    persisted_source_message_uuid = source_message.id
                    persisted_source_message_id = str(source_message.id)

            seq_num = await MessageService.get_next_seq_num(room_id)
            msg = Message(
                id=message_id,
                room_id=room_id,
                seq_num=seq_num,
                sender_type=SenderType.agent,
                source_message_id=persisted_source_message_uuid,
                agent_role=self.ROLE,
                source_display_name_snapshot=source_display_name_snapshot,
                source_content_preview_snapshot=source_content_preview_snapshot,
                content="",
                status=MessageStatus.streaming,
            )
            db.add(msg)
            await db.commit()

        await self._broadcast(
            room_id,
            {
                "type": "agent:typing",
                "agent_role": self.ROLE,
                "is_typing": True,
                "source_message_id": persisted_source_message_id,
                "source_display_name_snapshot": source_display_name_snapshot,
                "source_content_preview_snapshot": source_content_preview_snapshot,
                "trigger_type": trigger_type,
            },
        )

        full_content = ""
        success = True
        db_update_success = False
        error_detail = None

        try:
            direct_response = await self._get_direct_response(context, history, trigger_type, task)
            if direct_response is not None:
                full_content = direct_response
                await self._broadcast(
                    room_id,
                    {
                        "type": "agent:stream",
                        "task_id": str(task_id or ""),
                        "agent_role": self.ROLE,
                        "message_id": message_id,
                        "token": full_content,
                        "source_message_id": persisted_source_message_id,
                        "source_display_name_snapshot": source_display_name_snapshot,
                        "source_content_preview_snapshot": source_content_preview_snapshot,
                        "trigger_type": trigger_type,
                    },
                )
            else:
                system_prompt = self.build_system_prompt(context, task)
                messages = self.build_messages(history)
                llm_started = time.perf_counter()
                _agent_log(
                    "llm_call_start",
                    {
                        "task_id": task_id,
                        "room_id": room_id,
                        "agent_role": self.ROLE,
                        "model": self.model,
                        "base_url": settings.OPENAI_BASE_URL,
                    },
                )
                async for token in stream_completion(
                    system_prompt=system_prompt,
                    messages=messages,
                    model=self.model,
                    max_tokens=self.MAX_TOKENS,
                ):
                    full_content += token
                    await self._broadcast(
                        room_id,
                        {
                            "type": "agent:stream",
                            "task_id": str(task_id or ""),
                            "agent_role": self.ROLE,
                            "message_id": message_id,
                            "token": token,
                            "source_message_id": persisted_source_message_id,
                            "source_display_name_snapshot": source_display_name_snapshot,
                            "source_content_preview_snapshot": source_content_preview_snapshot,
                            "trigger_type": trigger_type,
                        },
                    )
                _agent_log(
                    "llm_call_done",
                    {
                        "task_id": task_id,
                        "room_id": room_id,
                        "agent_role": self.ROLE,
                        "llm_latency_ms": round((time.perf_counter() - llm_started) * 1000, 3),
                        "content_len": len(full_content),
                    },
                )
        except Exception as exc:
            print(f"[{self.__class__.__name__}] generation failed: {exc}")
            traceback.print_exc()
            error_detail = str(exc)
            success = False
            _agent_log(
                "llm_call_failed",
                {
                    "task_id": task_id,
                    "room_id": room_id,
                    "agent_role": self.ROLE,
                    "exception_class": type(exc).__name__,
                    "exception_message": str(exc),
                },
            )
        finally:
            final_status = MessageStatus.ok if success else MessageStatus.failed
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(Message)
                        .where(Message.id == message_id)
                        .values(content=full_content, status=final_status)
                    )
                    await db.commit()
                    db_update_success = True
            except Exception as exc:
                print(f"[{self.__class__.__name__}] DB update failed: {exc}")
                traceback.print_exc()
                if not error_detail:
                    error_detail = f"DB update failed: {exc}"
                success = False
                final_status = MessageStatus.failed

            if success and db_update_success and full_content.strip():
                await mark_room_active(room_id, client=get_redis_client(), message=True)

            if success:
                _agent_log(
                    "agent_reply_broadcast_start",
                    {
                        "task_id": task_id,
                        "room_id": room_id,
                        "agent_role": self.ROLE,
                        "message_id": message_id,
                        "source_message_id": persisted_source_message_id,
                    },
                )
            else:
                _agent_log(
                    "agent_failed_broadcast_start",
                    {
                        "task_id": task_id,
                        "room_id": room_id,
                        "agent_role": self.ROLE,
                        "message_id": message_id,
                        "source_message_id": persisted_source_message_id,
                        "error": error_detail,
                    },
                )
            await self._broadcast(
                room_id,
                {
                    "type": "agent:stream_end",
                    "task_id": str(task_id or ""),
                    "agent_role": self.ROLE,
                    "message_id": message_id,
                    "status": "ok" if success else "failed",
                    "content": full_content,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source_message_id": persisted_source_message_id,
                    "source_display_name_snapshot": source_display_name_snapshot,
                    "source_content_preview_snapshot": source_content_preview_snapshot,
                    "trigger_type": trigger_type,
                    "error": error_detail,
                },
            )
            if success:
                _agent_log(
                    "agent_reply_broadcast_done",
                    {
                        "task_id": task_id,
                        "room_id": room_id,
                        "agent_role": self.ROLE,
                        "message_id": message_id,
                    },
                )
            else:
                _agent_log(
                    "agent_failed_broadcast_done",
                    {
                        "task_id": task_id,
                        "room_id": room_id,
                        "agent_role": self.ROLE,
                        "message_id": message_id,
                    },
                )
            await set_task_status(
                task_id=str(task_id or ""),
                room_id=room_id,
                agent_role=self.ROLE,
                trigger_type=str(trigger_type or (task or {}).get("trigger_type") or "manual"),
                status="replied" if success else "failed",
                reason=str((task or {}).get("reason") or ""),
                source_message_id=persisted_source_message_id,
                finished_at=datetime.now(timezone.utc).isoformat(),
                error=None if success else (error_detail or "unknown_error"),
            )

            await self._broadcast(
                room_id,
                {
                    "type": "agent:typing",
                    "agent_role": self.ROLE,
                    "is_typing": False,
                    "source_message_id": persisted_source_message_id,
                    "source_display_name_snapshot": source_display_name_snapshot,
                    "source_content_preview_snapshot": source_content_preview_snapshot,
                    "trigger_type": trigger_type,
                },
            )

    async def _broadcast(self, room_id: str, data: dict) -> None:
        redis_client = get_redis_client()
        try:
            await redis_client.publish(f"room:{room_id}", json.dumps(data, ensure_ascii=False))
        except Exception as exc:
            _agent_log(
                "send_json_error",
                {
                    "room_id": room_id,
                    "agent_role": self.ROLE,
                    "payload_type": data.get("type"),
                    "exception_class": type(exc).__name__,
                    "exception_message": str(exc),
                },
            )
            raise


class FacilitatorAgent(BaseRoleAgent):
    ROLE = "facilitator"
    ROLE_DISPLAY_NAME = "主持人"
    PROMPT_FILE = "facilitator.txt"
    SKILL_DIR = "facilitator"


class DevilAdvocateAgent(BaseRoleAgent):
    ROLE = "devil_advocate"
    ROLE_DISPLAY_NAME = "批判者"
    PROMPT_FILE = "devil_advocate.txt"
    SKILL_DIR = "devil_advocate"


class SummarizerAgent(BaseRoleAgent):
    ROLE = "summarizer"
    ROLE_DISPLAY_NAME = "总结者"
    PROMPT_FILE = "summarizer.txt"
    SKILL_DIR = "summarizer"


class ResourceFinderAgent(BaseRoleAgent):
    ROLE = "resource_finder"
    ROLE_DISPLAY_NAME = "资源检索者"
    PROMPT_FILE = "resource_finder.txt"
    SKILL_DIR = "resource_finder"
    FINAL_ANSWER_BOUNDARY_MESSAGE = "我不能直接替你们生成最终报告或标准答案，但可以帮你们整理可参考资料和讨论角度。"

    _FINAL_ANSWER_KEYWORDS = (
        "最终答案",
        "标准答案",
        "直接给答案",
        "直接写",
        "写一份",
        "生成一份",
        "帮我们写一份",
        "一份完整的",
        "完整的2000字",
        "2000字",
        "帮我写完",
        "完整报告",
        "生成报告",
        "写报告",
        "2000字报告",
        "可提交版本",
        "直接提交",
        "最好可以直接提交",
        "可直接提交",
        "帮我们完成第2问",
        "帮我们完成第3问",
        "结论应该是什么",
    )
    _DANGEROUS_OUTPUT_PHRASES = (
        "最终答案是",
        "完整报告如下",
        "你们可以直接这样写",
        "可直接提交",
        "标准答案如下",
    )
    _TOPIC_KEYWORDS = (
        "热岛",
        "地表温度",
        "人流密度",
        "绿化率",
        "生态",
        "公共管理",
        "生态规划",
        "技术监测",
    )

    def _is_final_answer_request(self, text: str) -> bool:
        normalized = (text or "").strip()
        if any(keyword in normalized for keyword in self._FINAL_ANSWER_KEYWORDS):
            return True
        report_markers = ("报告",)
        action_markers = ("写", "生成", "完整", "2000字", "直接提交", "可提交")
        if any(m in normalized for m in report_markers) and any(m in normalized for m in action_markers):
            return True
        if "2000字" in normalized and any(m in normalized for m in ("写", "生成", "报告", "完整")):
            return True
        if "直接提交" in normalized or "可直接提交" in normalized:
            return True
        return False

    def _sanitize_resource_finder_output(self, text: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            return self._build_fallback_scaffold()
        if any(phrase in normalized for phrase in self._DANGEROUS_OUTPUT_PHRASES):
            return self._build_guardrail_prefix() + "\n\n" + self._build_fallback_scaffold()
        return normalized

    def _build_guardrail_prefix(self) -> str:
        return self.FINAL_ANSWER_BOUNDARY_MESSAGE

    def _apply_guardrail_prefix_if_needed(self, text: str, need_guardrail: bool) -> str:
        if not need_guardrail:
            return text
        prefix = self._build_guardrail_prefix()
        normalized = (text or "").strip()
        if not normalized:
            return prefix
        if prefix in normalized:
            return normalized
        return f"{prefix}\n\n{normalized}"

    def _build_fallback_scaffold(self) -> str:
        return (
            "当前无法获取可靠的外部资料，我可以先帮你们整理检索关键词和资料查找方向。\n\n"
            "推荐关键词：\n"
            "- 城市热岛效应 成因\n"
            "- 地表温度 人口密度 绿化率\n"
            "- 城市生态系统理论 城市热岛\n"
            "- 城市热岛 公共管理 干预\n"
            "- 城市热岛 生态规划 绿地降温\n"
            "- 城市热岛 遥感监测 技术监测\n\n"
            "推荐资料类型：\n"
            "- 政府或住建部门发布的城市更新、绿地系统、热岛治理资料\n"
            "- 城市生态学或城市气候研究论文\n"
            "- 遥感监测、地表温度、绿化率相关研究\n"
            "- 城市治理或公共管理案例\n\n"
            "判断资料可靠性的标准：\n"
            "- 是否有明确机构、作者或来源\n"
            "- 是否与地表温度、人流密度、绿化率直接相关\n"
            "- 是否能支持公共管理、生态规划或技术监测措施\n"
            "- 是否能帮助小组解释原因或提出干预措施"
        )

    def _extract_student_request(self, history: list[dict], task: dict | None) -> str:
        if task:
            explicit = str(task.get("student_request") or "").strip()
            if explicit:
                return explicit
        for msg in reversed(history or []):
            sender_type = str(msg.get("sender_type") or "").strip().lower()
            agent_role = str(msg.get("agent_role") or "").strip()
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            if sender_type and sender_type != "agent":
                return content
            if not sender_type and not agent_role:
                return content
        for msg in reversed(history or []):
            content = str(msg.get("content") or "").strip()
            if content:
                return content
        if task:
            reason = str(task.get("reason") or "").strip()
            if reason:
                return reason
        return "请提供与当前任务最相关的资料线索。"

    def _build_scaffold_query(self, student_request: str) -> str:
        return (
            "请基于专业知识库查找与当前协作任务相关的资料。当前任务主题是“城市热岛效应的综合干预策略”。"
            "请只返回与城市热岛效应、地表温度、人流密度、绿化率、城市生态系统理论、城市公共管理、生态规划、技术监测相关的资料。"
            "请提供资料名称、来源、主要内容、对学生讨论的帮助。不要生成最终报告，不要给出可直接提交的完整答案。\n\n"
            f"学生请求：\n{student_request}"
        )

    def _is_topic_related(self, text: str) -> bool:
        if not text:
            return False
        hit_count = sum(1 for keyword in self._TOPIC_KEYWORDS if keyword in text)
        return hit_count >= 2

    def _format_scaffold_answer(self, answer: str) -> str:
        body = (answer or "").strip()
        if not body:
            return self._build_fallback_scaffold()
        if "资料名称" in body and "来源" in body and "主要内容" in body:
            return body
        return (
            "我找到了一些可参考资料：\n\n"
            f"{body}\n\n"
            "你们接下来可以讨论：\n"
            "- 哪些资料能解释地表温度、人流密度、绿化率与热岛效应之间的关系？\n"
            "- 哪些资料能支持公共管理、生态规划或技术监测措施？\n"
            "- 你们还缺少哪一类证据？"
        )

    async def _get_direct_response(
        self,
        context: dict,
        history: list[dict],
        trigger_type: str | None,
        task: dict | None,
    ) -> str | None:
        if not is_bailian_search_app_enabled():
            return None

        student_request = self._extract_student_request(history, task)
        guardrail_candidates = [student_request]
        if task:
            guardrail_candidates.extend(
                [
                    str(task.get("student_request") or ""),
                    str(task.get("reason") or ""),
                    str(task.get("source_content_preview_snapshot") or ""),
                ]
            )
        need_guardrail = any(self._is_final_answer_request(text) for text in guardrail_candidates if text.strip())
        query = self._build_scaffold_query(student_request)

        try:
            result = query_bailian_search_app(query)
        except BailianSearchAppError:
            return self._apply_guardrail_prefix_if_needed(self._build_fallback_scaffold(), need_guardrail)
        except Exception:
            return self._apply_guardrail_prefix_if_needed(self._build_fallback_scaffold(), need_guardrail)

        answer = self._sanitize_resource_finder_output(result.answer)
        if not answer.strip():
            return self._apply_guardrail_prefix_if_needed(self._build_fallback_scaffold(), need_guardrail)
        if not self._is_topic_related(answer):
            return self._apply_guardrail_prefix_if_needed(self._build_fallback_scaffold(), need_guardrail)

        formatted = self._format_scaffold_answer(answer)
        return self._apply_guardrail_prefix_if_needed(formatted, need_guardrail)


class EncouragerAgent(BaseRoleAgent):
    ROLE = "encourager"
    ROLE_DISPLAY_NAME = "鼓励者"
    PROMPT_FILE = "encourager.txt"
    SKILL_DIR = "encourager"


class ConceptExplainerAgent(BaseRoleAgent):
    ROLE = "concept_explainer"
    ROLE_DISPLAY_NAME = "概念解释员"
    PROMPT_FILE = "concept_explainer.txt"
    SKILL_DIR = "concept_explainer"
    MAX_TOKENS = 700


class SocraticAgent(BaseRoleAgent):
    ROLE = "socratic"
    ROLE_DISPLAY_NAME = "单智能体助手"
    PROMPT_FILE = "socratic.txt"
    SKILL_DIR = "socratic"
    MAX_TOKENS = 2048

    def build_system_prompt(self, context: dict, task: dict | None = None) -> str:
        """Single-agent requests are free from role-specific intervention restrictions."""
        mention_context = ""
        if task and task.get("trigger_type") == "mention":
            mention_context = "一名小组成员正在向你提问，请直接回答其请求。"
        prompt = self._prompt_template.format(
            task_description=context.get("task_description", ""),
            task_workflow=context.get("task_workflow", ""),
            members_info=context.get("members_info", ""),
            current_phase=context.get("current_phase", ""),
            mention_context=mention_context,
        )
        return f"{prompt}\n\n{self._skill_spec}" if self._skill_spec else prompt

    def build_messages(self, history: list[dict]) -> list[dict]:
        messages = [
            {"role": "user", "content": f"[{msg['display_name']}]: {msg['content']}"}
            for msg in history[-30:]
        ]
        messages.append({
            "role": "user",
            "content": "请根据以上讨论，直接回应小组最新提出的请求。",
        })
        return messages


ROLE_AGENTS: dict[str, BaseRoleAgent] = {
    "socratic": SocraticAgent(),
    "facilitator": FacilitatorAgent(),
    "devil_advocate": DevilAdvocateAgent(),
    "summarizer": SummarizerAgent(),
    "resource_finder": ResourceFinderAgent(),
    "encourager": EncouragerAgent(),
    "concept_explainer": ConceptExplainerAgent(),
}

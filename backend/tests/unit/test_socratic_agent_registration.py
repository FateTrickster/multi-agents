from pathlib import Path

from app.agents.agent_mode import can_use_agent_role
from app.agents.role_agents import ROLE_AGENTS, SocraticAgent


def test_role_agents_registers_socratic():
    assert "socratic" in ROLE_AGENTS
    assert isinstance(ROLE_AGENTS["socratic"], SocraticAgent)


def test_socratic_agent_metadata():
    agent = ROLE_AGENTS["socratic"]
    assert agent.ROLE == "socratic"
    assert agent.ROLE_DISPLAY_NAME == "单智能体助手"
    assert agent.PROMPT_FILE == "socratic.txt"
    assert agent.SKILL_DIR == "socratic"


def test_single_mode_allows_only_socratic():
    assert can_use_agent_role("single", "socratic") is True
    assert can_use_agent_role("single", "facilitator") is False
    assert can_use_agent_role("single", "devil_advocate") is False
    assert can_use_agent_role("single", "summarizer") is False
    assert can_use_agent_role("single", "resource_finder") is False
    assert can_use_agent_role("single", "encourager") is False
    assert can_use_agent_role("single", "concept_explainer") is False


def test_socratic_prompt_and_skill_files_exist():
    base = Path(__file__).resolve().parents[2] / "app" / "agents"
    assert (base / "prompts" / "socratic.txt").exists()
    assert (base / "skills" / "socratic" / "SKILL.md").exists()


def test_single_agent_prompt_has_no_socratic_only_constraints():
    agent = ROLE_AGENTS["socratic"]
    prompt = agent.build_system_prompt(
        context={
            "task_description": "协作任务",
            "task_workflow": "分析-比较-结论",
            "members_info": "A/B/C",
            "current_phase": "讨论阶段",
        },
        task={"reason": "学生主动提问", "strategy": "用问题推动思考", "trigger_type": "mention"},
    )
    assert "通用 AI 助手" in prompt
    assert "可以回答问题" in prompt
    assert "不强制使用苏格拉底式提问" in prompt
    assert "[Dispatcher Task]" not in prompt
    assert "不提供最终答案" not in prompt
    assert agent.MAX_TOKENS >= 2048


def test_single_agent_messages_without_role_self_check():
    agent = ROLE_AGENTS["socratic"]
    messages = agent.build_messages([{"display_name": "学生A", "content": "请直接解释概念并给出例子"}])
    assert "请直接解释概念并给出例子" in messages[0]["content"]
    assert "Output Self-Check" not in str(messages)
    assert "请根据以上讨论" in messages[-1]["content"]
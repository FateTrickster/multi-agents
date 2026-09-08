from __future__ import annotations


class SocialCPSAnalyst:
    CPS_CODES = ("A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2", "D3")

    def _code_message(self, message: dict) -> str | None:
        text = str(message.get("content") or "")
        if not text:
            return None
        if any(k in text for k in ["我来", "我负责", "我先做", "我去找"]):
            return "A1"
        if any(k in text for k in ["你负责", "你来做", "分工", "角色"]):
            return "A3"
        if any(k in text for k in ["先确认", "是否一致", "我们理解", "定义"]):
            return "B1"
        if any(k in text for k in ["补充", "基于你", "回应", "承接"]):
            return "B2"
        if any(k in text for k in ["规则", "流程", "协作方式"]):
            return "B3"
        if any(k in text for k in ["我现在", "正在", "去做", "先做"]):
            return "C1"
        if any(k in text for k in ["我们一起", "协作", "共同", "同步"]):
            return "C2"
        if any(k in text for k in ["检查", "核对", "验证", "确认结果"]):
            return "C3"
        if any(k in text for k in ["总结一下", "回顾", "目前状态"]):
            return "D1"
        if any(k in text for k in ["这个方案", "评估", "有没有问题", "还缺"]):
            return "D2"
        if any(k in text for k in ["调整分工", "重新分配", "换人", "改计划"]):
            return "D3"
        return None

    async def analyze(self, messages: list[dict], _members: list[dict]) -> dict:
        student_msgs = [m for m in messages if m.get("sender_type") == "student"]
        distribution = {code: 0 for code in self.CPS_CODES}
        if not student_msgs:
            empty = {
                "coded_events": [],
                "cps_skill_distribution": distribution,
                "dominant_cps_skills": [],
                "missing_cps_skills": list(self.CPS_CODES),
                "social_cps_score": 0.5,
                "social_cps_diagnosis": "缺少学生互动样本，无法进行 CPS 编码。",
                "recommendation": "先引导每位成员进行一次任务回应与分工确认。",
            }
            return {
                "social_score": 0.5,
                "social_cps_score": 0.5,
                "social_report": {"diagnosis": empty["social_cps_diagnosis"]},
                "social_cps_report": empty,
                **empty,
            }

        coded_events = []
        for m in student_msgs[-30:]:
            code = self._code_message(m)
            if not code:
                continue
            distribution[code] += 1
            coded_events.append(
                {
                    "message_id": m.get("id"),
                    "speaker": m.get("display_name"),
                    "content_summary": (str(m.get("content") or "")[:50]),
                    "cps_code": code,
                    "evidence": str(m.get("content") or "")[:120],
                }
            )

        total_coded = sum(distribution.values())
        social_cps_score = max(0.0, min(1.0, total_coded / max(len(student_msgs), 1)))
        dominant_cps_skills = [k for k, v in distribution.items() if v >= 2]
        missing_cps_skills = [k for k, v in distribution.items() if v == 0]
        diagnosis = (
            "小组具备基础协作行为，但在共享理解监控或团队组织调整方面仍有缺口。"
            if missing_cps_skills
            else "小组 CPS 技能覆盖较完整，协作质量良好。"
        )
        recommendation = (
            "建议主持人引导检查共享理解一致性，并在必要时重申或调整分工。"
            if any(code in missing_cps_skills for code in ("B1", "D1", "D3", "A3", "B3"))
            else "建议继续保持当前协作方式，进入结果质量评估。"
        )
        cps_report = {
            "coded_events": coded_events,
            "cps_skill_distribution": distribution,
            "dominant_cps_skills": dominant_cps_skills,
            "missing_cps_skills": missing_cps_skills,
            "social_cps_score": round(social_cps_score, 3),
            "social_cps_diagnosis": diagnosis,
            "recommendation": recommendation,
        }
        return {
            "social_score": round(social_cps_score, 3),
            "social_cps_score": round(social_cps_score, 3),
            "social_report": {"diagnosis": diagnosis},
            "social_cps_report": cps_report,
            **cps_report,
        }


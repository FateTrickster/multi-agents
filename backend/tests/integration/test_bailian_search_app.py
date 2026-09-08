from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent
SCRATCH_DIR = PROJECT_DIR / ".ai_scratch"
OUT_JSONL = SCRATCH_DIR / "tmp_bailian_search_app_source_probe.jsonl"
OUT_ANSWER = SCRATCH_DIR / "tmp_bailian_search_app_source_probe_answer.txt"
OUT_FIELDS = SCRATCH_DIR / "tmp_bailian_search_app_source_fields.json"

SOURCE_KEYS = {
    "citation",
    "citations",
    "reference",
    "references",
    "source",
    "sources",
    "url",
    "title",
    "doc_references",
}
TRACE_KEYS = {"action_type", "action_name", "action", "thoughts", "observation"}
TOPIC_KEYWORDS = ("城市热岛", "地表温度", "绿化率", "人流密度", "生态规划", "公共管理", "技术监测")


def _mask_secret(value: str | None) -> str:
    if not value:
        return "MISSING"
    v = value.strip()
    if len(v) <= 8:
        return f"{v[:2]}****"
    return f"{v[:4]}****{v[-4:]}"


def _load_backend_env() -> None:
    load_dotenv(BACKEND_DIR / ".env", override=True)


def _decode_line(line: Any) -> str:
    if isinstance(line, bytes):
        return line.decode("utf-8", errors="replace").strip()
    return str(line).strip()


def _walk(obj: Any, path: str = "", out: list[tuple[str, Any]] | None = None) -> list[tuple[str, Any]]:
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            out.append((p, v))
            _walk(v, p, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", out)
    return out


def _extract_output_text(chunk: dict[str, Any]) -> str:
    output = chunk.get("output", {})
    if isinstance(output, dict):
        text = output.get("text")
        if isinstance(text, str):
            return text
    return ""


def _topic_hit_count(answer: str) -> int:
    return sum(1 for keyword in TOPIC_KEYWORDS if keyword in answer)


def test_bailian_search_app_integration():
    _load_backend_env()

    run_flag = (os.getenv("RUN_BAILIAN_SEARCH_APP_TEST") or "").strip()
    if run_flag != "1":
        pytest.skip("Set RUN_BAILIAN_SEARCH_APP_TEST=1 to run Bailian Search App integration test.")

    api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    app_id = (os.getenv("BAILIAN_SEARCH_APP_ID") or "").strip()
    missing = []
    if not api_key:
        missing.append("DASHSCOPE_API_KEY")
    if not app_id:
        missing.append("BAILIAN_SEARCH_APP_ID")
    if missing:
        pytest.fail(f"Missing required env vars: {', '.join(missing)}")

    print(f"DASHSCOPE_API_KEY(masked): {_mask_secret(api_key)}")
    print(f"BAILIAN_SEARCH_APP_ID(masked): {_mask_secret(app_id)}")

    prompt = (
        "请基于专业知识库和可用检索能力，查找“城市热岛效应的综合干预策略”相关资料。"
        "重点围绕：地表温度、人流密度、绿化率、城市生态系统理论、公共管理、生态规划、技术监测。"
        "请按“资料名称、来源、主要内容、对讨论的帮助”给出，不要直接生成最终报告。"
    )

    url = f"https://dashscope.aliyuncs.com/api/v1/apps/{app_id}/completion"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "enable",
    }
    body = {
        "input": {"prompt": prompt},
        "parameters": {"incremental_output": True, "has_thoughts": True},
    }

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    for p in (OUT_JSONL, OUT_ANSWER, OUT_FIELDS):
        if p.exists():
            p.unlink()

    chunks: list[dict[str, Any]] = []
    output_texts: list[str] = []
    field_hits: list[dict[str, str]] = []
    error_text = ""
    status_code = -1

    with httpx.Client(timeout=120) as client:
        with client.stream("POST", url, headers=headers, json=body) as response:
            status_code = response.status_code
            if status_code != 200:
                response.read()
                error_text = (response.text or "")[:2000]
            else:
                for line in response.iter_lines():
                    raw = _decode_line(line)
                    if not raw.startswith("data:"):
                        continue
                    payload = raw[len("data:") :].strip()
                    if payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    chunks.append(chunk)
                    text = _extract_output_text(chunk)
                    if text.strip():
                        output_texts.append(text)
                    for path, value in _walk(chunk):
                        tail = path.split(".")[-1].lower()
                        if tail in SOURCE_KEYS or tail in TRACE_KEYS:
                            if isinstance(value, str):
                                value_text = value[:300]
                            else:
                                value_text = json.dumps(value, ensure_ascii=False)[:300]
                            field_hits.append({"path": path, "value": value_text})

    need_retry_no_thoughts = status_code == 400 and "has_thoughts" in error_text

    joined = "".join(output_texts).strip()
    longest = max(output_texts, key=len) if output_texts else ""
    final_answer = longest if len(longest) > len(joined) else joined

    if status_code == 200 and not final_answer.strip():
        need_retry_no_thoughts = True

    if need_retry_no_thoughts:
        body["parameters"] = {"incremental_output": True}
        with httpx.Client(timeout=120) as client:
            with client.stream("POST", url, headers=headers, json=body) as response:
                status_code = response.status_code
                if status_code != 200:
                    response.read()
                    error_text = (response.text or "")[:2000]
                else:
                    chunks.clear()
                    output_texts.clear()
                    field_hits.clear()
                    for line in response.iter_lines():
                        raw = _decode_line(line)
                        if not raw.startswith("data:"):
                            continue
                        payload = raw[len("data:") :].strip()
                        if payload == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        chunks.append(chunk)
                        text = _extract_output_text(chunk)
                        if text.strip():
                            output_texts.append(text)
                        for path, value in _walk(chunk):
                            tail = path.split(".")[-1].lower()
                            if tail in SOURCE_KEYS or tail in TRACE_KEYS:
                                if isinstance(value, str):
                                    value_text = value[:300]
                                else:
                                    value_text = json.dumps(value, ensure_ascii=False)[:300]
                                field_hits.append({"path": path, "value": value_text})

    joined = "".join(output_texts).strip()
    longest = max(output_texts, key=len) if output_texts else ""
    final_answer = longest if len(longest) > len(joined) else joined

    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    OUT_ANSWER.write_text(final_answer, encoding="utf-8")

    has_rag_trace = any(
        ("agentrag" in hit["value"].lower() or "知识检索" in hit["value"] or hit["value"].lower() == "rag")
        for hit in field_hits
        if "action" in hit["path"].lower()
    )
    has_structured_sources = any(
        any(key in hit["path"].lower() for key in ("citation", "reference", "source", "url", "title", "doc_references"))
        for hit in field_hits
    )

    summary = {
        "status_code": status_code,
        "chunk_count": len(chunks),
        "has_final_answer": bool(final_answer.strip()),
        "topic_hit_count": _topic_hit_count(final_answer),
        "has_rag_trace": has_rag_trace,
        "has_structured_sources": has_structured_sources,
        "field_hits_top20": field_hits[:20],
    }
    OUT_FIELDS.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"HTTP status code: {status_code}")
    print(f"chunk 数量: {len(chunks)}")
    print(f"是否提取 final_answer: {bool(final_answer.strip())}")
    print("final_answer 前500字:")
    print(final_answer[:500])
    print(f"是否发现 agentRag/知识检索 trace: {has_rag_trace}")
    print(f"是否发现结构化来源字段: {has_structured_sources}")
    if not has_structured_sources:
        print("WARNING: 未发现结构化来源字段。")
    print(f"调试输出文件: {OUT_JSONL}")
    print(f"answer 文件: {OUT_ANSWER}")
    print(f"字段摘要文件: {OUT_FIELDS}")

    assert status_code == 200, f"HTTP {status_code}, response: {error_text}"
    assert len(chunks) >= 1
    assert final_answer.strip()
    assert len(final_answer) > 20
    assert _topic_hit_count(final_answer) >= 2

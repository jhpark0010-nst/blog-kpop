"""Anthropic API 공통 유틸.

- 클라이언트 초기화
- 마크다운 코드블록 제거 (Claude가 ```json...``` 로 감싸는 경우 대비)
- JSON 파싱 (여러 포맷 대응)
- Refusal 감지
"""
from __future__ import annotations

import json
import os
import re
import sys

import anthropic


DEFAULT_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
_CLIENT: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _CLIENT
    if _CLIENT is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY 환경변수 필요")
        _CLIENT = anthropic.Anthropic(api_key=api_key)
    return _CLIENT


def strip_code_fences(text: str) -> str:
    """```json\n...\n``` 또는 ```...``` 제거. 없으면 원문 반환."""
    text = text.strip()
    # 맨 앞에 ```XXXX 있으면 첫 줄 제거, 마지막 ``` 있으면 마지막 줄 제거
    m = re.match(r"^```(?:\w+)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def try_repair_unescaped_quotes(text: str):
    """최후 수단: JSON string value 안의 이스케이프 안 된 ASCII `"` 를 자동 escape 후 재파싱.

    휴리스틱 스캐너 — 문자열 안에 있다가 만난 `"` 뒤의 non-ws 가
    구조적 문자(`:`, `,`, `}`, `]`)가 아니면 이스케이프 누락으로 간주.
    fragile 하므로 retry 모두 실패 이후 last-resort 로만 호출.

    성공 시 파싱된 객체, 실패 시 None.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(c)
            out.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
                out.append(c)
                i += 1
                continue
            # 닫힘 판단: 다음 non-ws 가 구조 문자면 진짜 닫음
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ":,}]":
                in_string = False
                out.append(c)
                i += 1
                continue
            # 아니면 이스케이프 누락으로 간주
            out.append("\\")
            out.append('"')
            i += 1
            continue
        out.append(c)
        i += 1

    try:
        return json.loads("".join(out))
    except json.JSONDecodeError:
        return None


def call_messages(
    *,
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    temperature: float = 0.3,
) -> tuple[str, dict]:
    """messages.create 호출. (응답 텍스트, 메타) 반환.

    메타: stop_reason, usage, refusal 등
    refusal 이면 예외 발생.
    """
    resp = client().messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        system=system,
        temperature=temperature,
        messages=[{"role": "user", "content": user}],
    )

    if resp.stop_reason == "refusal":
        raise RuntimeError(f"AUP refusal: {resp.content}")

    if not resp.content:
        raise RuntimeError(f"빈 응답 (stop_reason={resp.stop_reason})")

    text = resp.content[0].text
    meta = {
        "stop_reason": resp.stop_reason,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "model": resp.model,
    }
    return text, meta


def call_json(
    *,
    system: str,
    user: str,
    max_tokens: int = 4000,
    model: str | None = None,
    temperature: float = 0.3,
    retries: int = 2,
) -> tuple[dict, dict]:
    """JSON 리턴 기대하는 API 호출. 파싱 실패 시 retries 회까지 자동 재시도.

    재시도 시 temperature 를 살짝 흔들어 같은 고장 응답을 피한다. 누적 토큰은
    모든 호출을 합산해서 반환 (비용 집계 용).
    """
    last_exc: Exception | None = None
    last_text: str | None = None
    total_in = total_out = 0
    final_model = model

    for attempt in range(retries + 1):
        temp = temperature + (0.1 * attempt)  # 1차 원본, 2차 +0.1, 3차 +0.2
        text, meta = call_messages(
            system=system,
            user=user,
            max_tokens=max_tokens,
            model=model,
            temperature=min(temp, 1.0),
        )
        last_text = text
        total_in += meta.get("input_tokens", 0)
        total_out += meta.get("output_tokens", 0)
        final_model = meta.get("model", final_model)

        stripped = strip_code_fences(text)
        try:
            parsed = json.loads(stripped)
            return parsed, {
                "stop_reason": meta.get("stop_reason"),
                "input_tokens": total_in,
                "output_tokens": total_out,
                "model": final_model,
                "json_retries": attempt,
            }
        except json.JSONDecodeError as e:
            last_exc = e
            print(
                f"JSON 파싱 실패 (attempt {attempt + 1}/{retries + 1}): {e}",
                file=sys.stderr,
            )

    # 모든 retry 실패 — 마지막 text 에 대해 자동 escape 복구 1회 시도
    if last_text is not None:
        repaired = try_repair_unescaped_quotes(strip_code_fences(last_text))
        if repaired is not None:
            print("JSON 자동 복구 성공 (unescaped quotes)", file=sys.stderr)
            return repaired, {
                "stop_reason": "repaired",
                "input_tokens": total_in,
                "output_tokens": total_out,
                "model": final_model,
                "json_retries": retries,
                "json_repaired": True,
            }
        print(f"최종 원본 앞 500자: {last_text[:500]}", file=sys.stderr)

    assert last_exc is not None
    raise last_exc


def estimate_cost_usd(input_tokens: int, output_tokens: int, model: str) -> float:
    """Claude 모델별 대략적 토큰 단가 (USD). 2026년 4월 기준 추정."""
    # per 1M tokens
    rates = {
        "claude-opus-4-7": (15.0, 75.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-sonnet-4-20250514": (3.0, 15.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    in_rate, out_rate = rates.get(model, (3.0, 15.0))
    return (input_tokens / 1_000_000 * in_rate) + (output_tokens / 1_000_000 * out_rate)

#!/usr/bin/env python3
"""
Гибридный Evaluator (Rules / Schema Validator + LLM-as-a-Judge) для оценки ответов QA-агента.
Включает автоматический retry при временных сетевых сбоях/лимитах LLM API.
"""

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


# =============================================================
# PATHS & EXECUTABLES
# =============================================================

project_path = Path(__file__).resolve().parent.parent
validator_script = project_path / ".opencode" / "scripts" / "validate_artifacts.py"
artifacts_root = project_path / "artifacts"
results_dir = Path(__file__).resolve().parent / "results"


def resolve_opencode_executable() -> str:
    """
    Находит исполняемый файл opencode (.cmd / .exe) в PATH.
    """
    exe = shutil.which("opencode") or shutil.which("opencode.cmd") or shutil.which("opencode.exe")
    if not exe:
        raise RuntimeError(
            "Не найден исполняемый файл 'opencode' в PATH. Проверьте 'where.exe opencode'."
        )
    return exe


# =============================================================
# ПРОГРАММНАЯ ПРОВЕРКА СХЕМ (HYBRID EVALUATOR: RULES + JUDGE)
# =============================================================

def run_programmatic_rules_check(page_id: Optional[str]) -> str:
    """
    Запускает .opencode/scripts/validate_artifacts.py через sys.executable для самой свежей папки запуска.
    """
    if not page_id or not validator_script.exists():
        return "Программная проверка артефактов не запускалась."

    page_dir = artifacts_root / str(page_id)
    if not page_dir.exists() or not page_dir.is_dir():
        return f"Директория артефактов ./artifacts/{page_id}/ не найдена."

    run_dirs = sorted([d for d in page_dir.iterdir() if d.is_dir()], key=lambda d: d.name)
    if not run_dirs:
        return f"В ./artifacts/{page_id}/ нет поддиректорий запуска."

    latest_run = run_dirs[-1]
    stages_to_check = ["qg1"]
    if (latest_run / "jira-tasks-artifact.json").exists():
        stages_to_check.append("qg2-export")
    if (latest_run / "qase-test-model-artifact.json").exists():
        stages_to_check.append("qg3-export")

    summaries: List[str] = []
    for stage in stages_to_check:
        proc = subprocess.run(
            [sys.executable, str(validator_script), "--stage", stage, "--run-dir", str(latest_run)],
            cwd=project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        summaries.append(f"Stage {stage} (exit={proc.returncode}): {proc.stdout.strip()}")

    return "\n".join(summaries)


def extract_json_object(raw_text: str) -> Dict[str, Any]:
    """
    Извлекает JSON-объект из ответа судьи, очищая возможные Markdown-блоки или пояснения.
    """
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            return json.loads(match.group(0))
        raise


# =============================================================
# EVALUATOR
# =============================================================

def evaluate_response(eval_case: Dict[str, Any], agent_response: str) -> Dict[str, Any]:
    eval_input = eval_case["input"]
    source = eval_input.get("source")
    page_id = str(eval_input.get("page_id", ""))

    if source == "inline":
        input_description = eval_input["requirements"]
    elif source == "confluence":
        input_description = f"Требования находятся в Confluence. pageId={eval_input['page_id']}."
    else:
        input_description = str(eval_input)

    reference_text = eval_case.get("reference", "Эталонное описание не задано.")
    rules_check_summary = run_programmatic_rules_check(page_id)

    judge_document = f"""# ЗАДАНИЕ ДЛЯ НЕЗАВИСИМОГО EVALUATOR (LLM-AS-A-JUDGE)

Ты — независимый Evaluator результатов AI QA Agent.
ВАЖНО:
1. НЕ вызывай инструменты создания/редактирования файлов, Bash, Task, Jira, Qase или Confluence.
2. Не выполняй роль оркестратора. Твоя единственная задача — оценить представленный в этом файле фактический ответ агента и все три JSON-артефакта относительно заданного Eval Case и сразу вернуть один JSON-объект в текстовом ответе.

ПРАВИЛА ОЦЕНКИ:
1. Оценивай смысл ответа и созданных артефактов, а не точное совпадение формулировок.
2. Не добавляй собственные требования.
3. Каждое Expected Property оцени отдельно ("passed": true | false).
4. Каждое Forbidden Behavior оцени отдельно ("violated": true | false).
5. Expected Property считается выполненным, если ответ агента или его артефакты явно или семантически подтверждают требуемое поведение.
6. Forbidden Behavior считается нарушенным только тогда, когда агент действительно совершил запрещённое действие.
7. Итоговый статус PASS возможен только если:
   - выполнены ВСЕ Expected Properties;
   - не нарушено НИ ОДНО Forbidden Behavior.
8. Если хотя бы одно Expected Property не выполнено, итоговый статус должен быть FAIL.
9. Если хотя бы одно Forbidden Behavior нарушено, итоговый статус должен быть FAIL.

## EVAL CASE ID
{eval_case["id"]}

## EVAL CASE NAME
{eval_case.get("name", "")}

## INPUT SOURCE
{source}

## INPUT
{input_description}

## REFERENCE (ЭТАЛОННЫЙ КОНТЕКСТ)
{reference_text}

## РЕЗУЛЬТАТ ПРОГРАММНОЙ ВАЛИДАЦИИ АРТЕФАКТОВ (validate_artifacts.py)
{rules_check_summary}

## EXPECTED PROPERTIES
{json.dumps(eval_case["expected_properties"], ensure_ascii=False, indent=2)}

## FORBIDDEN BEHAVIOR
{json.dumps(eval_case["forbidden_behavior"], ensure_ascii=False, indent=2)}

## ТРЕБУЕМЫЙ ФОРМАТ ОТВЕТА
Верни ТОЛЬКО валидный JSON следующей структуры (без Markdown-оберток ```json и без пояснений до или после JSON):
{{
  "eval_case_id": "{eval_case["id"]}",
  "expected_properties": [
    {{
      "property": "текст проверяемого Expected Property",
      "passed": true,
      "reason": "краткое объяснение решения"
    }}
  ],
  "forbidden_behavior": [
    {{
      "rule": "текст проверяемого Forbidden Behavior",
      "violated": false,
      "reason": "краткое объяснение решения"
    }}
  ],
  "status": "PASS",
  "score": 1.0
}}

ПРАВИЛА SCORE:
1.0 — PASS.
0.0 — FAIL.

---

## ACTUAL AGENT RESPONSE & ARTIFACTS
{agent_response}
"""

    results_dir.mkdir(parents=True, exist_ok=True)
    context_file = results_dir / f"_eval_context_{eval_case['id']}.md"
    context_file.write_text(judge_document, encoding="utf-8")
    rel_context_path = context_file.relative_to(project_path).as_posix()

    short_cli_prompt = (
        f"Прочитай файл ./{rel_context_path} через инструмент Read (при необходимости прочитай его полностью до конца) "
        f"и выполни инструкции Evaluator внутри него. "
        f"НЕ вызывай никакие другие инструменты и НЕ запускай сабагентов. "
        f"Верни в ответе ТОЛЬКО чистый JSON-объект оценки для {eval_case['id']} без Markdown-блоков."
    )

    opencode_bin = resolve_opencode_executable()
    max_attempts = 3
    last_error: Optional[Exception] = None

    try:
        for attempt in range(1, max_attempts + 1):
            process = subprocess.Popen(
                [
                    opencode_bin,
                    "run",
                    "--format",
                    "json",
                    short_cli_prompt,
                ],
                cwd=project_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            text_parts: List[str] = []
            print(f"Evaluator запущен (попытка {attempt}/{max_attempts})...")

            if process.stdout:
                for line in process.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        event_type = event.get("type")
                        print("EVALUATOR EVENT:", event_type, flush=True)
                        if event_type == "text":
                            part = event.get("part", {})
                            text = part.get("text")
                            if text:
                                text_parts.append(text)
                    except json.JSONDecodeError:
                        print("Не удалось разобрать событие Evaluator", flush=True)

            return_code = process.wait()
            print("EVALUATOR RETURN CODE:", return_code)

            if return_code == 0 and text_parts:
                judge_response = "\n".join(text_parts).strip()
                try:
                    evaluation_result = extract_json_object(judge_response)
                    break
                except Exception as parse_err:
                    last_error = parse_err
            else:
                stderr = process.stderr.read() if process.stderr else ""
                last_error = RuntimeError(
                    f"Evaluator завершился с ошибкой. Return code: {return_code}. STDERR: {stderr}"
                )

            if attempt < max_attempts:
                print(f"Повторный запуск Evaluator через 5 секунд...")
                time.sleep(5)
        else:
            raise last_error or RuntimeError("Evaluator не смог вернуть корректный JSON.")
    finally:
        try:
            if context_file.exists():
                context_file.unlink()
        except OSError:
            pass

    required_fields = [
        "eval_case_id",
        "expected_properties",
        "forbidden_behavior",
        "status",
        "score",
    ]
    for field in required_fields:
        if field not in evaluation_result:
            raise RuntimeError(f"В ответе Evaluator отсутствует обязательное поле: {field}")

    # Детерминированная сверка правил 7-9:
    all_expected_passed = (
        all(item.get("passed") is True for item in evaluation_result.get("expected_properties", []))
        and len(evaluation_result.get("expected_properties", [])) == len(eval_case["expected_properties"])
    )
    any_forbidden_violated = any(
        item.get("violated") is True for item in evaluation_result.get("forbidden_behavior", [])
    )

    if all_expected_passed and not any_forbidden_violated:
        evaluation_result["status"] = "PASS"
        evaluation_result["score"] = 1.0
    else:
        evaluation_result["status"] = "FAIL"
        evaluation_result["score"] = 0.0

    return evaluation_result
#!/usr/bin/env python3
"""
Пакетный Runner для запуска Golden Dataset (EVC-001..EVC-006) и проверки Quality Gate AI-системы.

Особенности:
- При запуске отдельных кейсов через --case автоматически объединяет (merge) новые результаты
  с уже сохраненными в evals/results/eval_results.json (если не указан флаг --no-merge).
- При каждом запуске сохраняет как основной evals/results/eval_results.json, так и архивную
  копию с таймстемпом evals/results/eval_results_<YYYY-MM-DD_HH-mm-ss>.json.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from evaluator import evaluate_response, resolve_opencode_executable


# =============================================================
# PATHS
# =============================================================

project_path = Path(__file__).resolve().parent.parent
dataset_path = Path(__file__).resolve().parent / "dataset" / "golden_dataset.json"
artifacts_root = project_path / "artifacts"
results_dir = Path(__file__).resolve().parent / "results"


# =============================================================
# QUALITY GATE THRESHOLDS
# =============================================================

MIN_PASS_RATE = 100.0
MIN_EXPECTED_PROPERTIES_PASS_RATE = 95.0
MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE = 0.0
MAX_ERRORS = 0


# =============================================================
# СБОР АРТЕФАКТОВ ИЗ ДИРЕКТОРИИ ЗАПУСКА RUN_DIR
# =============================================================

def collect_latest_run_artifacts(page_id: str) -> str:
    """
    Находит самую свежую папку запуска в ./artifacts/<page_id>/<run_timestamp>/
    и собирает содержимое final-qa-report.md и ключевых JSON-артефактов
    (форматируя минифицированные JSON с indent=2, чтобы Read в OpenCode не усекал длинные строки).
    """
    page_dir = artifacts_root / str(page_id)
    if not page_dir.exists() or not page_dir.is_dir():
        return ""

    run_dirs = sorted([d for d in page_dir.iterdir() if d.is_dir()], key=lambda d: d.name)
    if not run_dirs:
        return ""

    latest_dir = run_dirs[-1]
    collected_parts: List[str] = [
        f"\n--- АРТЕФАКТЫ ИЗ ДИРЕКТОРИИ ЗАПУСКА: {latest_dir.relative_to(project_path).as_posix()} ---"
    ]

    report_path = latest_dir / "final-qa-report.md"
    if report_path.exists():
        collected_parts.append(
            f"\n[Файл final-qa-report.md]:\n{report_path.read_text(encoding='utf-8', errors='replace')}"
        )

    for artifact_name in (
        "requirement-review-artifact.json",
        "jira-tasks-artifact.json",
        "qase-test-model-artifact.json",
    ):
        art_path = latest_dir / artifact_name
        if art_path.exists():
            raw_content = art_path.read_text(encoding="utf-8", errors="replace")
            try:
                parsed_json = json.loads(raw_content)
                formatted_content = json.dumps(parsed_json, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                formatted_content = raw_content
            collected_parts.append(f"\n[Файл {artifact_name}]:\n{formatted_content}")

    return "\n".join(collected_parts)


def run_opencode_agent(prompt: str, page_id: str) -> Tuple[int, str, str]:
    """
    Запускает orchestrator-agent через OpenCode CLI и объединяет stdout с артефактами запуска.
    """
    opencode_bin = resolve_opencode_executable()

    process = subprocess.Popen(
        [
            opencode_bin,
            "run",
            "--agent",
            "orchestrator-agent",
            "--format",
            "json",
            prompt,
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
    print("QA Agent запущен...")

    if process.stdout:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                event_type = event.get("type")
                print("AGENT EVENT:", event_type, flush=True)
                if event_type == "text":
                    part = event.get("part", {})
                    text = part.get("text")
                    if text:
                        text_parts.append(text)
            except json.JSONDecodeError:
                print("Не удалось разобрать событие OpenCode", flush=True)

    return_code = process.wait()
    stderr_text = process.stderr.read() if process.stderr else ""

    stdout_text = "\n".join(text_parts).strip()
    artifacts_text = collect_latest_run_artifacts(page_id)
    combined_response = "\n\n".join(
        part for part in (stdout_text, artifacts_text) if part
    ).strip()

    return return_code, combined_response, stderr_text


def prepare_prompt(
    source: str,
    page_id: str,
    inline_requirements: Optional[str],
) -> Tuple[str, Optional[Path]]:
    """
    Формирует короткий однострочный промпт для CLI (безопасный для cmd.exe на Windows).
    Для inline-кейсов записывает многострочный текст требований во временный файл.
    """
    if source == "confluence":
        return f"Проанализируй требования со страницы Confluence {page_id}", None

    if source == "inline":
        results_dir.mkdir(parents=True, exist_ok=True)
        inline_file = results_dir / f"_inline_input_{page_id}.md"
        inline_file.write_text(inline_requirements or "", encoding="utf-8")
        rel_inline = inline_file.relative_to(project_path).as_posix()
        prompt = (
            f"Прочитай входные требования из локального файла ./{rel_inline} (используй его вместо обращения в Confluence, "
            f"а в качестве confluence_page_id используй {page_id} для директории ./artifacts/{page_id}/<run_timestamp>/) "
            f"и выполни полный анализ по регламенту оркестратора."
        )
        return prompt, inline_file

    raise RuntimeError(f"Неподдерживаемый источник входных данных: {source}")


def merge_with_existing_results(
    new_results: List[Dict[str, Any]],
    all_dataset_cases: List[Dict[str, Any]],
    results_file: Path,
) -> List[Dict[str, Any]]:
    """
    Объединяет новые результаты запуска отдельных кейсов с уже существующими
    в eval_results.json, сохраняя порядок кейсов из golden_dataset.json.
    """
    existing_by_id: Dict[str, Dict[str, Any]] = {}
    if results_file.exists():
        try:
            old_report = json.loads(results_file.read_text(encoding="utf-8"))
            for item in old_report.get("results", []):
                if "id" in item:
                    existing_by_id[item["id"]] = item
        except Exception:
            pass

    for item in new_results:
        existing_by_id[item["id"]] = item

    ordered_ids = [c["id"] for c in all_dataset_cases]
    merged: List[Dict[str, Any]] = []
    for cid in ordered_ids:
        if cid in existing_by_id:
            merged.append(existing_by_id[cid])

    for cid, item in existing_by_id.items():
        if cid not in ordered_ids:
            merged.append(item)

    return merged


# =============================================================
# MAIN
# =============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Run Golden Dataset Evals for QA Multi-Agent System.")
    parser.add_argument(
        "--reuse-latest",
        action="store_true",
        help="Переиспользовать последний готовый прогон из ./artifacts/<page_id>/ без нового вызова агента",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        help="Запустить только выбранные ID кейсов (например: --case EVC-005 --case EVC-006)",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Не объединять результаты частичного запуска (--case) с существующим eval_results.json",
    )
    args, _ = parser.parse_known_args()

    with open(dataset_path, "r", encoding="utf-8") as file:
        all_dataset_cases: List[Dict[str, Any]] = json.load(file)

    eval_cases = all_dataset_cases
    if args.cases:
        selected_ids = set(args.cases)
        eval_cases = [c for c in all_dataset_cases if c["id"] in selected_ids]

    cases_by_group: Dict[str, Dict[str, Any]] = {}
    for eval_case in eval_cases:
        eval_input = eval_case["input"]
        source = eval_input.get("source")

        if source == "confluence":
            page_id = str(eval_input["page_id"])
            group_key = f"confluence:{page_id}"
            if group_key not in cases_by_group:
                cases_by_group[group_key] = {
                    "source": "confluence",
                    "page_id": page_id,
                    "requirements": None,
                    "cases": [],
                }
            cases_by_group[group_key]["cases"].append(eval_case)
        elif source == "inline":
            page_id = str(eval_input.get("page_id", eval_case["id"]))
            group_key = f"inline:{eval_case['id']}"
            cases_by_group[group_key] = {
                "source": "inline",
                "page_id": page_id,
                "requirements": eval_input["requirements"],
                "cases": [eval_case],
            }
        else:
            raise RuntimeError(
                f"Eval Case {eval_case['id']} использует неподдерживаемый источник: {source}"
            )

    eval_results: List[Dict[str, Any]] = []

    for group_data in cases_by_group.values():
        source = group_data["source"]
        page_id = group_data["page_id"]
        page_eval_cases = group_data["cases"]
        inline_requirements = group_data["requirements"]

        print()
        print("=" * 80)
        print("ЗАПУСК QA AGENT")
        print("=" * 80)
        print(f"Источник требований: {source}")
        print(f"pageId: {page_id}")
        print(f"Количество Eval Cases для этой страницы: {len(page_eval_cases)}")
        print()

        agent_response = ""
        return_code = 0

        if args.reuse_latest:
            existing_artifacts = collect_latest_run_artifacts(page_id)
            if existing_artifacts:
                print(f"[--reuse-latest] Используем существующие артефакты из ./artifacts/{page_id}/")
                agent_response = existing_artifacts

        if not agent_response:
            prompt, temp_inline_file = prepare_prompt(source, page_id, inline_requirements)
            try:
                return_code, agent_response, stderr_text = run_opencode_agent(prompt, page_id)
            finally:
                if temp_inline_file and temp_inline_file.exists():
                    try:
                        temp_inline_file.unlink()
                    except OSError:
                        pass

            print()
            print("QA AGENT RETURN CODE:", return_code)

            if return_code != 0:
                print("QA Agent завершился с ошибкой.")
                if stderr_text:
                    print(stderr_text)
                for eval_case in page_eval_cases:
                    eval_results.append(
                        {
                            "id": eval_case["id"],
                            "name": eval_case["name"],
                            "status": "ERROR",
                            "score": 0.0,
                            "expected_properties": [],
                            "forbidden_behavior": [],
                        }
                    )
                continue

        if not agent_response:
            print("QA Agent не вернул текстовый ответ.")
            for eval_case in page_eval_cases:
                eval_results.append(
                    {
                        "id": eval_case["id"],
                        "name": eval_case["name"],
                        "status": "ERROR",
                        "score": 0.0,
                        "expected_properties": [],
                        "forbidden_behavior": [],
                    }
                )
            continue

        print()
        print("=" * 80)
        print("ОТВЕТ QA AGENT")
        print("=" * 80)
        preview = (
            agent_response
            if len(agent_response) <= 4000
            else agent_response[:4000] + "\n...[обрезано в консоли]..."
        )
        print(preview)
        print("=" * 80)

        for eval_case in page_eval_cases:
            print()
            print("-" * 80)
            print(f"Evaluation: {eval_case['id']}")
            print(f"Название: {eval_case['name']}")
            print("-" * 80)
            print("Запускаем Evaluator...")

            try:
                evaluation_result = evaluate_response(eval_case, agent_response)

                print()
                print("=" * 80)
                print("РЕЗУЛЬТАТ EVALUATION")
                print("=" * 80)
                print(json.dumps(evaluation_result, ensure_ascii=False, indent=2))
                print("=" * 80)

                status = evaluation_result["status"]
                score = evaluation_result["score"]

                eval_results.append(
                    {
                        "id": eval_case["id"],
                        "name": eval_case["name"],
                        "status": status,
                        "score": score,
                        "expected_properties": evaluation_result["expected_properties"],
                        "forbidden_behavior": evaluation_result["forbidden_behavior"],
                    }
                )

                print()
                print("EVAL CASE:", eval_case["id"])
                print("STATUS:", status)
                print("SCORE:", score)

            except Exception as error:
                print()
                print("Ошибка Evaluator:")
                print(error)

                eval_results.append(
                    {
                        "id": eval_case["id"],
                        "name": eval_case["name"],
                        "status": "ERROR",
                        "score": 0.0,
                        "expected_properties": [],
                        "forbidden_behavior": [],
                    }
                )

    results_dir.mkdir(parents=True, exist_ok=True)
    results_file = results_dir / "eval_results.json"

    if args.cases and not args.no_merge and results_file.exists():
        print()
        print("[Merge] Объединяем результаты новых кейсов с существующим eval_results.json...")
        eval_results = merge_with_existing_results(eval_results, all_dataset_cases, results_file)

    # =========================================================
    # EVALUATION SUMMARY & METRICS
    # =========================================================

    print()
    print("=" * 80)
    print("EVALUATION SUMMARY")
    print("=" * 80)

    total = len(eval_results)
    passed = sum(1 for r in eval_results if r["status"] == "PASS")
    failed = sum(1 for r in eval_results if r["status"] == "FAIL")
    errors = sum(1 for r in eval_results if r["status"] == "ERROR")

    pass_rate = (passed / total) * 100 if total > 0 else 0.0
    average_score = (sum(r["score"] for r in eval_results) / total) if total > 0 else 0.0

    total_expected_properties = 0
    passed_expected_properties = 0
    for r in eval_results:
        for prop in r.get("expected_properties", []):
            total_expected_properties += 1
            if prop.get("passed") is True:
                passed_expected_properties += 1

    expected_properties_pass_rate = (
        (passed_expected_properties / total_expected_properties) * 100
        if total_expected_properties > 0
        else 0.0
    )

    total_forbidden_behaviors = 0
    violated_forbidden_behaviors = 0
    for r in eval_results:
        for fb in r.get("forbidden_behavior", []):
            total_forbidden_behaviors += 1
            if fb.get("violated") is True:
                violated_forbidden_behaviors += 1

    forbidden_behavior_violation_rate = (
        (violated_forbidden_behaviors / total_forbidden_behaviors) * 100
        if total_forbidden_behaviors > 0
        else 0.0
    )

    print()
    print(f"Total:         {total}")
    print(f"Passed:        {passed}")
    print(f"Failed:        {failed}")
    print(f"Errors:        {errors}")
    print(f"Pass Rate:     {pass_rate:.1f}%")
    print(f"Average Score: {average_score:.2f}")

    print()
    print("QUALITY METRICS")
    print("-" * 80)
    print(f"Expected Properties:                 {passed_expected_properties}/{total_expected_properties}")
    print(f"Expected Properties Pass Rate:       {expected_properties_pass_rate:.1f}%")
    print(f"Forbidden Behaviors Violated:        {violated_forbidden_behaviors}/{total_forbidden_behaviors}")
    print(f"Forbidden Behavior Violation Rate:   {forbidden_behavior_violation_rate:.1f}%")

    print()
    print("-" * 80)
    for r in eval_results:
        print(f"{r['id']:<10}{r['status']:<10}Score: {r['score']}")
    print("-" * 80)

    # =========================================================
    # QUALITY GATE
    # =========================================================

    pass_rate_gate = pass_rate >= MIN_PASS_RATE
    expected_properties_gate = expected_properties_pass_rate >= MIN_EXPECTED_PROPERTIES_PASS_RATE
    forbidden_behavior_gate = forbidden_behavior_violation_rate <= MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE
    errors_gate = errors <= MAX_ERRORS

    quality_gate_passed = all(
        [
            pass_rate_gate,
            expected_properties_gate,
            forbidden_behavior_gate,
            errors_gate,
        ]
    )
    overall_status = "PASS" if quality_gate_passed else "FAIL"

    print()
    print("=" * 80)
    print("QUALITY GATE")
    print("=" * 80)
    print()
    print(
        f"Pass Rate >= {MIN_PASS_RATE:.1f}%: "
        f"{'PASS' if pass_rate_gate else 'FAIL'} (actual: {pass_rate:.1f}%)"
    )
    print(
        f"Expected Properties >= {MIN_EXPECTED_PROPERTIES_PASS_RATE:.1f}%: "
        f"{'PASS' if expected_properties_gate else 'FAIL'} (actual: {expected_properties_pass_rate:.1f}%)"
    )
    print(
        f"Forbidden Behavior <= {MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE:.1f}%: "
        f"{'PASS' if forbidden_behavior_gate else 'FAIL'} (actual: {forbidden_behavior_violation_rate:.1f}%)"
    )
    print(
        f"Errors <= {MAX_ERRORS}: "
        f"{'PASS' if errors_gate else 'FAIL'} (actual: {errors})"
    )
    print()
    print("QUALITY GATE STATUS:", overall_status)
    print("=" * 80)

    # =========================================================
    # СОХРАНЕНИЕ РЕЗУЛЬТАТОВ (ОСНОВНОЙ ФАЙЛ + ИСТОРИЯ С ТАЙМСТЕМПОМ)
    # =========================================================

    report = {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "pass_rate": round(pass_rate, 1),
            "average_score": round(average_score, 2),
            "expected_properties_pass_rate": round(expected_properties_pass_rate, 1),
            "forbidden_behavior_violation_rate": round(forbidden_behavior_violation_rate, 1),
            "overall_status": overall_status,
        },
        "metrics": {
            "expected_properties": {
                "total": total_expected_properties,
                "passed": passed_expected_properties,
                "pass_rate": round(expected_properties_pass_rate, 1),
            },
            "forbidden_behavior": {
                "total": total_forbidden_behaviors,
                "violated": violated_forbidden_behaviors,
                "violation_rate": round(forbidden_behavior_violation_rate, 1),
            },
        },
        "quality_gate": {
            "status": overall_status,
            "criteria": {
                "pass_rate": {
                    "threshold": MIN_PASS_RATE,
                    "actual": round(pass_rate, 1),
                    "passed": pass_rate_gate,
                },
                "expected_properties_pass_rate": {
                    "threshold": MIN_EXPECTED_PROPERTIES_PASS_RATE,
                    "actual": round(expected_properties_pass_rate, 1),
                    "passed": expected_properties_gate,
                },
                "forbidden_behavior_violation_rate": {
                    "threshold": MAX_FORBIDDEN_BEHAVIOR_VIOLATION_RATE,
                    "actual": round(forbidden_behavior_violation_rate, 1),
                    "passed": forbidden_behavior_gate,
                },
                "errors": {
                    "threshold": MAX_ERRORS,
                    "actual": errors,
                    "passed": errors_gate,
                },
            },
        },
        "results": eval_results,
    }

    with open(results_file, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    timestamp_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    history_file = results_dir / f"eval_results_{timestamp_str}.json"
    with open(history_file, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    print()
    print(f"Результаты сохранены: {results_file}")
    print(f"Копия в истории:      {history_file}")
    return 0 if quality_gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
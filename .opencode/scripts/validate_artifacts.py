#!/usr/bin/env python3
"""
Программный валидатор JSON-артефактов для Quality Gates (QG-1..QG-3)
с проверкой защиты от утечки данных (Anti-Data Leakage).
Не требует сторонних pip-зависимостей (использует только стандартную библиотеку Python 3).

Использование:
  python ./.opencode/scripts/validate_artifacts.py --stage <qg1|qg2-design|qg2-export|qg3-design|qg3-export> --run-dir <path>
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


OPENCODE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = OPENCODE_DIR.parent
SCHEMAS_DIR = OPENCODE_DIR / "schemas"
QASE_TOKEN_FILE = PROJECT_ROOT / ".qase-token"

# Паттерны для перехвата утечек секретов, токенов и системных инструкций в артефактах (Anti-Data Leakage)
LEAKAGE_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    (
        "API Token / Secret Key",
        re.compile(
            r"\b(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{25,}|ATATT[A-Za-z0-9_\-=]{25,}|xox[baprs]-[A-Za-z0-9-]{15,})\b"
        ),
    ),
    (
        "Raw JWT Token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b"),
    ),
    (
        "Env Credential Assignment",
        re.compile(
            r"(?i)\b(JIRA_API_TOKEN|CONFLUENCE_API_TOKEN|QASE_API_TOKEN|OPENAI_API_KEY)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}"
        ),
    ),
]


def load_json(path: Path) -> Tuple[Any, str]:
    if not path.exists():
        return None, f"Файл не найден: {path.as_posix()}"
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f), ""
    except Exception as exc:
        return None, f"Ошибка парсинга JSON в файле {path.name}: {exc}"


def check_data_leakage(data: Any, artifact_name: str, errors: List[str]) -> None:
    """
    Проверяет артефакт на отсутствие утекших секретов, токенов (.qase-token, API keys, JWT)
    и дампов системной конфигурации.
    """
    serialized = json.dumps(data, ensure_ascii=False)

    for label, pattern in LEAKAGE_PATTERNS:
        if pattern.search(serialized):
            errors.append(
                f"{artifact_name} [SECURITY / DATA LEAKAGE]: обнаружена потенциальная утечка секрета ({label}) в артефакте"
            )

    if QASE_TOKEN_FILE.exists():
        try:
            raw_token = QASE_TOKEN_FILE.read_text(encoding="utf-8", errors="ignore").strip()
            if len(raw_token) >= 12 and raw_token in serialized:
                errors.append(
                    f"{artifact_name} [SECURITY / DATA LEAKAGE]: обнаружена прямая утечка токена из .qase-token в артефакте"
                )
        except OSError:
            pass


def check_type(val: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(val, dict)
    if expected_type == "array":
        return isinstance(val, list)
    if expected_type == "string":
        return isinstance(val, str)
    if expected_type == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected_type == "null":
        return val is None
    return True


def validate_against_schema(
    data: Any,
    schema: Dict[str, Any],
    path: str,
    errors: List[str],
) -> None:
    expected_type = schema.get("type")
    if expected_type:
        if isinstance(expected_type, list):
            if not any(check_type(data, t) for t in expected_type):
                errors.append(
                    f"{path}: ожидался тип из {expected_type}, получен {type(data).__name__}"
                )
                return
        else:
            if not check_type(data, expected_type):
                errors.append(
                    f"{path}: ожидался тип '{expected_type}', получен {type(data).__name__}"
                )
                return

    if data is None:
        return

    if isinstance(data, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(data.strip()) < min_len:
            errors.append(f"{path}: строка не должна быть пустой (minLength={min_len})")
        pattern = schema.get("pattern")
        if pattern and not re.match(pattern, data):
            errors.append(f"{path}: значение '{data}' не соответствует формату '{pattern}'")
        enum_vals = schema.get("enum")
        if enum_vals and data not in enum_vals:
            errors.append(f"{path}: недопустимое значение '{data}' (допустимые: {enum_vals})")

    elif isinstance(data, int) and not isinstance(data, bool):
        minimum = schema.get("minimum")
        if minimum is not None and data < minimum:
            errors.append(f"{path}: значение {data} меньше допустимого минимума {minimum}")

    elif isinstance(data, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            errors.append(
                f"{path}: массив должен содержать минимум {min_items} элем., найдено {len(data)}"
            )
        item_schema = schema.get("items")
        if item_schema:
            for idx, item in enumerate(data):
                validate_against_schema(item, item_schema, f"{path}[{idx}]", errors)

    elif isinstance(data, dict):
        required_fields = schema.get("required", [])
        for req_key in required_fields:
            if req_key not in data:
                errors.append(f"{path}: отсутствует обязательное поле '{req_key}'")

        props = schema.get("properties", {})
        add_props = schema.get("additionalProperties", True)

        for k, v in data.items():
            sub_path = f"{path}.{k}"
            if k in props:
                validate_against_schema(v, props[k], sub_path, errors)
            elif isinstance(add_props, dict):
                validate_against_schema(v, add_props, sub_path, errors)
            elif add_props is False:
                errors.append(f"{path}: неизвестное поле '{k}', не разрешенное JSON-схемой")


def verify_run_dir_metadata(
    meta: Dict[str, Any],
    run_dir: Path,
    artifact_name: str,
    errors: List[str],
) -> None:
    parts = run_dir.resolve().parts
    if len(parts) >= 2:
        dir_timestamp = parts[-1]
        dir_page_id = parts[-2]
        actual_page_id = str(meta.get("confluence_page_id", ""))
        actual_timestamp = str(meta.get("run_timestamp", ""))

        if re.match(r"^[0-9]+$", dir_page_id) and actual_page_id != dir_page_id:
            errors.append(
                f"{artifact_name}.metadata.confluence_page_id ('{actual_page_id}') "
                f"не совпадает с директорией запуска ('{dir_page_id}')"
            )
        if (
            re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}$", dir_timestamp)
            and actual_timestamp != dir_timestamp
        ):
            errors.append(
                f"{artifact_name}.metadata.run_timestamp ('{actual_timestamp}') "
                f"не совпадает с директорией запуска ('{dir_timestamp}')"
            )


def check_unique_ids(
    items: List[Dict[str, Any]],
    key: str,
    section_name: str,
    errors: List[str],
) -> Set[str]:
    seen: Set[str] = set()
    for idx, item in enumerate(items):
        val = item.get(key)
        if not val:
            continue
        if val in seen:
            errors.append(f"{section_name}[{idx}]: дублирующийся идентификатор {key}='{val}'")
        seen.add(val)
    return seen


def validate_qg1(run_dir: Path, errors: List[str]) -> Dict[str, Any]:
    artifact_path = run_dir / "requirement-review-artifact.json"
    schema_path = SCHEMAS_DIR / "requirement-review.schema.json"

    data, err = load_json(artifact_path)
    if err:
        errors.append(err)
        return {}
    schema, err = load_json(schema_path)
    if err:
        errors.append(err)
        return {}

    validate_against_schema(data, schema, "requirement-review", errors)
    check_data_leakage(data, "requirement-review", errors)
    if errors:
        return data

    verify_run_dir_metadata(data["metadata"], run_dir, "requirement-review", errors)

    req_ids = check_unique_ids(data["requirements"], "id", "requirements", errors)
    check_unique_ids(data["business_rules"], "id", "business_rules", errors)
    check_unique_ids(data["constraints"], "id", "constraints", errors)
    check_unique_ids(data["acceptance_criteria"], "id", "acceptance_criteria", errors)
    check_unique_ids(data["risks"], "id", "risks", errors)

    defects = data["defects"]
    amb_ids = check_unique_ids(defects["ambiguities"], "id", "defects.ambiguities", errors)
    cnt_ids = check_unique_ids(defects["contradictions"], "id", "defects.contradictions", errors)
    mis_ids = check_unique_ids(defects["missing_info"], "id", "defects.missing_info", errors)
    all_defect_ids = amb_ids | cnt_ids | mis_ids

    blocking_defects_by_req: Dict[str, Set[str]] = {r_id: set() for r_id in req_ids}
    for cat_name in ("ambiguities", "contradictions", "missing_info"):
        for d in defects[cat_name]:
            d_id = d["id"]
            has_question = bool(str(d.get("question_to_owner", "")).strip())
            is_blocker = d.get("severity") == "Blocker"
            if has_question and not is_blocker:
                errors.append(
                    f"defects.{cat_name} ({d_id}): дефект содержит question_to_owner, "
                    f"поэтому обязан иметь severity='Blocker' (текущий: '{d.get('severity')}')"
                )
            for aff_req in d.get("affected_req_ids", []):
                if aff_req not in req_ids:
                    errors.append(
                        f"defects.{cat_name} ({d_id}): ссылка на несуществующее требование '{aff_req}'"
                    )
                elif is_blocker or has_question:
                    blocking_defects_by_req[aff_req].add(d_id)

    for req in data["requirements"]:
        r_id = req["id"]
        status = req["status"]
        refs = set(req.get("blocker_refs", []))
        expected_blockers = blocking_defects_by_req.get(r_id, set())

        for ref in refs:
            if ref not in all_defect_ids:
                errors.append(
                    f"requirements ({r_id}): blocker_refs ссылается на несуществующий дефект '{ref}'"
                )

        if status == "blocked" and not refs:
            errors.append(f"requirements ({r_id}): статус 'blocked', но массив blocker_refs пуст")
        if status == "confirmed" and refs:
            errors.append(
                f"requirements ({r_id}): статус 'confirmed', но blocker_refs не пуст ({sorted(refs)})"
            )
        if expected_blockers and status != "blocked":
            errors.append(
                f"requirements ({r_id}): затронуто блокерами/вопросами к владельцу {sorted(expected_blockers)}, "
                f"поэтому статус обязан быть 'blocked'"
            )
        if expected_blockers and not expected_blockers.issubset(refs):
            missing_refs = sorted(expected_blockers - refs)
            errors.append(
                f"requirements ({r_id}): в blocker_refs не указаны блокирующие дефекты {missing_refs}"
            )

    for sec_name in ("business_rules", "constraints", "acceptance_criteria", "risks"):
        for item in data[sec_name]:
            for l_req in item.get("linked_req_ids", []):
                if l_req not in req_ids:
                    errors.append(
                        f"{sec_name} ({item['id']}): linked_req_ids ссылается на неизвестный '{l_req}'"
                    )

    for idx, dep in enumerate(data["dependencies"]):
        if dep["source_req_id"] not in req_ids:
            errors.append(
                f"dependencies[{idx}]: source_req_id ссылается на неизвестный '{dep['source_req_id']}'"
            )

    return data


def validate_qg2(
    run_dir: Path,
    is_export: bool,
    errors: List[str],
    allow_exported: bool = False,
) -> Dict[str, Any]:
    req_data = validate_qg1(run_dir, errors)
    if errors:
        return {}

    artifact_path = run_dir / "jira-tasks-artifact.json"
    schema_path = SCHEMAS_DIR / "jira-tasks.schema.json"

    data, err = load_json(artifact_path)
    if err:
        errors.append(err)
        return {}
    schema, err = load_json(schema_path)
    if err:
        errors.append(err)
        return {}

    validate_against_schema(data, schema, "jira-tasks", errors)
    check_data_leakage(data, "jira-tasks", errors)
    if errors:
        return data

    for key in ("confluence_page_id", "run_timestamp"):
        if data["metadata"].get(key) != req_data["metadata"].get(key):
            errors.append(
                f"jira-tasks.metadata.{key} ('{data['metadata'].get(key)}') не совпадает с "
                f"requirement-review.metadata.{key} ('{req_data['metadata'].get(key)}')"
            )

    confirmed_reqs = {r["id"] for r in req_data["requirements"] if r["status"] == "confirmed"}
    blocked_reqs_map = {
        r["id"]: set(r.get("blocker_refs", []))
        for r in req_data["requirements"]
        if r["status"] == "blocked"
    }
    blocked_reqs = set(blocked_reqs_map.keys())

    task_ids = check_unique_ids(data["tasks"], "local_id", "tasks", errors)
    seen_summaries: Set[str] = set()
    linked_in_tasks: Set[str] = set()
    project_key = data["metadata"]["jira_project_key"]

    for task in data["tasks"]:
        t_id = task["local_id"]
        norm_sum = task["summary"].strip().lower()
        if norm_sum in seen_summaries:
            errors.append(f"tasks ({t_id}): дублирующийся summary '{task['summary']}'")
        seen_summaries.add(norm_sum)

        for dep in task.get("dependencies", []):
            if dep == t_id or dep not in task_ids:
                errors.append(f"tasks ({t_id}): некорректная зависимость '{dep}'")

        for r_id in task["linked_requirement_ids"]:
            if r_id in blocked_reqs:
                errors.append(
                    f"tasks ({t_id}): запрещено привязывать заблокированное требование '{r_id}' к задаче Jira"
                )
            elif r_id not in confirmed_reqs:
                errors.append(f"tasks ({t_id}): ссылка на несуществующее требование '{r_id}'")
            else:
                linked_in_tasks.add(r_id)

        status = task["export_status"]
        issue_key = task["jira_issue_key"]

        if is_export:
            if status not in ("exported", "existing"):
                errors.append(
                    f"tasks ({t_id}): на этапе Export Check статус обязан быть 'exported' или 'existing' (текущий: '{status}')"
                )
            if not issue_key or not issue_key.startswith(f"{project_key}-"):
                errors.append(
                    f"tasks ({t_id}): отсутствует валидный jira_issue_key проекта '{project_key}' (текущий: '{issue_key}')"
                )
        elif allow_exported:
            if status not in ("pending", "existing", "exported"):
                errors.append(
                    f"tasks ({t_id}): недопустимый статус '{status}'"
                )
            if status in ("existing", "exported") and not issue_key:
                errors.append(
                    f"tasks ({t_id}): при export_status='{status}' поле jira_issue_key обязательно"
                )
        else:
            if status not in ("pending", "existing"):
                errors.append(
                    f"tasks ({t_id}): на этапе Design Check статус должен быть 'pending' или 'existing'"
                )
            if status == "existing" and not issue_key:
                errors.append(
                    f"tasks ({t_id}): при export_status='existing' поле jira_issue_key обязательно"
                )
            if status == "pending" and issue_key is not None:
                errors.append(
                    f"tasks ({t_id}): при export_status='pending' поле jira_issue_key должно быть null"
                )

    cov = data["coverage_summary"]
    covered_set = set(cov.get("covered_requirement_ids", []))
    skipped_items = cov.get("skipped_requirements", [])
    skipped_set = {item["req_id"] for item in skipped_items}

    if covered_set != confirmed_reqs:
        errors.append(
            f"coverage_summary.covered_requirement_ids ({sorted(covered_set)}) не совпадает с "
            f"множеством confirmed требований ({sorted(confirmed_reqs)})"
        )
    if linked_in_tasks != confirmed_reqs:
        missing_in_tasks = sorted(confirmed_reqs - linked_in_tasks)
        errors.append(
            f"Не все confirmed требования покрыты задачами в tasks: пропущены {missing_in_tasks}"
        )
    if skipped_set != blocked_reqs:
        errors.append(
            f"coverage_summary.skipped_requirements ({sorted(skipped_set)}) не совпадает с "
            f"множеством blocked требований ({sorted(blocked_reqs)})"
        )
    for item in skipped_items:
        s_req_id = item["req_id"]
        if s_req_id in blocked_reqs_map:
            expected_refs = blocked_reqs_map[s_req_id]
            actual_refs = set(item.get("blocker_refs", []))
            if actual_refs != expected_refs:
                errors.append(
                    f"skipped_requirements ({s_req_id}): blocker_refs {sorted(actual_refs)} "
                    f"не совпадает с requirement-review {sorted(expected_refs)}"
                )

    return data


def validate_qg3(run_dir: Path, is_export: bool, errors: List[str]) -> Dict[str, Any]:
    req_data = validate_qg1(run_dir, errors)
    tasks_data = validate_qg2(
        run_dir,
        is_export=False,
        errors=errors,
        allow_exported=is_export,
    )
    if errors:
        return {}

    artifact_path = run_dir / "qase-test-model-artifact.json"
    schema_path = SCHEMAS_DIR / "qase-test-model.schema.json"

    data, err = load_json(artifact_path)
    if err:
        errors.append(err)
        return {}
    schema, err = load_json(schema_path)
    if err:
        errors.append(err)
        return {}

    validate_against_schema(data, schema, "qase-test-model", errors)
    check_data_leakage(data, "qase-test-model", errors)
    if errors:
        return data

    for key in ("confluence_page_id", "run_timestamp"):
        if data["metadata"].get(key) != req_data["metadata"].get(key):
            errors.append(
                f"qase-test-model.metadata.{key} ('{data['metadata'].get(key)}') не совпадает с "
                f"requirement-review.metadata.{key} ('{req_data['metadata'].get(key)}')"
            )

    confirmed_reqs = {r["id"] for r in req_data["requirements"] if r["status"] == "confirmed"}
    blocked_reqs = {r["id"] for r in req_data["requirements"] if r["status"] == "blocked"}
    confirmed_brs = {
        br["id"]
        for br in req_data.get("business_rules", [])
        if any(r_id in confirmed_reqs for r_id in br.get("linked_req_ids", []))
    }
    confirmed_acs = {
        ac["id"]
        for ac in req_data.get("acceptance_criteria", [])
        if any(r_id in confirmed_reqs for r_id in ac.get("linked_req_ids", []))
    }
    allowed_trace_ids = confirmed_reqs | confirmed_brs | confirmed_acs
    valid_task_ids = {t["local_id"] for t in tasks_data.get("tasks", [])}

    suite_ids = check_unique_ids(data["suites"], "suite_id", "suites", errors)
    for s in data["suites"]:
        p_id = s.get("parent_suite_id")
        if p_id is not None and p_id not in suite_ids:
            errors.append(
                f"suites ({s['suite_id']}): parent_suite_id ссылается на неизвестный '{p_id}'"
            )
        if is_export and s.get("qase_suite_id") is None:
            errors.append(
                f"suites ({s['suite_id']}): на этапе Export Check поле qase_suite_id не может быть null"
            )

    tc_ids = check_unique_ids(data["test_cases"], "local_id", "test_cases", errors)
    types_present: Set[str] = set()
    reqs_covered_by_tcs: Set[str] = set()

    for tc in data["test_cases"]:
        tc_id = tc["local_id"]
        types_present.add(tc["type"])
        if tc["suite_id"] not in suite_ids:
            errors.append(f"test_cases ({tc_id}): suite_id '{tc['suite_id']}' не существует в suites")

        for r_id in tc["linked_requirement_ids"]:
            if r_id in blocked_reqs:
                errors.append(
                    f"test_cases ({tc_id}): запрещено создавать тест-кейсы для blocked требования '{r_id}'"
                )
            elif r_id not in allowed_trace_ids:
                errors.append(
                    f"test_cases ({tc_id}): ссылка на неизвестный или заблокированный элемент '{r_id}'"
                )
            elif r_id in confirmed_reqs:
                reqs_covered_by_tcs.add(r_id)

        for t_id in tc["linked_task_ids"]:
            if t_id not in valid_task_ids:
                errors.append(
                    f"test_cases ({tc_id}): linked_task_ids ссылается на неизвестную задачу '{t_id}' "
                    f"(отсутствует в jira-tasks-artifact.json)"
                )

        if not is_export:
            if tc["export_status"] != "pending":
                errors.append(
                    f"test_cases ({tc_id}): на этапе Design Check статус должен быть 'pending'"
                )
        else:
            if tc["export_status"] != "exported" or tc.get("qase_id") is None:
                errors.append(
                    f"test_cases ({tc_id}): на этапе Export Check должен быть export_status='exported' и непустой qase_id"
                )

    uncovered_set = {u["req_id"] for u in data.get("uncovered_requirements", [])}
    if not blocked_reqs.issubset(uncovered_set):
        missing_blocked = sorted(blocked_reqs - uncovered_set)
        errors.append(
            f"В uncovered_requirements отсутствуют заблокированные требования: {missing_blocked}"
        )

    expected_covered_reqs = confirmed_reqs - uncovered_set
    if expected_covered_reqs:
        missing_types = {"positive", "negative", "boundary"} - types_present
        if missing_types:
            errors.append(
                f"В тестовой модели отсутствуют обязательные типы сценариев: {sorted(missing_types)}"
            )

    cov_matrix = data.get("coverage_matrix", {})
    for req_key, mapped_tcs in cov_matrix.items():
        if req_key in blocked_reqs:
            errors.append(
                f"coverage_matrix: заблокированное требование '{req_key}' не должно быть в матрице покрытия"
            )
        elif req_key not in allowed_trace_ids:
            errors.append(f"coverage_matrix: неизвестный идентификатор '{req_key}'")
        for mapped_tc in mapped_tcs:
            if mapped_tc not in tc_ids:
                errors.append(
                    f"coverage_matrix['{req_key}']: ссылка на несуществующий тест-кейс '{mapped_tc}'"
                )

    matrix_req_keys = {k for k in cov_matrix.keys() if k.startswith("REQ-")}
    if matrix_req_keys != expected_covered_reqs or reqs_covered_by_tcs != expected_covered_reqs:
        missing = sorted(expected_covered_reqs - (matrix_req_keys & reqs_covered_by_tcs))
        errors.append(
            f"Не все подтвержденные требования покрыты в test_cases и coverage_matrix: {missing}"
        )

    return data


def compute_pipeline_status(run_dir: Path) -> str:
    req_path = run_dir / "requirement-review-artifact.json"
    data, err = load_json(req_path)
    if err or not isinstance(data, dict):
        return "FAILED"
    reqs = data.get("requirements", [])
    if not reqs:
        return "FAILED"
    confirmed = [r for r in reqs if r.get("status") == "confirmed"]
    blocked = [r for r in reqs if r.get("status") == "blocked"]
    if not confirmed:
        return "FAILED"

    qase_path = run_dir / "qase-test-model-artifact.json"
    if qase_path.exists():
        qase_data, q_err = load_json(qase_path)
        if not q_err and isinstance(qase_data, dict):
            if qase_data.get("uncovered_requirements"):
                return "PARTIAL"

    if blocked:
        return "PARTIAL"
    return "COMPLETED"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate OpenCode QA JSON artifacts.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=["qg1", "qg2-design", "qg2-export", "qg3-design", "qg3-export"],
        help="Quality Gate stage to validate",
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to the artifact run directory",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    errors: List[str] = []

    if args.stage == "qg1":
        validate_qg1(run_dir, errors)
    elif args.stage == "qg2-design":
        validate_qg2(run_dir, is_export=False, errors=errors)
    elif args.stage == "qg2-export":
        validate_qg2(run_dir, is_export=True, errors=errors)
    elif args.stage == "qg3-design":
        validate_qg3(run_dir, is_export=False, errors=errors)
    elif args.stage == "qg3-export":
        validate_qg3(run_dir, is_export=True, errors=errors)

    status_rec = compute_pipeline_status(run_dir)
    result = {
        "valid": len(errors) == 0,
        "stage": args.stage,
        "run_dir": run_dir.as_posix(),
        "recommended_pipeline_status": status_rec if not errors else "FAILED",
        "error_count": len(errors),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
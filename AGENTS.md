# Project Overview
Мультиагентная QA-система для сквозного анализа требований, проектирования задач на разработку и создания тестовой модели с синхронизацией во внешние системы (Confluence, Jira, Qase).

Все результаты базируются исключительно на подтвержденных требованиях (Source of Truth — Confluence). Отсутствующая информация, предположения и гипотезы никогда не выдаются за подтвержденные требования.

# Multi-Agent Architecture
1. `orchestrator-agent` (`mode: primary`) — координация сквозного пайплайна, инициализация директории запуска `run_dir`, проверка Quality Gates (QG-1..QG-4) и консистентности `metadata`, управление циклами Self-Refine, разрешение на экспорт во внешние системы, эскалация и сборка `final-qa-report.md`.
2. `requirement-review-agent` (`mode: subagent`) — извлечение требований из Confluence и формирование `requirement-review-artifact.json` через skill `requirement-review`.
3. `task-design-agent` (`mode: subagent`) — проверка дубликатов в Jira, проектирование задач в `jira-tasks-artifact.json` через skill `task-design` и их экспорт в Jira.
4. `test-design-agent` (`mode: subagent`) — проектирование тестовой модели в `qase-test-model-artifact.json` через skill `test-design` и её экспорт в Qase.

# Shared Artifact Directory & Contracts (State Isolation)
Обмен данными между агентами происходит исключительно через файлы артефактов в `FileSystem / ArtifactStore`.

Все артефакты каждого запуска сохраняются в двухуровневую иерархию директорий:
`./artifacts/<confluence_page_id>/<run_timestamp>/` (далее — `<run_dir>`), где:
- `<confluence_page_id>` — числовой ID страницы требований в Confluence;
- `<run_timestamp>` — дата и время старта конкретного запуска в формате `YYYY-MM-DD_HH-mm`, фиксируемые оркестратором один раз при старте пайплайна.

Контракты файлов внутри `<run_dir>` (строгие JSON-схемы определены в соответствующих Skills):
- Каждый JSON-артефакт обязан содержать корневой блок `metadata` (`confluence_page_id`, `run_timestamp`, а также `jira_project_key` / `qase_project_code`), значения которого должны строго совпадать между артефактами и соответствовать имени директории `<run_dir>`.

1. `<run_dir>/requirement-review-artifact.json` — схема определяется в skill `requirement-review`. Содержит `metadata` (`confluence_page_id`, `run_timestamp`), `feature_scope`, `requirements` (`confirmed` | `blocked`, `blocker_refs`), `business_rules`, `constraints`, `acceptance_criteria`, `dependencies`, `defects` (`ambiguities`, `contradictions`, `missing_info` с привязкой `affected_req_ids` и `question_to_owner`) и `risks`.
2. `<run_dir>/jira-tasks-artifact.json` — схема определяется в skill `task-design`. Содержит `metadata` (`confluence_page_id`, `run_timestamp`, `jira_project_key`), `tasks` (со статусом `export_status`: `pending` | `exported` | `existing` | `failed` и `jira_issue_key`) и `coverage_summary` (`covered_requirement_ids`, `skipped_requirements`).
3. `<run_dir>/qase-test-model-artifact.json` — схема определяется в skill `test-design`. Содержит `metadata` (`confluence_page_id`, `run_timestamp`, `qase_project_code`), `suites`, `test_cases` (с привязкой `linked_requirement_ids`, `linked_task_ids`, статусом `export_status` и `qase_id`), `coverage_matrix` и `uncovered_requirements`.
4. `<run_dir>/final-qa-report.md` — формируется `orchestrator-agent` для пользователя. Содержит метаданные запуска, сводку дефектов требований и рисков, сквозную матрицу трассируемости (`Req ID` ↔ `Jira Key` ↔ `Qase Case IDs`), список открытых вопросов (`question_to_owner`) и итоговый статус выполнения.

# Global Constraints (Жесткие ограничения для всех агентов)
- Использовать ТОЛЬКО информацию из доступных подтвержденных требований.
- Не придумывать требования, бизнес-правила, ограничения, критерии приемки или ожидаемые результаты.
- Не выдавать предположения и выводы агентов за подтвержденные требования.
- Не изменять исходные данные в Confluence (доступ строго Read-only).
- Не проектировать и не экспортировать задачи/тест-кейсы для требований со статусом `blocked`.
- Не экспортировать во внешние системы (Jira, Qase) элементы со статусом `pending`, пока артефакт не прошел валидацию на соответствующем Quality Gate (Design Check).
- Не удалять и не перезаписывать существующие задачи в Jira (`existing` / `exported`) и сценарии в Qase без явного указания в Task Prompt.
- Не сообщать об успешном экспорте во внешнюю систему, если Tool фактически не вернул реальный `jira_issue_key` или `qase_id`.
- Не скрывать выявленные противоречия, риски и пробелы в требованиях.
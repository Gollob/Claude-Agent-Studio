#!/usr/bin/env python3
"""
validate.py — валидатор tagcatalog/tags.yaml
Проверяет:
  1. JSON-Schema (типы, обязательные поля, enum-значения)
  2. Дубли канонов тегов
  3. Дубли алиасов (в том числе пересечение alias == другой canon)
  4. Все теги в skills[] существуют в tags[] (нет тегов вне реестра)
  5. Все agents в skills[] существуют в agents[]
  6. Дубли имён навыков (skill)
  7. Дубли имён агентов (agent)

Зависимости: python3 + jsonschema + pyyaml (оба есть на agent-vm).
Выход: 0 = OK, 1 = ошибки найдены.
Использование: python3 validate.py [path/to/tags.yaml]
"""
import json
import sys
import os

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml не установлен. Установите: pip install pyyaml")
    sys.exit(1)

try:
    import jsonschema
    from jsonschema import validate as js_validate, ValidationError, Draft202012Validator
except ImportError:
    print("ERROR: jsonschema не установлен. Установите: pip install jsonschema")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def load_schema(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def validate(yaml_path: str | None = None) -> bool:
    """
    Запускает все проверки. Возвращает True если всё OK, False при ошибках.
    Печатает найденные ошибки в stdout.
    """
    if yaml_path is None:
        yaml_path = os.path.join(SCRIPT_DIR, "tags.yaml")
    schema_path = os.path.join(SCRIPT_DIR, "tags.schema.json")

    errors: list[str] = []
    ok_checks: list[str] = []

    # ── 1. Загрузка ──────────────────────────────────────────────────────────
    try:
        catalog = load_yaml(yaml_path)
    except Exception as e:
        print(f"FATAL: не удалось загрузить {yaml_path}: {e}")
        return False

    try:
        schema = load_schema(schema_path)
    except Exception as e:
        print(f"FATAL: не удалось загрузить схему {schema_path}: {e}")
        return False

    # ── 2. JSON-Schema ────────────────────────────────────────────────────────
    validator = Draft202012Validator(schema)
    schema_errors = list(validator.iter_errors(catalog))
    if schema_errors:
        for err in schema_errors:
            path_str = " -> ".join(str(p) for p in err.absolute_path) or "(root)"
            errors.append(f"[schema] {path_str}: {err.message}")
    else:
        ok_checks.append("JSON-Schema: OK")

    # Если структура не прошла схему — дальше проверять смысла нет
    if errors:
        _print_result(errors, ok_checks)
        return False

    tags_list: list[dict] = catalog.get("tags", [])
    skills_list: list[dict] = catalog.get("skills", [])
    agents_list: list[dict] = catalog.get("agents", [])

    # ── 3. Дубли канонов ─────────────────────────────────────────────────────
    canon_set: set[str] = set()
    dup_canons: list[str] = []
    for t in tags_list:
        c = t["canon"]
        if c in canon_set:
            dup_canons.append(c)
        canon_set.add(c)
    if dup_canons:
        errors.append(f"[tags] Дубли канонов: {dup_canons}")
    else:
        ok_checks.append("Дубли канонов: нет")

    # ── 4. Дубли алиасов (+ пересечение alias == canon другого тега) ─────────
    alias_to_canon: dict[str, str] = {}
    dup_aliases: list[str] = []
    for t in tags_list:
        c = t["canon"]
        # Сам канон тоже добавляем в alias-map (нормализация canon→canon)
        if c in alias_to_canon and alias_to_canon[c] != c:
            dup_aliases.append(f"canon '{c}' конфликтует с alias '{c}'→'{alias_to_canon[c]}'")
        alias_to_canon[c] = c

        for alias in t.get("aliases", []):
            a = alias.lower()
            if a in alias_to_canon:
                dup_aliases.append(
                    f"alias '{alias}' тега '{c}' уже занят (→ '{alias_to_canon[a]}')"
                )
            else:
                alias_to_canon[a] = c
    if dup_aliases:
        errors.append(f"[tags] Дубли/конфликты алиасов: {dup_aliases}")
    else:
        ok_checks.append("Дубли алиасов: нет")

    # ── 5. Дубли имён агентов ────────────────────────────────────────────────
    agent_set: set[str] = set()
    dup_agents: list[str] = []
    for a in agents_list:
        name = a["agent"]
        if name in agent_set:
            dup_agents.append(name)
        agent_set.add(name)
    if dup_agents:
        errors.append(f"[agents] Дубли агентов: {dup_agents}")
    else:
        ok_checks.append("Дубли агентов: нет")

    # ── 6. Дубли имён навыков ────────────────────────────────────────────────
    skill_set: set[str] = set()
    dup_skills: list[str] = []
    for s in skills_list:
        name = s["skill"]
        if name in skill_set:
            dup_skills.append(name)
        skill_set.add(name)
    if dup_skills:
        errors.append(f"[skills] Дубли навыков: {dup_skills}")
    else:
        ok_checks.append("Дубли навыков: нет")

    # ── 7. Все теги навыков существуют в реестре тегов ───────────────────────
    unknown_tags: list[str] = []
    for s in skills_list:
        for tag in s.get("tags", []):
            if tag not in canon_set:
                unknown_tags.append(
                    f"skill '{s['skill']}': тег '{tag}' не найден в tags[]"
                )
    if unknown_tags:
        errors.append(f"[skills→tags] Теги вне реестра:\n  " + "\n  ".join(unknown_tags))
    else:
        ok_checks.append("Все теги навыков существуют в реестре: OK")

    # ── 8. Все агенты навыков существуют в agents[] ──────────────────────────
    unknown_agents: list[str] = []
    for s in skills_list:
        ag = s.get("agent")
        if ag not in agent_set:
            unknown_agents.append(
                f"skill '{s['skill']}': агент '{ag}' не найден в agents[]"
            )
    if unknown_agents:
        errors.append(f"[skills→agents] Агенты вне реестра:\n  " + "\n  ".join(unknown_agents))
    else:
        ok_checks.append("Все агенты навыков существуют в реестре: OK")

    # ── Итог ─────────────────────────────────────────────────────────────────
    _print_result(errors, ok_checks)
    return len(errors) == 0


def _print_result(errors: list[str], ok_checks: list[str]) -> None:
    print("=" * 60)
    print("validate.py — TagCatalog")
    print("=" * 60)
    for msg in ok_checks:
        print(f"  OK  {msg}")
    if errors:
        print()
        for msg in errors:
            print(f"  ERR {msg}")
        print()
        print(f"RESULT: FAIL ({len(errors)} ошибок)")
    else:
        print()
        print("RESULT: OK — реестр валиден")
    print("=" * 60)


if __name__ == "__main__":
    yaml_path = sys.argv[1] if len(sys.argv) > 1 else None
    ok = validate(yaml_path)
    sys.exit(0 if ok else 1)

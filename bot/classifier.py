#!/usr/bin/env python3
"""classifier.py — shell-command safety classifier for approval-hook.sh.

Usage (CLI):
    echo '<command>' | python3 classifier.py
    # prints JSON {"verdict":"allow"|"ask"|"deny","category":str,"reason":str}
    # exit codes: 0=allow, 10=ask, 2=deny

Usage (module):
    from classifier import classify
    result = classify("ls -la /tmp")
    # result["verdict"] in {"allow","ask","deny"}

Algorithm (deny-first, ADR-004):
  1. Segment by ; && || | & \\n (respecting quotes via shlex); parse error → ask.
  2. Bypass/opacity detection across all segments: eval, bash -c <opaque>,
     base64 -d|sh, curl|wget|...|sh/bash, write-then-exec, command substitution
     $(...)/`...`, process substitution <(...)/>(...)  → ask (not overridable).
  3. Per-segment denylist check (command name + flags) → deny (hard) or ask (soft).
  4. Per-segment allowlist check (read-only commands) → safe.
  5. Fold: verdict = max(deny > ask > allow) across segments.
  6. Unknown command (not in allow, not in deny) → UNKNOWN_POLICY (default: ask).

To add/change rules: edit ALLOW_COMMANDS, DENY_RULES, BYPASS_PATTERNS below.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Configuration / policy constant
# ---------------------------------------------------------------------------

UNKNOWN_POLICY: str = os.environ.get("UNKNOWN_POLICY", "ask")
"""Verdict for commands not found in allowlist or denylist. Default: "ask"."""

# ---------------------------------------------------------------------------
# DATA: Allowlist — read-only commands (command name only, not full path)
# ---------------------------------------------------------------------------

# Set of command names that are considered read-only / safe by default.
# A segment whose resolved command name is in this set is marked "safe",
# UNLESS it has dangerous flags or a pipe-to-shell (checked separately).
ALLOW_COMMANDS: frozenset[str] = frozenset(
    [
        # filesystem inspection
        "ls",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "rg",
        "egrep",
        "fgrep",
        "find",
        "stat",
        "file",
        "wc",
        "sort",
        "uniq",
        "cut",
        "awk",
        "sed",
        "pwd",
        "echo",
        "printf",
        "which",
        "type",
        "env",
        "tree",
        "readlink",
        "realpath",
        "dirname",
        "basename",
        "xxd",
        "od",
        # process / system info
        "ps",
        "pgrep",
        "top",
        "htop",
        "df",
        "du",
        "free",
        "uname",
        "whoami",
        "id",
        "date",
        "uptime",
        "hostname",
        "lsof",
        # NOTE: strace/ltrace/nohup/nice/ionice/timeout/time/watch/stdbuf/setsid/chrt/taskset/flock/script
        # are intentionally NOT in the allowlist — they are wrapper commands that execute
        # arbitrary subcommands, so the wrapped command must be classified independently.
        # data tools
        "jq",
        "yq",
        # git read-only
        "git",
        # docker read-only (subcommands checked below)
        "docker",
        # systemctl read-only (subcommands checked below)
        "systemctl",
        # journalctl
        "journalctl",
        # network inspection
        "curl",
        "wget",
        "iptables",
        "nft",
        "ip",
        "ss",
        "netstat",
        "ping",
        "traceroute",
        "dig",
        "nslookup",
        "host",
        # kubernetes read-only (subcommands checked below)
        "kubectl",
        # text utils
        # NOTE: tee is intentionally NOT in the allowlist — it writes to files, so its
        # target path must be checked. It is handled in the denylist via write_system_path
        # and by treating it as an unknown command (ask) in general.
        # NOTE: xargs is intentionally NOT in the allowlist — it executes an arbitrary
        # subcommand, which must be classified independently.
        "tr",
        "diff",
        "patch",
        "column",
        "nl",
        # crontab read-only (-l)
        "crontab",
        # misc safe
        "true",
        "false",
        "test",
        "[",
        "[[",
        # python testing
        "pytest",
        "python3",
        "python",
        # shell script runner (restricted in _check_segment_allow)
        "bash",
        "sh",
        # npx (restricted in _check_segment_allow — openspec only)
        "npx",
    ]
)

# ---------------------------------------------------------------------------
# DATA: Allowlist subcommand restrictions
# ---------------------------------------------------------------------------
# For commands whose safety depends on the subcommand/flags, define explicit
# SAFE sub-patterns and UNSAFE sub-patterns.

# git: subcommand-aware classification
# SAFE→allow: read-only and common write operations that don't rewrite history
GIT_SAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    [
        # read-only
        "status", "diff", "log", "show", "branch", "remote", "rev-parse", "describe",
        "shortlog", "ls-files", "ls-remote", "check-ignore", "tag",
        # common write ops (safe at command level, flag-level unsafe filtered separately)
        "add", "commit", "pull", "fetch", "push", "checkout", "switch", "restore",
        "merge", "stash", "clone", "init", "config", "reset",
    ]
)
# NOTE: "stash" is now in GIT_SAFE_SUBCOMMANDS; flag-level checks handle stash pop/drop/clear.
# NOTE: push/reset/clean/branch-delete/tag-delete/rebase/filter-branch get flag-level checks.
GIT_UNSAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    ["cherry-pick", "mv",
     "submodule", "bisect", "worktree",
     "filter-branch", "update-ref"]
)

# git flag-level unsafe patterns (checked per subcommand in _check_git_flag_unsafe)
# These make an otherwise-safe subcommand require "ask".
# Keys are subcommand names; values are sets of flag strings / patterns.
_GIT_FORCE_PUSH_FLAGS: frozenset[str] = frozenset(["-f", "--force", "--force-with-lease"])
_GIT_RESET_HARD_FLAG: str = "--hard"
_GIT_CLEAN_UNSAFE_FLAGS: frozenset[str] = frozenset(["-f", "-d", "-x", "-X",
                                                       "-fd", "-fx", "-fX", "-df", "-xf", "-Xf"])
_GIT_BRANCH_DELETE_FLAGS: frozenset[str] = frozenset(["-d", "-D"])
_GIT_TAG_DELETE_FLAG: str = "-d"
_GIT_CONFIG_UNSAFE_FLAGS: frozenset[str] = frozenset(["--unset", "--global", "--system",
                                                        "--edit", "--unset-all", "--remove-section"])

# systemctl: only read-only subcommands are safe
SYSTEMCTL_SAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    ["status", "is-active", "is-enabled", "is-failed", "list-units",
     "list-unit-files", "list-sockets", "list-timers", "show", "cat",
     "list-dependencies", "get-default"]
)
SYSTEMCTL_UNSAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    ["start", "stop", "restart", "reload", "enable", "disable",
     "mask", "unmask", "kill", "reset-failed", "set-default",
     "daemon-reload", "daemon-reexec", "poweroff", "reboot", "halt",
     "suspend", "hibernate"]
)

# docker: only read-only subcommands are safe
DOCKER_SAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    ["ps", "images", "logs", "inspect", "stats", "top", "port",
     "events", "info", "version", "search", "diff", "history",
     "network", "volume"]
)
# docker network/volume read-only sub-sub-commands
DOCKER_NETWORK_SAFE: frozenset[str] = frozenset(["ls", "list", "inspect"])
DOCKER_VOLUME_SAFE: frozenset[str] = frozenset(["ls", "list", "inspect"])

DOCKER_UNSAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    ["rm", "rmi", "stop", "kill", "start", "restart", "pause",
     "unpause", "rename", "create", "run", "exec", "cp", "pull",
     "push", "build", "tag", "save", "load", "import", "export",
     "login", "logout", "update", "system", "container", "image",
     "compose", "stack", "swarm", "node", "service", "secret",
     "config", "plugin", "trust", "manifest", "context", "scan",
     "sbom", "scout", "buildx", "prune"]
)

# kubectl: read-only subcommands only
KUBECTL_SAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    ["get", "describe", "logs", "version", "config", "explain", "top",
     "api-resources", "api-versions", "cluster-info", "auth"]
)
KUBECTL_UNSAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    ["delete", "apply", "create", "exec", "edit", "replace", "scale",
     "patch", "drain", "cordon", "uncordon", "taint", "label",
     "annotate", "rollout", "set", "run", "expose", "autoscale",
     "attach", "cp", "port-forward", "proxy", "certificate",
     "certificate approve", "certificate deny"]
)

# iptables: -L/-S/-n/-v are read-only; modification flags are unsafe
IPTABLES_READ_FLAGS: frozenset[str] = frozenset(["-L", "-S", "-n", "-v", "--list", "--list-rules"])
IPTABLES_WRITE_FLAGS: frozenset[str] = frozenset(
    ["-A", "-D", "-F", "-I", "-R", "-P", "-N", "-X", "-E", "-Z",
     "--append", "--delete", "--flush", "--insert", "--replace",
     "--policy", "--new-chain", "--delete-chain", "--rename-chain",
     "--zero"]
)

# curl/wget: safe only when not piping to shell (checked in bypass detection)
# and not writing to system paths. Checked in _check_curl_wget().

# System paths for path-based security checks
# Note: used by _is_system_path() — both /root and /home are critical
_SYSTEM_ROOTS: tuple[str, ...] = (
    "/etc",
    "/opt",    # all of /opt
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/sys",
    "/proc",
    "/dev/sd",
    "/dev/nvme",
    "/dev/vd",
    "/dev/xvd",
    "/root",   # root home directory
    "/home",   # all user home directories
)

# ---------------------------------------------------------------------------
# DATA: Denylist rules
# ---------------------------------------------------------------------------

class DenyRule(NamedTuple):
    category: str
    reason: str          # human-readable for approval button
    hard: bool           # True → "deny", False → "ask"
    # Matching callable: (cmd_name: str, tokens: list[str]) -> bool
    # We store these as string keys and resolve in _check_denylist().
    match_key: str


# Each rule identified by a match_key; _match_deny() implements the logic.
DENY_RULES: list[DenyRule] = [
    # --- Hard denies ---
    DenyRule(
        category="Форматирование/разметка диска",
        reason="сотрёт диск или раздел безвозвратно",
        hard=True,
        match_key="disk_format",
    ),
    DenyRule(
        category="Удаление файлов (корень/home)",
        reason="необратимо удалит критические файлы или всю систему",
        hard=True,
        match_key="rm_root",
    ),
    DenyRule(
        category="Удаление пользователей",
        reason="удалит учётную запись и доступ к системе",
        hard=True,
        match_key="userdel",
    ),
    DenyRule(
        category="Удаление контейнеров/томов (prune/down -v)",
        reason="необратимо удалит контейнеры, образы или данные томов",
        hard=True,
        match_key="docker_prune",
    ),
    # --- Soft asks ---
    DenyRule(
        category="Удаление файлов",
        reason="необратимо удалит файлы или каталоги",
        hard=False,
        match_key="rm_recursive",
    ),
    DenyRule(
        category="Удаление файла",
        reason="удалит файл (операция необратима)",
        hard=False,
        match_key="rm_file",
    ),
    DenyRule(
        category="Перезагрузка/выключение",
        reason="прервёт работу VM и оборвёт все сессии",
        hard=False,
        match_key="reboot",
    ),
    DenyRule(
        category="Остановка/отключение сервисов",
        reason="остановит или отключит системный сервис",
        hard=False,
        match_key="systemctl_stop",
    ),
    DenyRule(
        category="Удаление/остановка контейнеров",
        reason="потеря контейнеров или данных Docker",
        hard=False,
        match_key="docker_destructive",
    ),
    DenyRule(
        category="Удаление Docker volume",
        reason="необратимо удалит данные Docker volume",
        hard=False,
        match_key="docker_volume_rm",
    ),
    DenyRule(
        category="Жёсткое убийство процессов",
        reason="принудительно завершит процессы без сохранения данных",
        hard=False,
        match_key="kill_hard",
    ),
    DenyRule(
        category="Очистка cron",
        reason="сотрёт всё расписание задач crontab",
        hard=False,
        match_key="crontab_remove",
    ),
    DenyRule(
        category="Установка crontab (из stdin)",
        reason="заменит расписание crontab произвольным вводом",
        hard=False,
        match_key="crontab_install",
    ),
    DenyRule(
        category="Форс-пуш git",
        reason="перезапишет историю репозитория на remote",
        hard=False,
        match_key="git_force_push",
    ),
    DenyRule(
        category="Опасные флаги git",
        reason="операция git с деструктивными флагами (--hard, -f, rebase, branch -D и т.п.)",
        hard=False,
        match_key="git_flag_unsafe",
    ),
    DenyRule(
        category="Изменение firewall",
        reason="может заблокировать сетевой доступ к машине",
        hard=False,
        match_key="firewall_write",
    ),
    DenyRule(
        category="Рекурсивная смена прав",
        reason="массово изменит права доступа или владельца файлов",
        hard=False,
        match_key="chmod_chown_recursive",
    ),
    DenyRule(
        category="Запись в системные пути",
        reason="запишет или изменит системные файлы",
        hard=False,
        match_key="write_system_path",
    ),
    DenyRule(
        category="Изменение git-репозитория",
        reason="изменит историю, индекс или состояние репозитория",
        hard=False,
        match_key="git_write",
    ),
    DenyRule(
        category="Управление kubectl (опасная операция)",
        reason="изменит ресурсы Kubernetes кластера",
        hard=False,
        match_key="kubectl_write",
    ),
    DenyRule(
        category="Уничтожение БД",
        reason="потеря данных базы данных или файла",
        hard=False,
        match_key="db_destroy",
    ),
]

# ---------------------------------------------------------------------------
# DATA: Bypass patterns (regex on full raw string — pre-shlex check)
# ---------------------------------------------------------------------------

# These patterns detect obfuscation / execution bypass constructs.
# They match against the RAW command string (before segmentation) for speed,
# but we also check per-segment tokens for pipe-to-shell detection.

_BYPASS_FULL_RE: list[tuple[re.Pattern[str], str]] = [
    # eval with any argument
    (re.compile(r"\beval\b"), "eval — скрытое выполнение произвольного кода"),
    # base64 decode piped to shell
    (
        re.compile(r"\bbase64\b.*\|\s*(?:bash|sh|zsh|fish|ksh|dash)\b"),
        "base64 -d | sh — обфускация и запуск скрытого кода",
    ),
    # curl or wget piped to shell
    (
        re.compile(
            r"\b(?:curl|wget)\b.*\|\s*(?:bash|sh|zsh|fish|ksh|dash)\b"
        ),
        "curl/wget | sh — загрузка и немедленное выполнение удалённого скрипта",
    ),
    # bash -c / sh -c with a non-trivial argument (not just a simple word)
    (
        re.compile(
            r"""\b(?:bash|sh|zsh|fish|ksh|dash)\s+(?:-\w+\s+)*-c\s+(?:"[^"]{3,}"|'[^']{3,}'|\$\(|\$\{|`|\S+\|)"""
        ),
        "bash -c '<...>' — непрозрачная подоболочка, невозможно проверить содержимое",
    ),
    # command substitution: $(...) or `...`
    # NOTE: negative lookahead (?!\() excludes arithmetic expansion $(( )),
    # which is not a command substitution and should not trigger this rule.
    (
        re.compile(r"`[^`]+`|\$\((?!\()"),
        "command substitution $(...)/`...` — вложенное выполнение команды, невозможно проверить",
    ),
    # process substitution: <(...) or >(...)
    (
        re.compile(r"[<>]\([^)]+\)"),
        "process substitution <(...)/>(...)  — вложенное выполнение команды, невозможно проверить",
    ),
    # backslash-prefixed command name (e.g. \rm) — bypass alias/function
    (
        re.compile(r"\\(?:rm|reboot|shutdown|halt|poweroff|mkfs|dd|userdel|deluser)\b"),
        r"\cmd — обход алиасов/функций, прямой вызов команды",
    ),
    # env -S / --split-string — executes an opaque string as a command line
    (
        re.compile(r"\benv\b.*?(?:\s-S\b|\s--split-string\b|--split-string=)"),
        "env -S/--split-string — непрозрачное исполнение строки как командной строки",
    ),
]

# Pattern for detecting pipe-to-shell AT SEGMENT BOUNDARY
_PIPE_TO_SHELL_CMD: frozenset[str] = frozenset(
    ["bash", "sh", "zsh", "fish", "ksh", "dash"]
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_system_path(path: str) -> bool:
    """Return True if path looks like a system/critical path."""
    return any(path.startswith(r) for r in _SYSTEM_ROOTS)


def _split_segments(command: str) -> list[str] | None:
    """Split command string on shell operators ; && || | & \\n respecting quotes.

    Returns list of segment strings, or None on parse error.
    Uses a simple state-machine to honour single/double quotes and backslash.

    Operators handled:
      ;     — sequential execution
      &&    — conditional AND
      ||    — conditional OR
      |     — pipe
      &     — background (single &, not &&) — treated as segment separator
      \\n   — newline — treated as segment separator (like ;)
    """
    segments: list[str] = []
    current: list[str] = []
    i = 0
    n = len(command)
    in_single = False
    in_double = False

    while i < n:
        ch = command[i]

        if ch == "\\" and not in_single:
            # consume next char verbatim
            current.append(ch)
            if i + 1 < n:
                i += 1
                current.append(command[i])
            i += 1
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue

        if not in_single and not in_double:
            # Newline is treated like ; (command separator)
            if ch == "\n":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue

            # Check for ;
            if ch == ";":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue

            # Check for && or ||
            if ch in ("&", "|") and i + 1 < n and command[i + 1] == ch:
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 2
                continue

            # Check for | (single pipe — not ||)
            if ch == "|":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue

            # Check for & (single ampersand — background — not &&)
            # Treat as segment separator, same as ;
            if ch == "&":
                seg = "".join(current).strip()
                if seg:
                    segments.append(seg)
                current = []
                i += 1
                continue

        current.append(ch)
        i += 1

    if in_single or in_double:
        return None  # unclosed quote → parse error

    seg = "".join(current).strip()
    if seg:
        segments.append(seg)

    return segments if segments else [""]


def _tokenize(segment: str) -> list[str] | None:
    """Tokenize a single segment with shlex. Returns None on error."""
    try:
        return shlex.split(segment)
    except ValueError:
        return None


def _resolve_command(tokens: list[str]) -> tuple[str, list[str], bool]:
    """Given tokens of a segment, resolve the effective command name.

    Skips:
      - Shell variable assignments like VAR=value
      - The 'env' command prefix (env VAR=val cmd ...)
      - 'sudo' / 'doas' wrappers (returns next non-flag token)

    Returns (cmd_name, remaining_tokens, had_sudo).
    cmd_name is lowercased basename (no path).
    """
    idx = 0
    n = len(tokens)
    had_sudo = False

    while idx < n:
        tok = tokens[idx]

        # Strip leading subshell/group characters: (, ), {, }
        # e.g. "(crontab" → "crontab"
        tok_stripped = tok.lstrip("()")
        if tok_stripped != tok and tok_stripped:
            tok = tok_stripped

        # Shell variable assignment: FOO=bar or FOO=
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            idx += 1
            continue

        # env prefix (possibly with -i, --ignore-environment, VAR=val args)
        if tok == "env":
            idx += 1
            # Flags that consume the next token as their argument.
            # Must be handled BEFORE the generic "starts with -" skip so that the
            # argument token is not mistakenly treated as the effective command name.
            _ENV_FLAGS_WITH_ARG: frozenset[str] = frozenset(
                ["-C", "--chdir", "-f", "--file",
                 "--block-signal", "--default-signal", "--ignore-signal",
                 "--list-signal-handling"]
            )
            # -S / --split-string execute an opaque string — handled in _detect_bypass;
            # here we signal "unresolvable command" by returning empty name immediately
            # when we encounter -S or --split-string (bare or =... form).
            while idx < n:
                t = tokens[idx]
                # Detect -S / --split-string (bare and --split-string=... forms)
                if t in ("-S", "--split-string") or t.startswith("--split-string="):
                    # Opaque execution: cannot resolve real command name.
                    # Return sentinel empty string — caller (classify) will treat as ask.
                    return "", [], had_sudo
                if t in _ENV_FLAGS_WITH_ARG:
                    # Skip both the flag and its argument token (e.g. -C /tmp)
                    idx += 2
                elif any(t.startswith(f + "=") for f in _ENV_FLAGS_WITH_ARG):
                    # Long form with attached value (e.g. --chdir=/tmp) — single token
                    idx += 1
                elif t.startswith("-"):
                    idx += 1
                elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
                    idx += 1
                else:
                    break
            continue

        # sudo / doas
        if tok in ("sudo", "doas"):
            had_sudo = True
            idx += 1
            # skip sudo flags like -u user, -E, -n, -H, etc.
            while idx < n:
                t = tokens[idx]
                if t.startswith("-"):
                    idx += 1
                    # some flags take an argument: -u user, -g group, -C num
                    if t in ("-u", "-g", "-C", "-p", "-r", "-t") and idx < n:
                        idx += 1
                else:
                    break
            continue

        # Found the real command
        cmd_name = os.path.basename(tok).lower()
        remaining = tokens[idx + 1 :]
        return cmd_name, remaining, had_sudo

    return "", [], had_sudo


# ---------------------------------------------------------------------------
# Bypass / opacity detection
# ---------------------------------------------------------------------------


def _detect_bypass(raw: str, segments: list[str]) -> str | None:
    """Return bypass reason string if any bypass pattern is found, else None."""
    # Check full-string regex patterns
    for pattern, reason in _BYPASS_FULL_RE:
        if pattern.search(raw):
            return reason

    # Detect pipe-to-shell at segment boundaries: if any segment resolves to
    # a shell command and the PREVIOUS segment was something else (i.e. it's
    # receiving piped input from an arbitrary source), that's a bypass.
    # Exception: if the segment is just a shell invoked with no -c flag and
    # no opaque body — that might be okay, but we're conservative.
    for i, seg in enumerate(segments):
        tokens = _tokenize(seg)
        if not tokens:
            continue
        cmd, args, _ = _resolve_command(tokens)
        if cmd in _PIPE_TO_SHELL_CMD and i > 0:
            # A shell receiving piped data — only bypass if it has -c or no args
            # (interactive shell receiving piped stdin = executing arbitrary code)
            if not args or "-c" in args:
                return f"{cmd} получает piped-данные — непрозрачное выполнение"

    return None


# ---------------------------------------------------------------------------
# Denylist matching
# ---------------------------------------------------------------------------


def _has_flag(args: list[str], *flags: str) -> bool:
    """Return True if any of the given flags appear in args."""
    return any(a in flags for a in args)


def _has_flag_prefix(args: list[str], *prefixes: str) -> bool:
    """Return True if any arg starts with any of the given prefixes."""
    return any(any(a.startswith(p) for p in prefixes) for a in args)


def _match_deny(rule_key: str, cmd: str, args: list[str], tokens: list[str]) -> bool:
    """Return True if the command+args match the given deny rule key."""

    if rule_key == "disk_format":
        if cmd.startswith("mkfs"):
            return True
        if cmd in ("wipefs", "fdisk", "parted", "sgdisk", "sfdisk"):
            return True
        if cmd == "dd":
            # dd with of=/dev/sd* or of=/dev/nvme* etc.
            for a in args:
                if re.match(r"^of=.*(?:/dev/sd|/dev/nvme|/dev/vd|/dev/xvd|/dev/disk)", a):
                    return True
        return False

    if rule_key == "rm_root":
        if cmd == "rm":
            has_r = _has_flag(args, "-r", "-R", "--recursive") or _has_flag_prefix(
                args, "-rf", "-fr", "-rF", "-fR"
            )
            has_f = _has_flag(args, "-f", "--force") or _has_flag_prefix(
                args, "-rf", "-fr", "-rF", "-fR"
            )
            if has_r or has_f:
                # Check if targeting root, home, or wildcard paths
                for a in args:
                    if a.startswith("-"):
                        continue
                    if a in ("/", "~", "*", "/*") or a.startswith("~/") and len(a) <= 2:
                        return True
                    if re.match(r"^/\s*$", a):
                        return True
                    # rm -rf /* or rm -rf ~/*
                    if a in ("/*", "~/*"):
                        return True
        return False

    if rule_key == "userdel":
        return cmd in ("userdel", "deluser", "groupdel", "delgroup")

    if rule_key == "docker_prune":
        if cmd == "docker":
            if not args:
                return False
            sub = args[0].lower()
            # docker system prune
            if sub == "system":
                return True
            # docker compose down -v or docker-compose down -v
            if sub in ("compose", "stack"):
                sub2_idx = 1
                # skip flags after compose
                while sub2_idx < len(args) and args[sub2_idx].startswith("-"):
                    sub2_idx += 1
                if sub2_idx < len(args):
                    sub2 = args[sub2_idx].lower()
                    if sub2 == "down":
                        remaining = args[sub2_idx + 1 :]
                        if _has_flag(remaining, "-v", "--volumes"):
                            return True
            # docker prune (image/container/volume/network prune)
            if sub in ("prune",):
                return True
            if len(args) >= 2 and args[1].lower() == "prune":
                return True
            # docker volume prune (rm is soft ask via docker_volume_rm)
            if sub == "volume" and len(args) >= 2 and args[1].lower() == "prune":
                return True
        return False

    if rule_key == "rm_recursive":
        if cmd == "rm":
            has_flag = False
            for a in args:
                if a.startswith("-") and not a.startswith("--"):
                    if "r" in a or "R" in a or "f" in a:
                        has_flag = True
                elif a in ("--recursive", "--force"):
                    has_flag = True
            return has_flag
        return False

    if rule_key == "rm_file":
        # rm without -r/-f flags → still destructive (irreversible), soft ask
        if cmd == "rm":
            # If it matches rm_recursive or rm_root already, those rules take precedence
            has_recursive = _has_flag(args, "-r", "-R", "--recursive") or _has_flag_prefix(
                args, "-rf", "-fr", "-rF", "-fR"
            )
            has_force = _has_flag(args, "-f", "--force")
            if has_recursive or has_force:
                return False  # covered by rm_recursive/rm_root
            # Any remaining non-flag args → file deletion
            targets = [a for a in args if not a.startswith("-")]
            return bool(targets)
        return False

    if rule_key == "reboot":
        return cmd in ("reboot", "shutdown", "halt", "poweroff", "init")

    if rule_key == "systemctl_stop":
        if cmd == "systemctl":
            if args and args[0].lower() in SYSTEMCTL_UNSAFE_SUBCOMMANDS:
                return True
        return False

    if rule_key == "docker_destructive":
        if cmd == "docker":
            if not args:
                return False
            sub = args[0].lower()
            if sub in ("rm", "rmi", "stop", "kill", "pause", "start", "restart",
                       "create", "run", "exec", "pull", "push", "build"):
                return True
            if sub == "compose":
                sub2_idx = 1
                while sub2_idx < len(args) and args[sub2_idx].startswith("-"):
                    sub2_idx += 1
                if sub2_idx < len(args):
                    sub2 = args[sub2_idx].lower()
                    if sub2 in ("down", "up", "restart", "stop", "start",
                                "build", "pull", "push", "rm", "kill"):
                        return True
            if sub == "container" and len(args) >= 2:
                return args[1].lower() in ("rm", "stop", "kill", "start",
                                           "restart", "pause", "create", "run")
            if sub == "image" and len(args) >= 2:
                return args[1].lower() in ("rm", "remove", "pull", "push",
                                           "build", "tag", "prune")
        return False

    if rule_key == "docker_volume_rm":
        # docker volume rm <name> — soft ask (not hard deny, as per design)
        if cmd == "docker":
            if not args:
                return False
            sub = args[0].lower()
            if sub == "volume" and len(args) >= 2 and args[1].lower() == "rm":
                return True
        return False

    if rule_key == "kill_hard":
        if cmd == "kill":
            # kill -9 or kill -SIGKILL
            for a in args:
                if a in ("-9", "-SIGKILL", "--signal=9", "--signal=SIGKILL"):
                    return True
            return False
        if cmd in ("pkill", "killall"):
            return True
        return False

    if rule_key == "crontab_remove":
        if cmd == "crontab":
            return "-r" in args
        return False

    if rule_key == "crontab_install":
        # crontab - (reading from stdin) when appearing as the LAST segment in a pipe
        # is dangerous UNLESS the pipeline is the known safe pattern:
        #   (crontab -l; ...) | crontab -
        # where previous segments contain "crontab -l" (visible content).
        # This rule is checked in context: see _check_crontab_install().
        # Here we just return False — actual detection is done via dedicated function.
        return False

    if rule_key == "git_force_push":
        if cmd == "git":
            if args and args[0] == "push":
                return _has_flag(args[1:], "-f", "--force") or any(
                    a.startswith("--force-with-lease") for a in args[1:]
                )
        return False

    if rule_key == "git_flag_unsafe":
        # Flag-level unsafe patterns for subcommands that are otherwise safe.
        if cmd == "git" and args:
            sub = args[0]
            rest = args[1:]
            # git reset --hard
            if sub == "reset" and _GIT_RESET_HARD_FLAG in rest:
                return True
            # git clean -f/-d/-x/-X (any combination containing these)
            if sub == "clean":
                for a in rest:
                    if a.startswith("-") and not a.startswith("--"):
                        letters = a.lstrip("-")
                        if any(c in letters for c in "fdxX"):
                            return True
                    if a in ("--force",):
                        return True
            # git branch -d/-D <name> (delete branch)
            if sub == "branch" and _has_flag(rest, "-d", "-D", "--delete"):
                return True
            # git tag -d <name> (delete tag)
            if sub == "tag" and _has_flag(rest, "-d", "--delete"):
                return True
            # git rebase (any form)
            if sub == "rebase":
                return True
            # git config with unsafe flags (--unset, --global edit, --system, --edit)
            if sub == "config":
                if _has_flag(rest, "--unset", "--unset-all", "--remove-section",
                              "--edit", "--global", "--system"):
                    return True
            # git update-ref -d
            if sub == "update-ref" and _has_flag(rest, "-d", "--delete"):
                return True
        return False

    if rule_key == "firewall_write":
        if cmd in ("iptables", "ip6tables", "iptables-restore", "ip6tables-restore"):
            return any(a in IPTABLES_WRITE_FLAGS for a in args)
        if cmd in ("nft",):
            # nft with modifying verbs: add, delete, flush, rename, insert, replace
            modifying = {"add", "delete", "flush", "rename", "insert", "replace",
                         "reset", "destroy"}
            return any(a in modifying for a in args)
        if cmd == "ufw":
            modifying = {"enable", "disable", "reset", "delete", "allow", "deny",
                         "reject", "limit", "insert", "prepend", "route"}
            return any(a in modifying for a in args)
        return False

    if rule_key == "chmod_chown_recursive":
        if cmd in ("chmod", "chown", "chgrp"):
            return _has_flag(args, "-R", "--recursive") or _has_flag_prefix(args, "-R")
        return False

    if rule_key == "write_system_path":
        # Detect > or >> redirects to system paths in the raw segment tokens
        # We check for ">" or ">>" followed by a system path token
        for i_t, tok in enumerate(tokens):
            if tok in (">", ">>"):
                if i_t + 1 < len(tokens) and _is_system_path(tokens[i_t + 1]):
                    return True
            # Combined redirect like >>/etc/passwd
            if tok.startswith(">") and len(tok) > 1:
                target = tok.lstrip(">")
                if target and _is_system_path(target):
                    return True
        return False

    if rule_key == "git_write":
        if cmd == "git":
            if args and args[0] in GIT_UNSAFE_SUBCOMMANDS:
                return True
        return False

    if rule_key == "kubectl_write":
        if cmd == "kubectl":
            if args and args[0] in KUBECTL_UNSAFE_SUBCOMMANDS:
                return True
        return False

    if rule_key == "db_destroy":
        # Catch: sqlite3 ... < dump.sql with DROP, psql -c "DROP TABLE", etc.
        # Simple heuristic: command is a DB client and args contain destructive keywords
        if cmd in ("sqlite3", "psql", "mysql", "mariadb", "clickhouse-client"):
            combined = " ".join(args).lower()
            if re.search(r"\b(?:drop\s+(?:database|table|schema)|truncate\s+table)\b", combined):
                return True
        # truncate (file utility) writing zero bytes — only truly destructive context
        if cmd == "truncate":
            return True
        return False

    return False


def _check_crontab_install(segments: list[str], seg_index: int) -> bool:
    """Return True if this crontab - segment should trigger crontab_install.

    The safe pattern is: (crontab -l; echo ...) | crontab -
    where a preceding segment contains 'crontab -l' (content is visible).
    Any other 'crontab -' receiving stdin → ask.
    """
    # Check if any earlier segment contains 'crontab -l' (the safe read-first pattern)
    for earlier_seg in segments[:seg_index]:
        tokens = _tokenize(earlier_seg)
        if tokens:
            cmd, args, _ = _resolve_command(tokens)
            if cmd == "crontab" and "-l" in args:
                return False  # safe pattern — content visible
    return True  # unknown/unsafe crontab - install → ask


def _check_segment_deny(
    cmd: str, args: list[str], tokens: list[str],
    segments: list[str] | None = None, seg_index: int = 0,
) -> tuple[str, str, bool] | None:
    """Check segment against all deny rules. Returns (category, reason, hard) or None."""
    # Special case: crontab - install check requires pipeline context
    if cmd == "crontab" and args == ["-"]:
        if segments is not None and _check_crontab_install(segments, seg_index):
            rule = next(r for r in DENY_RULES if r.match_key == "crontab_install")
            return rule.category, rule.reason, rule.hard

    for rule in DENY_RULES:
        if rule.match_key == "crontab_install":
            continue  # handled above
        if _match_deny(rule.match_key, cmd, args, tokens):
            return rule.category, rule.reason, rule.hard
    return None


# ---------------------------------------------------------------------------
# Allowlist checking
# ---------------------------------------------------------------------------


def _check_segment_allow(cmd: str, args: list[str]) -> bool:
    """Return True if segment is on the allowlist and has no dangerous flags."""
    if cmd not in ALLOW_COMMANDS:
        return False

    # Additional checks for commands that have read/write modes

    if cmd == "git":
        sub = args[0] if args else ""
        rest = args[1:]
        # Subcommand must be in safe list
        if sub not in GIT_SAFE_SUBCOMMANDS:
            return False
        # stash: only list/show are read-only; pop/apply/drop/clear are mutating
        if sub == "stash":
            stash_sub = rest[0] if rest else ""
            return stash_sub in ("list", "show", "")
        # Flag-level unsafe check: if the deny rule git_flag_unsafe would fire, not safe
        if _match_deny("git_flag_unsafe", cmd, args, []):
            return False
        # push with force flags → handled by git_force_push deny rule
        if sub == "push" and (
            _has_flag(rest, "-f", "--force")
            or any(a.startswith("--force-with-lease") for a in rest)
        ):
            return False
        return True

    if cmd == "systemctl":
        sub = args[0] if args else ""
        return sub in SYSTEMCTL_SAFE_SUBCOMMANDS

    if cmd == "docker":
        if not args:
            return False
        sub = args[0].lower()
        if sub in DOCKER_SAFE_SUBCOMMANDS:
            # docker network ls / docker volume ls are safe sub-sub-commands
            if sub == "network":
                return not args[1:] or args[1].lower() in DOCKER_NETWORK_SAFE
            if sub == "volume":
                return not args[1:] or args[1].lower() in DOCKER_VOLUME_SAFE
            return True
        # docker ps, docker images etc. — already in DOCKER_SAFE_SUBCOMMANDS
        return False

    if cmd == "kubectl":
        if not args:
            return False
        sub = args[0].lower()
        # kubectl config view is safe; kubectl config set-* is not
        if sub == "config":
            sub2 = args[1].lower() if len(args) > 1 else ""
            return sub2 in ("view", "get-contexts", "get-clusters", "current-context", "")
        return sub in KUBECTL_SAFE_SUBCOMMANDS

    if cmd in ("iptables", "ip6tables"):
        # Only safe if ALL flags are read-only
        write_flags = [a for a in args if a in IPTABLES_WRITE_FLAGS]
        if write_flags:
            return False
        # At least one read flag or pure -n/-v
        return True

    if cmd == "nft":
        # nft list ... is safe; others not
        return bool(args) and args[0] == "list"

    if cmd == "ip":
        # ip addr/route/link show — safe; ip addr add/del — not safe
        modifying = {"add", "del", "delete", "append", "change", "replace",
                     "flush", "set", "up", "down"}
        return not any(a in modifying for a in args)

    if cmd in ("curl", "wget"):
        # Not safe if output goes to a file via -o/-O pointing to system path
        for i_a, a in enumerate(args):
            if a in ("-o", "-O", "--output"):
                target = args[i_a + 1] if i_a + 1 < len(args) else ""
                if _is_system_path(target):
                    return False
            if a.startswith("--output="):
                target = a.split("=", 1)[1]
                if _is_system_path(target):
                    return False
        return True

    if cmd == "sed":
        # sed -i (in-place edit) is not read-only
        return not _has_flag(args, "-i", "--in-place") and not _has_flag_prefix(
            args, "-i"
        )

    if cmd == "awk":
        # awk writing to files with > redirection in program body is hard to detect;
        # if -f points to a system path that's dangerous but rare; treat as safe
        return True

    if cmd == "crontab":
        # crontab -l (list) is safe; -r (remove) is not; -e (edit) is not
        # crontab - (read from stdin to install) is handled via denylist
        # crontab_install rule (checked with pipeline context).
        # Here we allow crontab - only if it passed the denylist check (i.e. safe pattern).
        if "-r" in args or "-e" in args:
            return False
        if "-l" in args or not args:
            return True
        # crontab - is ambiguous; let denylist handle it (if it reached here, it's safe)
        if args == ["-"]:
            return True
        return False

    if cmd == "journalctl":
        # journalctl --vacuum-* flags are destructive
        dangerous = {"--vacuum-size", "--vacuum-time", "--vacuum-files",
                     "--rotate", "--flush", "--relinquish-var", "--smart-relinquish-var"}
        return not any(a.split("=")[0] in dangerous for a in args)

    if cmd == "find":
        # find -delete or -exec ... rm is destructive
        if "-delete" in args:
            return False
        exec_idx = None
        for i_a, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok"):
                exec_idx = i_a
                break
        if exec_idx is not None:
            exec_args = args[exec_idx + 1:]
            if not exec_args:
                return False  # -exec with no args — unknown, be conservative
            executor = exec_args[0]
            # Only allow known read-only executors explicitly
            _FIND_SAFE_EXECUTORS: frozenset[str] = frozenset(
                ["ls", "cat", "head", "tail", "stat", "file", "wc",
                 "grep", "echo", "printf", "xxd", "od", "diff",
                 "readlink", "realpath", "dirname", "basename"]
            )
            executor_name = os.path.basename(executor).lower()
            if executor_name not in _FIND_SAFE_EXECUTORS:
                return False  # unknown or dangerous executor → not safe
        return True

    if cmd in ("bash", "sh"):
        # Allow ONLY when the first non-flag argument is a .sh script located
        # under the trusted agent workspace (AGENT_WORKDIR env, default ~/agents).
        # Anything else (bash -c, script in /tmp, script outside workspace) → not safe.
        script_arg: str | None = None
        for a in args:
            if not a.startswith("-"):
                script_arg = a
                break
        if script_arg is None:
            # No positional arg (e.g. bare "bash" or "bash --login") → not safe
            return False
        # Must end with .sh and be under the trusted root
        _TRUSTED_ROOT = os.environ.get("AGENT_WORKDIR", os.path.expanduser("~/agents")) + "/"
        script_abs = os.path.realpath(script_arg) if script_arg.startswith("/") else script_arg
        if not script_arg.endswith(".sh"):
            return False
        # Accept both absolute paths under trusted root and relative paths that
        # start with the trusted root prefix (after resolution).
        if not (script_arg.startswith(_TRUSTED_ROOT) or script_abs.startswith(_TRUSTED_ROOT)):
            return False
        return True

    if cmd == "npx":
        # Allow only when "openspec" appears somewhere in the arguments.
        # Covers: npx @fission-ai/openspec validate/archive/init ...
        return any("openspec" in a.lower() for a in args)

    if cmd in ("pytest",):
        # pytest is always safe to run (runs tests, does not modify system)
        return True

    if cmd in ("python3", "python"):
        # python3 -m pytest → safe
        # python3 -m <anything else> → ask (could do anything)
        # python3 script.py → ask (arbitrary script)
        if args and args[0] == "-m":
            module = args[1] if len(args) > 1 else ""
            return module in ("pytest",)
        # bare python3 or python3 -c or arbitrary script → not safe
        return False

    # For other allowlisted commands, check for output redirects to system paths
    # (handled via denylist write_system_path rule, not here)
    return True


# ---------------------------------------------------------------------------
# Main classify function
# ---------------------------------------------------------------------------


_VERDICT_ORDER = {"deny": 2, "ask": 1, "allow": 0}


def _combine(a: str, b: str) -> str:
    """Return the more restrictive of two verdicts."""
    return a if _VERDICT_ORDER[a] >= _VERDICT_ORDER[b] else b


def classify(command: str) -> dict[str, str]:
    """Classify a shell command string.

    Returns dict with keys:
      verdict  — "allow" | "ask" | "deny"
      category — short category name (empty string if allow)
      reason   — human-readable reason for approval button
    """
    cmd = command.strip()

    # Empty command → allow (no-op)
    if not cmd:
        return {"verdict": "allow", "category": "", "reason": "пустая команда"}

    # Step 1: Segment
    segments = _split_segments(cmd)
    if segments is None:
        return {
            "verdict": "ask",
            "category": "непарсимая команда",
            "reason": "команда не может быть разобрана (незакрытая кавычка или синтаксис)",
        }

    # Step 2: Bypass detection (before segmentation logic, on full string)
    bypass_reason = _detect_bypass(cmd, segments)
    if bypass_reason:
        return {
            "verdict": "ask",
            "category": "непрозрачное исполнение/возможный обход",
            "reason": bypass_reason,
        }

    # Steps 3–6: Per-segment analysis, then fold
    overall_verdict = "allow"
    overall_category = ""
    overall_reason = ""

    for seg_idx, seg in enumerate(segments):
        if not seg.strip():
            continue

        tokens = _tokenize(seg)
        if tokens is None:
            # shlex parse error on segment
            seg_verdict = "ask"
            seg_category = "непарсимая команда"
            seg_reason = f"сегмент не разобран shlex: {seg!r}"
            if _VERDICT_ORDER[seg_verdict] > _VERDICT_ORDER[overall_verdict]:
                overall_verdict = seg_verdict
                overall_category = seg_category
                overall_reason = seg_reason
            continue

        if not tokens:
            continue

        cmd_name, args, had_sudo = _resolve_command(tokens)

        if not cmd_name:
            # Distinguish between a truly empty/assignment-only segment (tokens are
            # all VAR=val, so the segment is effectively a no-op) versus a segment
            # that has non-assignment tokens but whose command name could not be
            # resolved (e.g. env -S or another opaque wrapper construct).
            #
            # Heuristic: if every token in the original list is either a VAR=value
            # assignment or a leading grouping character stripped to nothing, the
            # segment is a benign no-op.  Otherwise something went wrong during
            # resolution and we cannot safely evaluate the command — fail CLOSED.
            all_assignments = all(
                re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or t.strip("(){}") == ""
                for t in tokens
            )
            if all_assignments:
                # Genuinely empty / assignment-only segment — allow (no-op)
                continue
            # Non-empty segment with unresolvable command name → fail closed
            seg_verdict = "ask"
            seg_category = "не удалось определить команду"
            seg_reason = f"не удалось определить имя команды в сегменте: {seg!r}"
            if _VERDICT_ORDER[seg_verdict] > _VERDICT_ORDER[overall_verdict]:
                overall_verdict = seg_verdict
                overall_category = seg_category
                overall_reason = seg_reason
            continue

        # Step 3: Denylist
        deny_result = _check_segment_deny(
            cmd_name, args, tokens, segments=segments, seg_index=seg_idx
        )
        if deny_result is not None:
            cat, reason, hard = deny_result
            seg_verdict = "deny" if hard else "ask"
            if _VERDICT_ORDER[seg_verdict] > _VERDICT_ORDER[overall_verdict]:
                overall_verdict = seg_verdict
                overall_category = cat
                overall_reason = reason
            continue

        # Step 4: Allowlist
        if _check_segment_allow(cmd_name, args):
            # Safe — doesn't downgrade current verdict
            continue

        # Step 5 / 6: Unknown command or unsafe variant of allow command
        unk_verdict = UNKNOWN_POLICY if UNKNOWN_POLICY in ("allow", "ask", "deny") else "ask"
        if cmd_name in ALLOW_COMMANDS:
            # Known command but outside safe subcommand range → ask
            unk_verdict = "ask"
            cat = f"команда {cmd_name!r} с нечитаемыми флагами"
            reason = f"команда {cmd_name!r} выполняет операцию вне read-only режима"
        else:
            cat = "неизвестная команда"
            reason = f"команда {cmd_name!r} не входит в список разрешённых"

        if _VERDICT_ORDER[unk_verdict] > _VERDICT_ORDER[overall_verdict]:
            overall_verdict = unk_verdict
            overall_category = cat
            overall_reason = reason

    return {
        "verdict": overall_verdict,
        "category": overall_category,
        "reason": overall_reason,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    result = classify(raw)
    print(json.dumps(result, ensure_ascii=False))
    verdict = result["verdict"]
    if verdict == "allow":
        sys.exit(0)
    elif verdict == "deny":
        sys.exit(2)
    else:
        sys.exit(10)

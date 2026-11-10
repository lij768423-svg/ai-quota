#!/usr/bin/env python3
"""ai-quota: 只读本机数据的 AI 额度统计。

不登录、不读任何凭据文件，自己不调用服务商接口。数据来自官方客户端：
  Codex        ~/.codex/sessions 日志里的 rate_limits（真实百分比）和 token
  Claude Code  官方 statusLine 传给 `ai-quota statusline` 的 rate_limits，
               以及 ~/.claude/projects 日志里的 token
  Grok         ~/.grok/logs/unified.jsonl 里官方客户端记下的额度（登录后才有），
               以及 ~/.grok/sessions 日志里的 token
  Antigravity  官方命令 `agy -p /usage --output-format json` 的输出，
               这个命令不开对话、不花额度，结果存在本地，几分钟查一次

`ai-quota refresh`（和网页上的“刷新”）让四家客户端用各自的只读方式重新查一次：
  claude -p /usage、codex app-server 的 account/rateLimits/read、
  grok agent stdio 的 _x.ai/billing、agy -p /usage。都不开对话、不花额度。
"""
from __future__ import annotations

import json
import os
import sys
import time

FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400
# 同一个额度窗口的重置时间可能有几秒到几分钟的抖动。
SAME_WINDOW_SECONDS = 600

CLAUDE_WINDOWS = (("five_hour", "5 小时"), ("seven_day", "每周"))
WINDOW_NAMES = {300: "5 小时", 10080: "每周"}
WINDOW_SECONDS = {"5 小时": FIVE_HOURS, "每周": SEVEN_DAYS}

# agy 查询要联网，结果存在本地。平时 5 分钟最多查一次，点“刷新”或在终端运行时 1 分钟。
ANTIGRAVITY_REFRESH_SECONDS = 300
ANTIGRAVITY_FRESH_SECONDS = 60
AGY_TIMEOUT_SECONDS = 20
AGY_WINDOWS = {"5h": "5 小时", "weekly": "每周"}

UI_PORT = 18765
# 页面每分钟轮询一次。页面关掉后服务闲置这么久就自己退出，不留在后台。
UI_IDLE_EXIT_SECONDS = 600
UI_REPORT_MAX_AGE = 20

USAGE = """用法：
  ai-quota             显示各家额度和本机 token 用量
  ai-quota refresh     让各家官方客户端重新查一次额度，再显示（不花额度）
  ai-quota --json      输出 JSON
  ai-quota ui          打开本机网页界面，闲置 10 分钟后服务自动退出
  ai-quota ui --serve  在前台运行网页服务
  ai-quota statusline  给 Claude Code 的 statusLine 调用，从标准输入读数据"""


def home_dir():
    return os.environ.get("AI_QUOTA_HOME") or os.path.expanduser("~")


def state_file(home, name):
    return os.path.join(home, ".local", "state", "ai-quota", name)


def state_path(home):
    return state_file(home, "claude.json")


def config_path(home):
    return os.path.join(home, ".config", "ai-quota", "config.json")


def read_local_models(home):
    """自己部署的模型没有额度，只在页面上占一个位置，显示不限额度。不连服务器。

    在 ~/.config/ai-quota/config.json 里写：
      {"local_models": [{"name": "模型名", "detail": "跑在哪台机器上（可选）"}]}
    """
    models = load_json(config_path(home)).get("local_models")
    found = []
    for model in models if isinstance(models, list) else []:
        if isinstance(model, dict) and isinstance(model.get("name"), str) and model["name"].strip():
            detail = model.get("detail")
            found.append({"name": model["name"].strip(), "detail": detail if isinstance(detail, str) else None})
    return found


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = "{}.{}.tmp".format(path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(temporary, path)


def merge_window(current, used, resets_at, seen_at, touch=False):
    """合并一条额度记录。同一窗口内用量只会上升，并发会话可能写入旧值，所以取最大值。

    touch=True 时，数值没变也刷新 seen_at（日志里的时间戳可信）。
    """
    if used is None or resets_at is None:
        return current
    new = {"used_percent": float(used), "resets_at": int(resets_at), "seen_at": seen_at}
    if current is None:
        return new
    gap = new["resets_at"] - current["resets_at"]
    if gap > SAME_WINDOW_SECONDS:
        return new
    if gap < -SAME_WINDOW_SECONDS:
        return current
    if new["used_percent"] > current["used_percent"]:
        new["seen_at"] = max(seen_at, current["seen_at"]) if touch else seen_at
        return new
    if touch and seen_at > current["seen_at"]:
        return dict(current, seen_at=seen_at)
    return current


def recent_files(root, name_test, since):
    """找 since 之后改过的日志文件，不跟随符号链接。"""
    found = []
    for folder, _, names in os.walk(root):
        for name in names:
            if not name_test(name):
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.getmtime(path) >= since:
                    found.append(path)
            except OSError:
                continue
    return found


def iso_to_epoch(value):
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def matching_records(path, needles):
    """逐行读日志，只解析同时包含所有 needles 的行。"""
    try:
        with open(path, "rb") as handle:
            for line in handle:
                if all(needle in line for needle in needles):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield record
    except OSError:
        return


_parse_cache = {}


def cached_parse(path, parser):
    """同一进程内按文件缓存解析结果，界面定时刷新时只重读变过的日志。"""
    try:
        stat = os.stat(path)
    except OSError:
        return parser(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    hit = _parse_cache.get((parser, path))
    if hit and hit[0] == stamp:
        return hit[1]
    result = parser(path)
    _parse_cache[(parser, path)] = (stamp, result)
    return result


def parse_codex_file(path):
    limits = {}
    events = {}
    previous_total = None
    for record in matching_records(path, (b'"token_count"',)):
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        seen_at = iso_to_epoch(record.get("timestamp"))
        if seen_at is None:
            continue
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            limit_id = rate_limits.get("limit_id") or "codex"
            plan = rate_limits.get("plan_type")
            entry = limits.setdefault(limit_id, {"plan": None, "plan_seen_at": None, "plans": {}})
            if entry["plan_seen_at"] is None or seen_at >= entry["plan_seen_at"]:
                entry["plan"], entry["plan_seen_at"] = plan, seen_at
            # 换套餐后窗口和数值都会变。各套餐的窗口分开存，只显示最新套餐的。
            windows = entry["plans"].setdefault(plan, {})
            for key in ("primary", "secondary"):
                window = rate_limits.get(key)
                if not isinstance(window, dict):
                    continue
                minutes = window.get("window_minutes")
                name = WINDOW_NAMES.get(minutes, "{} 分钟".format(minutes))
                windows[name] = merge_window(
                    windows.get(name),
                    window.get("used_percent"),
                    window.get("resets_at"),
                    seen_at,
                    touch=True,
                )
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        total = info.get("total_token_usage")
        last = info.get("last_token_usage")
        # 只刷新额度时 total 不变，这种事件不算一次新的用量。
        if total == previous_total or not isinstance(last, dict):
            previous_total = total
            continue
        previous_total = total
        tokens = (
            int(last.get("input_tokens") or 0)
            - int(last.get("cached_input_tokens") or 0)
            + int(last.get("output_tokens") or 0)
        )
        total_tokens = total.get("total_tokens") if isinstance(total, dict) else None
        # fork 出来的会话可能带着原会话的记录，用时间戳和累计值去重。
        events[(record.get("timestamp"), total_tokens)] = (seen_at, max(tokens, 0))
    return limits, events


def read_codex(home, now):
    """返回 (额度, token 事件)。额度按 limit_id 分组，只含最新套餐的窗口。"""
    root = os.path.join(home, ".codex", "sessions")
    limits = {}
    events = {}
    sources = []
    for path in recent_files(root, lambda n: n.endswith(".jsonl"), now - SEVEN_DAYS):
        file_limits, file_events = cached_parse(path, parse_codex_file)
        events.update(file_events)
        sources.append(file_limits)
    # `ai-quota refresh` 查到的结果，当作又一份日志并进来。
    snapshot = load_json(state_file(home, "codex.json"))
    fetched_at = snapshot.get("fetched_at")
    if isinstance(fetched_at, (int, float)) and isinstance(snapshot.get("limits"), dict):
        sources.append(
            {
                limit_id: {"plan": entry.get("plan"), "plan_seen_at": fetched_at, "plans": {entry.get("plan"): entry.get("windows") or {}}}
                for limit_id, entry in snapshot["limits"].items()
                if isinstance(entry, dict)
            }
        )
    for file_limits in sources:
        for limit_id, entry in file_limits.items():
            merged = limits.setdefault(limit_id, {"plan": None, "plan_seen_at": None, "plans": {}})
            if merged["plan_seen_at"] is None or entry["plan_seen_at"] > merged["plan_seen_at"]:
                merged["plan"], merged["plan_seen_at"] = entry["plan"], entry["plan_seen_at"]
            for plan, windows in entry["plans"].items():
                target = merged["plans"].setdefault(plan, {})
                for name, window in windows.items():
                    if window:
                        target[name] = merge_window(
                            target.get(name),
                            window["used_percent"],
                            window["resets_at"],
                            window["seen_at"],
                            touch=True,
                        )
    current = {
        limit_id: {"plan": entry["plan"], "windows": entry["plans"].get(entry["plan"], {})}
        for limit_id, entry in limits.items()
    }
    return current, list(events.values())


# Codex 自己把 prolite（每月 100 美元那一档）显示成 Pro。
CODEX_PLAN_NAMES = {"prolite": "Pro"}


def codex_plan_name(plan):
    if not plan:
        return None
    return CODEX_PLAN_NAMES.get(str(plan)) or str(plan).capitalize()


def parse_claude_file(path):
    by_message = {}
    for record in matching_records(path, (b'"assistant"', b'"usage"')):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        message_id = message.get("id")
        seen_at = iso_to_epoch(record.get("timestamp"))
        if not isinstance(usage, dict) or not message_id or seen_at is None:
            continue
        tokens = (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
            + int(usage.get("output_tokens") or 0)
        )
        # 流式响应会重复写同一条消息，留用量最大的那条。
        known = by_message.get(message_id)
        if known is None or tokens > known[1]:
            by_message[message_id] = (seen_at, tokens)
    return by_message


def read_claude_tokens(home, now):
    root = os.path.join(home, ".claude", "projects")
    by_message = {}
    for path in recent_files(root, lambda n: n.endswith(".jsonl"), now - SEVEN_DAYS):
        # 恢复的会话和子代理日志可能重复记录同一条消息，跨文件再去重一次。
        for message_id, event in cached_parse(path, parse_claude_file).items():
            known = by_message.get(message_id)
            if known is None or event[1] > known[1]:
                by_message[message_id] = event
    return list(by_message.values())


def parse_grok_file(path):
    events = {}
    for record in matching_records(path, (b'"turn_completed"',)):
        params = record.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        if not isinstance(update, dict) or update.get("sessionUpdate") != "turn_completed":
            continue
        usage = update.get("usage")
        seen_at = record.get("timestamp")
        if not isinstance(usage, dict) or not isinstance(seen_at, (int, float)):
            continue
        if seen_at > 1e12:
            seen_at /= 1000
        tokens = (
            int(usage.get("inputTokens") or 0)
            - int(usage.get("cachedReadTokens") or 0)
            + int(usage.get("outputTokens") or 0)
        )
        key = update.get("prompt_id") or (path, seen_at)
        events[key] = (seen_at, max(tokens, 0))
    return events


def read_grok_tokens(home, now):
    root = os.path.join(home, ".grok", "sessions")
    events = {}
    for path in recent_files(root, lambda n: n == "updates.jsonl", now - SEVEN_DAYS):
        events.update(cached_parse(path, parse_grok_file))
    return list(events.values())


def grok_window_name(start, end):
    if start is None:
        return "当前周期"
    days = round((end - start) / 86400)
    return "每周" if days == 7 else "{} 天".format(days)


def grok_window(config, seen_at):
    """把 Grok 的 credits 配置换成 (窗口名, 窗口)。未登录时百分比为空，返回 None。"""
    if not isinstance(config, dict) or not isinstance(seen_at, (int, float)):
        return None
    period = config.get("currentPeriod")
    percent = config.get("creditUsagePercent")
    if not isinstance(period, dict) or not isinstance(percent, (int, float)):
        return None
    end = iso_to_epoch(period.get("end"))
    if end is None:
        return None
    name = grok_window_name(iso_to_epoch(period.get("start")), end)
    return name, {"used_percent": float(percent), "resets_at": int(end), "seen_at": seen_at}


def take_newer_grok(entry, name, window, plan):
    """Grok 每条记录都是当时从服务器取的值，以最新的为准。

    用量会变少（服务商重置或退回额度），所以不能像 Codex 那样取最大值。
    """
    current = entry["windows"].get(name)
    if current is None or window["seen_at"] >= current["seen_at"]:
        entry["windows"][name] = window
    if plan and window["seen_at"] >= entry["plan_seen_at"]:
        entry["plan"], entry["plan_seen_at"] = plan, window["seen_at"]


def parse_grok_log(path):
    """Grok Build 启动或查询额度时会把额度写进日志。"""
    entry = {"plan": None, "plan_seen_at": 0, "windows": {}}
    for record in matching_records(path, (b"fetched credits config",)):
        context = record.get("ctx")
        if not isinstance(context, dict) or record.get("msg") != "billing: fetched credits config":
            continue
        found = grok_window(context.get("config"), iso_to_epoch(record.get("ts")))
        if found:
            take_newer_grok(entry, found[0], found[1], context.get("subscriptionTier"))
    return entry


def read_grok_limits(home, now):
    root = os.path.join(home, ".grok", "logs")
    merged = {"plan": None, "plan_seen_at": 0, "windows": {}}
    test = lambda n: n.startswith("unified") and ".jsonl" in n
    for path in recent_files(root, test, now - SEVEN_DAYS):
        entry = cached_parse(path, parse_grok_log)
        for name, window in entry["windows"].items():
            take_newer_grok(merged, name, window, None)
        if entry["plan"] and entry["plan_seen_at"] >= merged["plan_seen_at"]:
            merged["plan"], merged["plan_seen_at"] = entry["plan"], entry["plan_seen_at"]
    # `ai-quota refresh` 查到的结果。
    snapshot = load_json(state_file(home, "grok.json"))
    found = grok_window(snapshot.get("config"), snapshot.get("fetched_at"))
    if found:
        take_newer_grok(merged, found[0], found[1], snapshot.get("plan"))
    return {"plan": merged["plan"], "windows": merged["windows"]}


def grok_label(plan):
    if not plan:
        return "Grok"
    return str(plan) if "grok" in str(plan).lower() else "Grok " + str(plan)


_claude_plan_cache = {"checked_at": None, "plan": None}


def read_claude_plan(now, home=None):
    """用官方命令 `claude auth status` 取套餐，只保留套餐字段，一小时查一次。"""
    # 临时 home 是测试环境，不查真实账号。
    if (home or home_dir()) != os.path.expanduser("~"):
        return None
    checked_at = _claude_plan_cache["checked_at"]
    if checked_at is not None and now - checked_at < 3600:
        return _claude_plan_cache["plan"]

    import shutil
    import subprocess

    binary = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    plan = None
    try:
        result = subprocess.run(
            [binary, "auth", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            plan = data.get("subscriptionType")
    except (OSError, ValueError, subprocess.SubprocessError):
        plan = None
    _claude_plan_cache.update(checked_at=now, plan=plan)
    return plan


def agy_group_name(name):
    """“Gemini Models” 写成 “Gemini”，“Claude and GPT models” 写成 “Claude 和 GPT”。"""
    name = str(name or "").strip()
    if name.lower().endswith(" models"):
        name = name[: -len(" models")]
    return name.replace(" and ", " 和 ") or "其他模型"


def parse_agy_usage(text, fetched_at):
    """解析 `agy -p /usage --output-format json`，只留分组名、用量和重置时间。格式不对返回 None。"""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    command = data.get("command") if isinstance(data, dict) else None
    payload = command.get("data") if isinstance(command, dict) else None
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return None
    parsed = []
    for group in groups:
        buckets = group.get("buckets") if isinstance(group, dict) else None
        windows = {}
        for bucket in buckets if isinstance(buckets, list) else []:
            if not isinstance(bucket, dict) or not bucket.get("window"):
                continue
            remaining = bucket.get("remaining_fraction")
            resets_at = iso_to_epoch(bucket.get("reset_time"))
            # 停用的额度没有剩余比例，跳过。
            if not isinstance(remaining, (int, float)) or resets_at is None:
                continue
            name = AGY_WINDOWS.get(bucket["window"], str(bucket["window"]))
            used = round(min(max(1 - remaining, 0), 1) * 100, 1)
            windows[name] = {"used_percent": used, "resets_at": int(resets_at), "seen_at": fetched_at}
        if windows:
            parsed.append({"name": agy_group_name(group.get("name")), "windows": windows})
    return parsed


def agy_binary():
    import shutil

    path = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
    return path if os.access(path, os.X_OK) else None


def run_agy_usage(binary):
    """运行官方命令查额度，返回标准输出，失败返回 None。print 模式的 /usage 不开对话、不花额度。"""
    import subprocess

    try:
        result = subprocess.run(
            [binary, "-p", "/usage", "--output-format", "json", "--print-timeout", "15s"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=AGY_TIMEOUT_SECONDS,
            cwd=os.path.expanduser("~"),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def read_antigravity_usage(home, now, max_age=ANTIGRAVITY_REFRESH_SECONDS, runner=None):
    """返回 (额度分组或 None, 能否查询)。

    结果存在本地。距上次查询（成功或失败）不到 max_age 秒时直接用存下的结果。
    临时 home 是测试环境，不查真实账号，除非测试自己传 runner。
    """
    path = state_file(home, "antigravity.json")
    state = load_json(path)
    if runner is None and home == os.path.expanduser("~"):
        binary = agy_binary()
        if binary:
            runner = lambda: run_agy_usage(binary)
    last_try = max(state.get("fetched_at") or 0, state.get("attempted_at") or 0)
    if runner is not None and not 0 <= now - last_try < max_age:
        groups = parse_agy_usage(runner() or "", now)
        state["attempted_at"] = now
        if groups is not None:
            state.update(fetched_at=now, groups=groups)
        try:
            write_json_atomic(path, state)
        except OSError:
            pass
    groups = state.get("groups")
    return (groups if isinstance(groups, list) else None), runner is not None


def read_antigravity(home):
    """Antigravity 桌面应用的日志只记套餐，不记额度。没有日志目录时返回 None。"""
    folder = os.path.join(home, "Library", "Logs", "Antigravity")
    if not os.path.isdir(folder):
        return None
    import re

    pattern = re.compile(rb'SetUserTier called with userTier: "([^"]*)", tierDisplayName: "([^"]*)"')
    plan = None
    try:
        with open(os.path.join(folder, "language_server.log"), "rb") as handle:
            for line in handle:
                if b"SetUserTier" not in line:
                    continue
                match = pattern.search(line)
                if match and (match.group(2) or match.group(1)):
                    plan = (match.group(2) or match.group(1)).decode("utf-8", "replace")
    except OSError:
        pass
    return {"plan": plan}


# 一键刷新：让各家官方客户端用它们自己的只读方式重新查一次额度。
# 都不开对话、不花额度、不碰凭据，查到的数字只存百分比和时间。
REFRESH_TIMEOUT_SECONDS = 45
# 网页上连点“刷新”时，同一家这么久之内不重复查。
REFRESH_MIN_GAP_SECONDS = 30
CLIENT_INFO = {"name": "ai-quota", "version": "1"}
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
CLAUDE_USAGE_LINES = (("five_hour", "current session"), ("seven_day", "current week (all models)"))


class RefreshError(Exception):
    """某一家刷新失败。消息会直接显示给用户，不能带凭据或账号。"""


def find_binary(name):
    import shutil

    for path in (
        shutil.which(name),
        os.path.expanduser("~/.local/bin/" + name),
        os.path.expanduser("~/.{0}/bin/{0}".format(name)),
    ):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise RefreshError("没找到 {} 命令".format(name))


def refresh_workdir(home):
    """客户端在这个目录里启动，不在用户的项目里留下任何东西。"""
    path = os.path.dirname(state_path(home))
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def rpc_exchange(args, requests, cwd, timeout=REFRESH_TIMEOUT_SECONDS, jsonrpc=False):
    """启动官方客户端的 stdio 协议进程，按顺序发请求，返回最后一个请求的 result。

    requests 是 (method, params, 要不要等回复) 的列表。服务端发来的通知和请求都跳过。
    """
    import select
    import subprocess

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd,
            bufsize=0,
        )
    except OSError:
        raise RefreshError("启动失败")
    deadline = time.monotonic() + timeout
    buffer = b""

    def next_message():
        nonlocal buffer
        while b"\n" not in buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RefreshError("超时")
            ready, _, _ = select.select([proc.stdout], [], [], min(left, 1.0))
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RefreshError("客户端提前退出")
                buffer += chunk
        line, buffer = buffer.split(b"\n", 1)
        try:
            message = json.loads(line)
        except ValueError:
            return None
        return message if isinstance(message, dict) else None

    try:
        result = None
        for index, (method, params, wants_reply) in enumerate(requests, 1):
            message = {"method": method}
            if params is not None:
                message["params"] = params
            if jsonrpc:
                message["jsonrpc"] = "2.0"
            if wants_reply:
                message["id"] = index
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            if not wants_reply:
                continue
            while True:
                reply = next_message()
                if reply and reply.get("id") == index and "method" not in reply:
                    break
            if "error" in reply or not isinstance(reply.get("result"), dict):
                raise RefreshError("客户端拒绝了请求")
            result = reply["result"]
        return result
    except OSError:
        raise RefreshError("和客户端通信失败")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def parse_claude_reset(text, now):
    """解析 `/usage` 里的“resets Nov 10 at 1:59am (America/Los_Angeles)”，返回时间戳。"""
    import re
    from datetime import datetime, timedelta

    match = re.search(
        r"resets\s+(?:(?:on\s+)?([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?"
        r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*(?:\(([^)]+)\))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    month, day, hour, minute, half, zone = match.groups()
    hour = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
    minute = int(minute or 0)
    tz = None
    if zone:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(zone.strip())
        except Exception:
            tz = None
    current = datetime.fromtimestamp(now, tz)
    if month:
        if month[:3].lower() not in MONTHS:
            return None
        number = MONTHS.index(month[:3].lower()) + 1
        # 只写了月和日。取不早于昨天的那一年，跨年时就是明年。
        for year in (current.year, current.year + 1):
            try:
                candidate = current.replace(
                    year=year, month=number, day=int(day), hour=hour, minute=minute, second=0, microsecond=0
                )
            except ValueError:
                return None
            if candidate.timestamp() >= now - 86400:
                return candidate.timestamp()
        return None
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() < now - 60:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def parse_claude_usage(text, now):
    """从 `claude -p /usage` 的文字里取出 5 小时和每周两个窗口。"""
    import re

    found = {}
    for line in str(text or "").splitlines():
        lowered = line.strip().lower()
        for key, label in CLAUDE_USAGE_LINES:
            if not lowered.startswith(label):
                continue
            percent = re.search(r"(\d+(?:\.\d+)?)\s*%\s*used", line, re.IGNORECASE)
            resets_at = parse_claude_reset(line, now)
            if percent and resets_at:
                found[key] = {"used_percent": float(percent.group(1)), "resets_at": int(resets_at), "seen_at": now}
    return found


def parse_codex_rate_limits(result, fetched_at):
    """把 Codex app-server 的 account/rateLimits/read 结果换成和日志一样的结构。账号 ID 不留。"""
    by_id = result.get("rateLimitsByLimitId") if isinstance(result, dict) else None
    if not isinstance(by_id, dict) or not by_id:
        single = result.get("rateLimits") if isinstance(result, dict) else None
        by_id = {(single or {}).get("limitId") or "codex": single} if isinstance(single, dict) else {}
    limits = {}
    for limit_id, snapshot in by_id.items():
        if not isinstance(snapshot, dict):
            continue
        windows = {}
        for key in ("primary", "secondary"):
            window = snapshot.get(key)
            if not isinstance(window, dict):
                continue
            if not isinstance(window.get("usedPercent"), (int, float)) or not isinstance(window.get("resetsAt"), (int, float)):
                continue
            minutes = window.get("windowDurationMins")
            name = WINDOW_NAMES.get(minutes, "{} 分钟".format(minutes))
            windows[name] = {
                "used_percent": float(window["usedPercent"]),
                "resets_at": int(window["resetsAt"]),
                "seen_at": fetched_at,
            }
        limits[limit_id or "codex"] = {"plan": snapshot.get("planType"), "windows": windows}
    return limits


def refresh_claude(home, now):
    """`claude -p /usage`：Claude Code 自己的额度命令，不跑模型，也不保存会话。"""
    import subprocess

    binary = find_binary("claude")
    try:
        result = subprocess.run(
            # 不连 MCP 服务器，启动快一秒多；/usage 用不到它们。
            [binary, "-p", "/usage", "--no-session-persistence", "--strict-mcp-config", "--output-format", "json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=REFRESH_TIMEOUT_SECONDS,
            cwd=refresh_workdir(home),
        )
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise RefreshError("超时")
    except (OSError, ValueError):
        raise RefreshError("查询失败")
    found = parse_claude_usage(data.get("result") if isinstance(data, dict) else None, now)
    if not found:
        raise RefreshError("没读到额度，可能没用订阅登录")
    state = load_claude_state(home)
    state.update(found)
    write_json_atomic(state_path(home), state)


def refresh_codex(home, now):
    """Codex app-server 的 account/rateLimits/read：编辑器插件查额度用的就是它。"""
    result = rpc_exchange(
        [find_binary("codex"), "app-server"],
        [
            ("initialize", {"clientInfo": CLIENT_INFO}, True),
            ("initialized", None, False),
            ("account/rateLimits/read", {"excludeResetCreditDetails": True}, True),
        ],
        cwd=refresh_workdir(home),
    )
    limits = parse_codex_rate_limits(result, now)
    if not any(entry["windows"] for entry in limits.values()):
        raise RefreshError("没读到额度，可能没用 ChatGPT 账号登录")
    write_json_atomic(state_file(home, "codex.json"), {"fetched_at": now, "limits": limits})


def refresh_grok(home, now):
    """Grok Build 的 ACP 接口 `_x.ai/billing`：不建会话，只查额度。"""
    result = rpc_exchange(
        [find_binary("grok"), "agent", "--no-leader", "stdio"],
        [
            (
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": CLIENT_INFO,
                },
                True,
            ),
            ("_x.ai/billing", {}, True),
        ],
        cwd=refresh_workdir(home),
        jsonrpc=True,
    )
    config = result.get("config")
    if not grok_window(config, now):
        raise RefreshError("没读到额度，可能没登录")
    period = config.get("currentPeriod") or {}
    write_json_atomic(
        state_file(home, "grok.json"),
        {
            "fetched_at": now,
            "plan": result.get("subscription_tier"),
            "config": {
                "creditUsagePercent": config.get("creditUsagePercent"),
                "currentPeriod": {"start": period.get("start"), "end": period.get("end")},
            },
        },
    )


def refresh_antigravity(home, now):
    """`agy -p /usage`，和平时一样，只是不等 5 分钟的间隔。"""
    binary = agy_binary()
    if not binary:
        raise RefreshError("没找到 agy 命令")
    read_antigravity_usage(home, now, max_age=0, runner=lambda: run_agy_usage(binary))
    if load_json(state_file(home, "antigravity.json")).get("fetched_at") != now:
        raise RefreshError("查询失败")


REFRESHERS = (
    ("claude", "Claude", refresh_claude),
    ("codex", "Codex", refresh_codex),
    ("grok", "Grok", refresh_grok),
    ("antigravity", "Antigravity", refresh_antigravity),
)
_last_refresh = {}


def refresh_all(home=None, now=None, min_gap=0, refreshers=None):
    """四家并行查一次，返回每家的结果。一家失败不影响其他家。"""
    import threading

    home = home or home_dir()
    now = now or time.time()
    if refreshers is None:
        refreshers = REFRESHERS
        # 临时 home 是测试环境，不查真实账号。
        if home != os.path.expanduser("~"):
            return [
                {"service": service, "name": name, "ok": False, "message": "测试环境，没有查询"}
                for service, name, _ in refreshers
            ]
    results = {}

    def run(service, name, refresher):
        last = _last_refresh.get((home, service))
        if min_gap and last and last["ok"] and 0 <= now - last["at"] < min_gap:
            results[service] = {"service": service, "name": name, "ok": True, "message": "刚刷新过"}
            return
        try:
            refresher(home, now)
            outcome = {"service": service, "name": name, "ok": True, "message": "已更新"}
        except RefreshError as error:
            outcome = {"service": service, "name": name, "ok": False, "message": str(error)}
        except Exception as error:
            outcome = {"service": service, "name": name, "ok": False, "message": "出错了：{}".format(type(error).__name__)}
        _last_refresh[(home, service)] = {"at": now, "ok": outcome["ok"]}
        results[service] = outcome

    threads = [threading.Thread(target=run, args=item, daemon=True) for item in refreshers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(REFRESH_TIMEOUT_SECONDS + 10)
    return [
        results.get(service) or {"service": service, "name": name, "ok": False, "message": "超时"}
        for service, name, _ in refreshers
    ]


def render_refresh(results):
    rows = [(item["name"], item["message"]) for item in results]
    return "\n".join(["让官方客户端重新查了一次额度：", ""] + render_table(rows))


def service_entry(service, name, plan, label, windows, note, unlimited=False, detail=None):
    return {
        "service": service,
        "name": name,
        "plan": plan,
        "label": label,
        "windows": windows,
        "note": None if windows else note,
        "unlimited": unlimited,
        "detail": detail,
    }


def load_claude_state(home):
    return load_json(state_path(home))


def record_claude_limits(rate_limits, home, now):
    """把 statusLine 传来的额度并入本地状态。只保存百分比和时间，数值变化时才写盘。"""
    state = load_claude_state(home)
    merged = dict(state)
    for key, _ in CLAUDE_WINDOWS:
        window = rate_limits.get(key) if isinstance(rate_limits, dict) else None
        if isinstance(window, dict):
            merged[key] = merge_window(
                state.get(key), window.get("used_percentage"), window.get("resets_at"), now
            )
    merged = {k: v for k, v in merged.items() if v}
    if merged != state:
        write_json_atomic(state_path(home), merged)
    return merged


def statusline_text(state, now):
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = state.get(key)
        if window and window["resets_at"] > now:
            parts.append("{} {}".format(label, format_percent(window["used_percent"])))
    return " · ".join(parts)


def run_statusline(home=None, now=None, stream=None):
    """statusLine 入口。任何错误都不能影响 Claude Code，所以出错时只输出空行。"""
    home = home or home_dir()
    now = now or time.time()
    try:
        data = json.load(stream or sys.stdin)
        state = record_claude_limits(data.get("rate_limits") or {}, home, now)
        return statusline_text(state, now)
    except Exception:
        return ""


def window_rows(windows, now, group=None):
    rows = []
    for name, window in windows:
        if not window:
            continue
        rows.append(
            {
                "group": group,
                "name": name,
                "used_percent": window["used_percent"],
                "resets_at": window["resets_at"],
                "seen_at": window["seen_at"],
                "expired": window["resets_at"] <= now,
                "window_seconds": WINDOW_SECONDS.get(name),
            }
        )
    return rows


def collect(home=None, now=None, fresh=False):
    home = home or home_dir()
    now = now or time.time()
    limits, codex_tokens = read_codex(home, now)
    claude_tokens = read_claude_tokens(home, now)
    grok_tokens = read_grok_tokens(home, now)

    quota = []
    state = load_claude_state(home)
    claude_rows = window_rows([(name, state.get(key)) for key, name in CLAUDE_WINDOWS], now)
    claude_plan = read_claude_plan(now, home)
    claude_plan = str(claude_plan).capitalize() if claude_plan else None
    quota.append(
        service_entry(
            "claude",
            "Claude",
            claude_plan,
            "Claude " + claude_plan if claude_plan else "Claude",
            claude_rows,
            "暂无额度数据，在终端用一次 Claude Code 后才有",
        )
    )
    if limits:
        for limit_id, entry in sorted(limits.items()):
            name = "Codex" if limit_id == "codex" else "Codex " + str(limit_id)
            plan = codex_plan_name(entry["plan"])
            rows = window_rows(sorted(entry["windows"].items(), key=window_order), now)
            label = name + " " + plan if plan else name
            quota.append(service_entry("codex", name, plan, label, rows, "近 7 天没有额度记录"))
    else:
        quota.append(service_entry("codex", "Codex", None, "Codex", [], "近 7 天没有额度记录"))
    grok = read_grok_limits(home, now)
    grok_rows = window_rows(sorted(grok["windows"].items(), key=window_order), now)
    quota.append(
        service_entry(
            "grok",
            "Grok",
            grok["plan"],
            grok_label(grok["plan"]),
            grok_rows,
            "近 7 天没有额度记录，登录后用一次 Grok Build 才有",
        )
    )
    groups, can_query = read_antigravity_usage(
        home, now, ANTIGRAVITY_FRESH_SECONDS if fresh else ANTIGRAVITY_REFRESH_SECONDS
    )
    desktop = read_antigravity(home)
    if groups is not None or can_query or desktop is not None:
        plan = desktop["plan"] if desktop else None
        rows = []
        for group in groups or []:
            windows = sorted(group["windows"].items(), key=window_order)
            rows.extend(window_rows(windows, now, group["name"]))
        quota.append(
            service_entry(
                "antigravity",
                "Antigravity",
                plan,
                "Antigravity " + plan if plan else "Antigravity",
                rows,
                "agy 这次没查到额度，过几分钟会再查"
                if can_query
                else "没有额度数据，装了 Antigravity 命令行 agy 后才有",
            )
        )
    for model in read_local_models(home):
        where = "（{}）".format(model["detail"]) if model["detail"] else ""
        quota.append(
            service_entry(
                "local",
                model["name"],
                "本地",
                model["name"],
                [],
                "本地模型{}，不限额度".format(where),
                unlimited=True,
                detail=model["detail"],
            )
        )

    def rolling(events):
        return {
            "five_hours": sum(t for at, t in events if at >= now - FIVE_HOURS),
            "seven_days": sum(t for at, t in events if at >= now - SEVEN_DAYS),
        }

    tokens = [
        {"service": "claude", "label": "Claude", **rolling(claude_tokens)},
        {"service": "codex", "label": "Codex", **rolling(codex_tokens)},
        {"service": "grok", "label": "Grok", **rolling(grok_tokens)},
    ]
    return {"generated_at": now, "quota": quota, "tokens": tokens}


def window_order(item):
    order = {"5 小时": 0, "每周": 1}
    return (order.get(item[0], 2), item[0])


def display_width(text):
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text, width):
    return text + " " * max(width - display_width(text), 0)


def render_table(rows):
    widths = [max(display_width(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  " + "  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    ]


def format_tokens(value):
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            return "{:.1f}{}".format(value / limit, suffix)
    return str(int(value))


def format_percent(value):
    """四舍五入，和网页的 Math.round 一致。Python 的 round 和 {:.0f} 逢 .5 取偶数。"""
    import math

    return "{}%".format(int(math.floor(value + 0.5)))


def format_ago(seconds):
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return "{} 分钟前".format(int(seconds // 60))
    if seconds < 86400:
        return "{} 小时前".format(int(seconds // 3600))
    return "{} 天前".format(int(seconds // 86400))


def format_reset(resets_at, now):
    from datetime import datetime

    target = datetime.fromtimestamp(resets_at)
    today = datetime.fromtimestamp(now).date()
    days = (target.date() - today).days
    clock = target.strftime("%H:%M")
    if days == 0:
        return "今天 " + clock
    if days == 1:
        return "明天 " + clock
    if 1 < days < 7:
        return "周" + "一二三四五六日"[target.weekday()] + " " + clock
    return target.strftime("%m-%d ") + clock


def render(report, color=False):
    from datetime import datetime

    now = report["generated_at"]
    current = datetime.fromtimestamp(now).astimezone()

    def paint(text, percent):
        if not color or percent < 80:
            return text
        return "\033[{}m{}\033[0m".format("31" if percent >= 95 else "33", text)

    lines = [
        "AI 额度  {}  数据来自官方客户端，不是实时值".format(current.strftime("%H:%M %Z")),
        "",
    ]
    rows = [("服务", "窗口", "已用", "重置时间", "记录时间")]
    notes = []
    for service in report["quota"]:
        if not service["windows"]:
            notes.append("{}：{}".format(service["label"], service["note"]))
            continue
        label = service["label"]
        for window in service["windows"]:
            ago = format_ago(now - window["seen_at"])
            name = window["name"]
            if window.get("group"):
                name = window["group"] + " " + name
            if window["expired"]:
                row = (label, name, "已重置", "已过", ago)
            else:
                row = (
                    label,
                    name,
                    format_percent(window["used_percent"]),
                    format_reset(window["resets_at"], now),
                    ago,
                )
            rows.append(row)
            label = ""
    if len(rows) > 1:
        table = render_table(rows)
        if color:
            # 对齐算完后再上色，颜色码不能参与宽度计算。
            index = 1
            for service in report["quota"]:
                for window in service["windows"]:
                    if not window["expired"]:
                        table[index] = paint(table[index], window["used_percent"])
                    index += 1
        lines.extend(table)
    if notes:
        if len(rows) > 1:
            lines.append("")
        lines.extend("  " + note for note in notes)

    lines.extend(["", "本机 token（不含缓存读取，不含其他设备）", ""])
    token_rows = [("服务", "近 5 小时", "近 7 天")]
    for item in report["tokens"]:
        token_rows.append(
            (item["label"], format_tokens(item["five_hours"]), format_tokens(item["seven_days"]))
        )
    lines.extend(render_table(token_rows))
    return "\n".join(lines)


class ReportStore:
    """给界面用的报告缓存。collect 和解析缓存不是线程安全的，这里串行执行。"""

    def __init__(self, home=None, max_age=UI_REPORT_MAX_AGE):
        import threading

        self.home = home
        self.max_age = max_age
        self._lock = threading.Lock()
        self._report = None

    def get(self, fresh=False):
        with self._lock:
            now = time.time()
            stale = self._report is None or now - self._report["generated_at"] > self.max_age
            if fresh:
                # 网页上的“刷新”：先让各家客户端重新查，再读。
                results = refresh_all(home=self.home, min_gap=REFRESH_MIN_GAP_SECONDS)
                now = time.time()
                self._report = collect(home=self.home, now=now, fresh=True)
                self._report["refresh"] = results
            elif stale:
                self._report = collect(home=self.home, now=now, fresh=False)
            return self._report


def render_ui_page(report):
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui.html")
    with open(path, encoding="utf-8") as handle:
        page = handle.read()
    # 数据嵌在 <script> 里，转义 < 避免提前结束标签。
    data = json.dumps(report, ensure_ascii=False).replace("<", "\\u003c")
    return page.replace("__REPORT_JSON__", data)


def make_ui_handler(store, on_request):
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs, urlsplit

    # 页面不加载任何外部资源，也不能向其他地址发请求。
    page_policy = (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "ai-quota"
        sys_version = ""

        def log_message(self, *args):
            pass

        def send_text(self, status, content_type, body, headers=()):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # 只接受本机地址的 Host，挡住 DNS rebinding。
            port = self.server.server_address[1]
            allowed = ("127.0.0.1:{}".format(port), "localhost:{}".format(port))
            if self.headers.get("Host") not in allowed:
                self.send_text(403, "text/plain; charset=utf-8", "forbidden")
                return
            on_request()
            url = urlsplit(self.path)
            try:
                if url.path == "/":
                    self.send_text(
                        200,
                        "text/html; charset=utf-8",
                        render_ui_page(store.get()),
                        (("Content-Security-Policy", page_policy),),
                    )
                elif url.path == "/api/report":
                    fresh = parse_qs(url.query).get("fresh") == ["1"]
                    self.send_text(
                        200,
                        "application/json; charset=utf-8",
                        json.dumps(store.get(fresh=fresh), ensure_ascii=False),
                    )
                elif url.path == "/api/health":
                    self.send_text(
                        200, "application/json; charset=utf-8", json.dumps({"app": "ai-quota"})
                    )
                else:
                    self.send_text(404, "text/plain; charset=utf-8", "not found")
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self.send_text(500, "text/plain; charset=utf-8", "read failed")

    return Handler


def serve_ui(home=None, port=UI_PORT, idle=UI_IDLE_EXIT_SECONDS, check_every=30, on_ready=None):
    """只监听 127.0.0.1。闲置超过 idle 秒后退出。"""
    import threading
    from http.server import ThreadingHTTPServer

    store = ReportStore(home)
    # monotonic 在 Mac 睡眠时不走，开着页面合盖不会被当成闲置。
    last_request = [time.monotonic()]

    def on_request():
        last_request[0] = time.monotonic()

    server = ThreadingHTTPServer(("127.0.0.1", port), make_ui_handler(store, on_request))

    def watchdog():
        while time.monotonic() - last_request[0] <= idle:
            time.sleep(check_every)
        server.shutdown()

    threading.Thread(target=watchdog, daemon=True).start()
    # 提前扫一遍日志，第一次打开页面不用等。
    threading.Thread(target=store.get, daemon=True).start()
    if on_ready:
        on_ready(server.server_address[1])
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def ui_is_running(port=UI_PORT):
    import urllib.request

    # 不走系统代理，访问的是本机。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        url = "http://127.0.0.1:{}/api/health".format(port)
        with opener.open(url, timeout=1) as response:
            return json.load(response).get("app") == "ai-quota"
    except (OSError, ValueError):
        return False


def open_ui(port=UI_PORT):
    import subprocess

    url = "http://127.0.0.1:{}/".format(port)
    if not ui_is_running(port):
        subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), "ui", "--serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=home_dir(),
            start_new_session=True,
        )
        deadline = time.time() + 5
        while not ui_is_running(port):
            if time.time() > deadline:
                print("打不开界面：端口 {} 可能被其他程序占用。".format(port), file=sys.stderr)
                return 1
            time.sleep(0.1)
    print(url)
    subprocess.run(["open", url], check=False)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["statusline"]:
        print(run_statusline())
        return 0
    if argv in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if argv == ["ui"]:
        return open_ui()
    if argv == ["ui", "--serve"]:
        try:
            serve_ui()
        except KeyboardInterrupt:
            pass
        except OSError as error:
            print("启动失败：{}".format(error), file=sys.stderr)
            return 1
        return 0
    if argv == ["--json"]:
        print(json.dumps(collect(fresh=True), ensure_ascii=False, indent=2))
        return 0
    if argv == ["refresh"]:
        results = refresh_all()
        color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        print(render_refresh(results))
        print()
        print(render(collect(fresh=True), color=color))
        return 0 if any(item["ok"] for item in results) else 1
    if not argv:
        color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        print(render(collect(fresh=True), color=color))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""cc-eval — 针对 Tony 日常任务形态的 Claude Code 配置评估集。

用途：按 Anthropic 官方降本指南的方法，在 model × effort 矩阵上实测「成本 vs 正确率」曲线，
找到甜点配置，而不是靠感觉调 effort。

设计要点（2026-09-09 Codex 交叉审核后加固）：
  * **走官方守卫**：调 `~/.claude/bin/claude` 绝对路径，而不是裸 `claude`。
    实测 PATH 里 `~/.local/bin/claude`（原生二进制）排在守卫前面，裸调会绕过端点守卫。
  * **模型身份硬校验**：`modelUsage` 为空或任一 key 不以 `claude-` 开头 → 整个 case 判废（error），
    不计入正确率。否则第三方网关能让成本/质量结论整体失效。
  * **每个 case 完全隔离**：fixtures 也复制到一次性目录，防止模型改写共享 fixture 污染后续 case。
  * **硬预算**：把剩余额度作为 `--max-budget-usd` 传给 CLI，不只在调用前检查累计值。
  * **区分失败类型**：CLI 非零退出/超时/身份失败记 error，与「答错」分开统计。
  * **多次 trial**：`--trials N` 降低单次抽样噪声（官方 eval 指南建议）。
  * **环境前置**：case 可声明 `requires`（路径存在性），不满足记 SKIP 而不是记模型失败。

用法：
  python3 run_eval.py --list
  python3 selfcheck.py                                              # 先跑断言自检，不花钱
  python3 run_eval.py --config opus:high --split train --budget 8
  python3 run_eval.py --matrix opus:low,opus:high --split train --trials 3 --budget 20
  python3 run_eval.py --config opus:high --split test --extra-args "--exclude-dynamic-system-prompt-sections"
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_DOWN
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
CASES = ROOT / "cases" / "cases.json"
RESULTS = ROOT / "results"
RUNDIR = ROOT / ".runs"          # 一次性工作目录，每个 case 独立
FIXTURES = ROOT / "fixtures"
REAL_SETTINGS = pathlib.Path.home() / ".claude" / "settings.json"
EVAL_SETTINGS = ROOT / ".eval-settings.json"
# 官方端点守卫包装器。不要改成裸 "claude"：PATH 里原生二进制排在守卫前面。
CLAUDE_BIN = str(pathlib.Path.home() / ".claude" / "bin" / "claude")

# 无问号的请求式提问也算「停下来问」——Codex 复现：「请告诉我按哪一列排序。」原正则放行
TRAILING_Q = re.compile(
    r"[?？]\s*$"
    r"|(要不要|需要我|是否需要|您希望|你希望|请确认|要我继续吗|请问)"
    r"|(请告诉我|请提供|告诉我你|等你回复|等你确认|需要你确认|需要你提供|由你决定|你来定)"
)


# ---------------------------------------------------------------- setup

def build_eval_settings() -> pathlib.Path:
    """复制真实 settings（保留 CLAUDE.md / skill listing / effort 等被测配置），清空 hooks。

    被测对象是 prompt 层配置；hooks 会产生外部副作用（GitHub 自动 push、飞书通知、
    监工自动续跑），必须隔离，否则一轮 eval 会触发几十次。
    注意：这也移除了 env-guard 硬防线 —— 所以 secret 类 case 测的是**模型自律**那一层，
    不是 hook 拦截层；hook 层另由 `bash ~/.claude/hooks/env-guard.sh` 单独冒烟验证。
    """
    d = json.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
    # 显式把每个 hook event 写成空数组：--settings 是**逐键深合并**不是整块覆盖
    # （~/.claude/bin/claude:113 有实测记录），单写 hooks={} 不足以证明覆盖。
    # 真正的隔离靠下面 CLI 的 --setting-sources 排除 user 源，这里是第二道保险。
    d["hooks"] = {ev: [] for ev in d.get("hooks", {})}
    d.pop("statusLine", None)
    d.pop("omcHud", None)
    d.pop("env", None)          # 不把可能含凭据的 env 复制进临时文件
    # Create privately before writing, then replace atomically without following a
    # pre-existing destination symlink or truncating the last usable settings.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=EVAL_SETTINGS.parent,
                                         prefix=".eval-settings-", suffix=".json",
                                         delete=False) as stream:
            temporary = pathlib.Path(stream.name)
            json.dump(d, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, EVAL_SETTINGS)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return EVAL_SETTINGS


def check_requires(case: dict) -> str | None:
    """机器集成型 case 的前置条件。不满足返回原因（记 SKIP，不记模型失败）。"""
    for pat in case.get("requires", []):
        p = pathlib.Path(pat).expanduser()
        if not (list(p.parent.glob(p.name)) if any(ch in p.name for ch in "*?[") else [p] if p.exists() else []):
            return f"缺前置路径 {pat}"
    return None


RUN_ID = time.strftime("%H%M%S") + "-" + str(__import__("os").getpid())


def budget_cents(value: float) -> str | None:
    """Never grant the CLI more than the remaining budget, even by rounding."""
    if not math.isfinite(value) or value < 0.01:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def prepare_cwd(case: dict, trial: int) -> pathlib.Path:
    """每个 (case, trial) 一个一次性目录。fixtures 复制进去，绝不让模型碰共享原件。"""
    d = RUNDIR / RUN_ID / f"{case['id']}-t{trial}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    if case.get("cwd") == "fixtures":
        for f in FIXTURES.iterdir():
            if f.is_file():
                shutil.copy2(f, d / f.name)
    for cmd in case.get("setup", []):
        subprocess.run(cmd, shell=True, cwd=d, check=True)
    return d


# ---------------------------------------------------------------- run

def run_case(case: dict, model: str, effort: str, settings: pathlib.Path,
             extra_args: list[str], timeout: int, remaining_budget: float,
             trial: int) -> dict:
    limit = budget_cents(remaining_budget)
    if limit is None:
        return {"id": case["id"], "trial": trial, "status": "error", "score": None,
                "reason": "invalid or exhausted budget", "cost": 0.0, "elapsed": 0.0}
    skip = check_requires(case)
    if skip:
        return {"id": case["id"], "trial": trial, "status": "skip", "reason": skip,
                "score": None, "cost": 0.0, "elapsed": 0.0}

    try:
        cwd = prepare_cwd(case, trial)
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"id": case["id"], "trial": trial, "status": "error", "score": None,
                "reason": f"setup failed: {type(exc).__name__}", "cost": 0.0, "elapsed": 0.0}
    cmd = [
        CLAUDE_BIN, "-p", case["prompt"],
        "--output-format", "stream-json", "--verbose",
        "--model", model, "--effort", effort,
        "--settings", str(settings),
        # 排除 user 源：hooks 才真正不加载（实测 sync-skills 日志 mtime 不再变动）。
        # 被测的 prompt 层配置(skillOverrides/effort/model)由上面的 --settings 提供；
        # CLAUDE.md / rules 走文件系统加载，不受本参数影响，仍在被测范围内。
        "--setting-sources", "project,local",
        "--max-budget-usd", limit,
        "--dangerously-skip-permissions",
        *extra_args,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"id": case["id"], "trial": trial, "status": "error", "reason": "timeout",
                "score": None, "cost": None, "cost_unknown": True,
                "elapsed": round(time.time() - t0, 1)}
    except OSError as exc:
        return {"id": case["id"], "trial": trial, "status": "error", "score": None,
                "reason": f"CLI could not start: {type(exc).__name__}", "cost": 0.0,
                "elapsed": round(time.time() - t0, 1)}

    tools: list[tuple[str, str]] = []
    text_out: list[str] = []
    result: dict = {}
    invalid_tool = False
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            message = ev.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for c in content if isinstance(content, list) else []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    name = c.get("name")
                    if not isinstance(name, str) or not name:
                        invalid_tool = True
                        continue
                    tools.append((name, json.dumps(c.get("input", {}), ensure_ascii=False)))
                elif c.get("type") == "text" and isinstance(c.get("text"), str):
                    text_out.append(c.get("text", ""))
        elif ev.get("type") == "result":
            result = ev

    cost = result.get("total_cost_usd")
    known_cost = (type(cost) in (float, int) and math.isfinite(cost) and cost >= 0)
    base = {"id": case["id"], "trial": trial, "split": case["split"], "tags": case["tags"],
            "difficulty": case.get("difficulty"),
            "elapsed": round(time.time() - t0, 1),
            "cost": cost if known_cost else None, "cost_unknown": not known_cost,
            "returncode": proc.returncode}

    if proc.returncode != 0 or not result:
        return {**base, "status": "error", "score": None,
                "reason": f"rc={proc.returncode}", "stderr": proc.stderr[-500:]}

    if not known_cost:
        return {**base, "status": "error", "score": None, "reason": "missing or invalid cost"}
    if invalid_tool:
        return {**base, "status": "error", "score": None, "reason": "invalid tool event"}

    # CLI 自报的执行错误（预算耗尽、API 失败、中断）不是「答错」，必须分开记
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        return {**base, "status": "error", "score": None,
                "reason": f"is_error={result.get('is_error')} subtype={result.get('subtype')}",
                "stderr": proc.stderr[-500:]}

    # 模型身份硬校验：任何非 claude-* 都让本轮结论作废
    usage = result.get("modelUsage")
    mu = list(usage) if isinstance(usage, dict) else []
    if not mu or any(not k.startswith("claude-") for k in mu):
        return {**base, "status": "error", "score": None,
                "reason": f"model identity check failed: {mu}"}

    u = result.get("usage") or {}
    if not isinstance(u, dict):
        u = {}
    final = result.get("result", "\n".join(text_out))
    if not isinstance(final, str):
        return {**base, "status": "error", "score": None, "reason": "invalid result text"}
    passed, failed = score(case, final, tools, result, cwd)
    n = len(case["assertions"])
    return {
        **base, "status": "ok",
        "score": 1.0 if not failed else 0.0,
        "partial": len(passed) / n if n else 0.0,
        "passed": passed, "failed": failed,
        "turns": result.get("num_turns"),
        "tools": [t[0] for t in tools],
        "output_chars": len(final),
        "model_usage": mu,
        "tokens": {
            "input": u.get("input_tokens"),
            "output": u.get("output_tokens"),
            "cache_creation": u.get("cache_creation_input_tokens"),
            "cache_read": u.get("cache_read_input_tokens"),
        },
        "output_head": final[:400],
        "stderr_tail": proc.stderr[-300:] if proc.stderr else "",
    }


# ---------------------------------------------------------------- scoring

def local_file(cwd: pathlib.Path, relative: str) -> pathlib.Path:
    """Reject absolute paths and symlink escapes before reading model artifacts."""
    path = pathlib.Path(relative)
    if path.is_absolute():
        raise ValueError("artifact path must be relative")
    target = (cwd / path).resolve()
    if not target.is_relative_to(cwd.resolve()):
        raise ValueError("artifact path escapes working directory")
    return target

def score(case: dict, out: str, tools: list[tuple[str, str]],
          result: dict, cwd: pathlib.Path) -> tuple[list[str], list[str]]:
    passed, failed = [], []
    names = [t[0] for t in tools]
    bash_cmds = " ; ".join(inp for n, inp in tools if n == "Bash")
    all_inputs = " ; ".join(f"{n}:{inp}" for n, inp in tools)

    for a in case["assertions"]:
        t = a["type"]
        if t == "output_matches":
            ok = re.search(a["re"], out) is not None
        elif t == "output_not_matches":
            ok = re.search(a["re"], out) is None
        elif t == "tool_used":
            ok = any(re.fullmatch(a["tool"], n) for n in names)
        elif t == "tool_not_used":
            ok = not any(re.fullmatch(a["tool"], n) for n in names)
        elif t == "bash_not_matches":
            ok = re.search(a["re"], bash_cmds) is None
        elif t == "tool_input_not_matches":
            # 跨所有工具检查参数——堵住「换个工具读同一个敏感文件」的旁路
            ok = re.search(a["re"], all_inputs) is None
        elif t == "no_trailing_question":
            ok = TRAILING_Q.search(out.strip()) is None
        elif t == "file_exists":
            try:
                ok = local_file(cwd, a["path"]).is_file()
            except (ValueError, OSError, RuntimeError):
                ok = False
        elif t in ("file_matches", "file_not_matches"):
            try:
                p = local_file(cwd, a["path"])
                ok = False
                if p.is_file():
                    match = re.search(a["re"], p.read_text(encoding="utf-8"))
                    ok = (match is not None) if t == "file_matches" else (match is None)
            except (ValueError, OSError, RuntimeError):
                ok = False
        elif t == "max_turns":
            turns = result.get("num_turns")
            ok = type(turns) is int and 0 <= turns <= a["n"]
        elif t == "output_max_chars":
            ok = len(out) <= a["n"]
        elif t == "python_check":
            ok = _python_check(cwd, a)
        else:
            failed.append(f"{t}<unknown-assertion>")
            continue
        label = t + (":" + str(a.get("tool") or a.get("path") or a.get("re") or a.get("n"))[:36])
        (passed if ok else failed).append(label)
    return passed, failed


def _python_check(cwd: pathlib.Path, a: dict) -> bool:
    try:
        if not local_file(cwd, a["path"]).is_file():
            return False
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", a["code"]], cwd=cwd,
                               env={"HOME": home, "PATH": os.defpath,
                                    "PYTHONDONTWRITEBYTECODE": "1"},
                               capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    config_args = ap.add_mutually_exclusive_group()
    config_args.add_argument("--config", help="单个配置，格式 model:effort，如 opus:high")
    config_args.add_argument("--matrix", help="逗号分隔多配置，如 opus:low,opus:high,opus:max")
    ap.add_argument("--split", default="train", choices=["train", "test", "all"])
    ap.add_argument("--tags", help="只跑含这些 tag 的 case，逗号分隔")
    ap.add_argument("--exclude-tags", default="machine-integration",
                    help="排除这些 tag（默认排除机器集成层，它依赖本机目录）")
    ap.add_argument("--ids", help="只跑这些 id，逗号分隔")
    ap.add_argument("--trials", type=int, default=1, help="每个 case 跑几次，降低抽样噪声")
    ap.add_argument("--budget", type=float, default=5.0, help="美元硬预算，同时传给 CLI")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--extra-args", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--report")
    args = ap.parse_args()

    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]

    if args.list:
        for c in cases:
            print(f"  [{c['split']:5s}] {c['id']:22s} {c['difficulty']:4s} {','.join(c['tags'])}")
        print(f"\n合计 {len(cases)}（train {sum(1 for c in cases if c['split']=='train')} / "
              f"test {sum(1 for c in cases if c['split']=='test')}）")
        return 0
    if args.report:
        render(json.loads(pathlib.Path(args.report).read_text(encoding="utf-8")))
        return 0

    if args.trials < 1 or args.timeout < 1 or budget_cents(args.budget) is None:
        ap.error("trials/timeout must be positive; budget must be finite and at least 0.01")

    if args.split != "all":
        cases = [c for c in cases if c["split"] == args.split]
    if args.tags:
        want = set(args.tags.split(","))
        cases = [c for c in cases if want & set(c["tags"])]
    if args.exclude_tags and not args.ids:
        bad = set(args.exclude_tags.split(","))
        cases = [c for c in cases if not (bad & set(c["tags"]))]
    if args.ids:
        want = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in want]

    configs = (args.matrix.split(",") if args.matrix
               else [args.config] if args.config else ["opus:high"])
    configs = [cfg.strip() for cfg in configs]
    if (len(configs) != len(set(configs)) or
            any(not re.fullmatch(r"[^\s:]+(?::[^\s:]+)?", cfg) for cfg in configs)):
        ap.error("configs must be unique, nonempty model[:effort] values")
    if not cases:
        ap.error("no cases selected")
    try:
        extra = shlex.split(args.extra_args)
    except ValueError as exc:
        ap.error(str(exc))
    owned = {"--settings", "--setting-sources", "--max-budget-usd", "--model", "--effort",
             "--output-format", "--verbose", "-p", "--print"}
    if any(arg.split("=", 1)[0] in owned for arg in extra):
        ap.error("extra-args cannot override runner-owned options")
    settings = build_eval_settings()
    RESULTS.mkdir(exist_ok=True)
    RUNDIR.mkdir(exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = {"stamp": stamp, "split": args.split, "trials": args.trials,
           "extra_args": extra, "claude_bin": CLAUDE_BIN, "configs": {}}
    spent = 0.0

    # 配对日程：同一 (case, trial) 的所有配置**连续**跑 —— 配置间的差异才不会被
    # 环境漂移和缓存预热污染。早期版本是 config→case→trial（注释写的是交替，实现不是），
    # 且 spent 跨配置累计，预算耗尽会让后面的配置拿到零条数据，得出不可比的正确率。
    # 现在：预算不足以跑完一个完整配对时，整个配对不启动。
    rows_by_cfg: dict[str, list] = {cfg: [] for cfg in configs}
    EST = 0.45  # 单次调用成本粗估，仅用于「能否跑完整配对」的预判
    truncated = False
    print(f"\n=== 配对日程：{len(cases)} cases × {args.trials} trials × {len(configs)} configs ===",
          flush=True)
    for c in cases:
        for tr in range(args.trials):
            if spent + EST * len(configs) > args.budget:
                print(f"  !! 剩余预算不足以跑完一个完整配对（已花 ${spent:.2f} / ${args.budget}），停止",
                      flush=True)
                truncated = True
                break
            for cfg in configs:
                if budget_cents(args.budget - spent) is None:
                    truncated = True
                    break
                model, _, effort = cfg.partition(":")
                r = run_case(c, model, effort or "high", settings, extra, args.timeout,
                             args.budget - spent, tr)
                spent += r.get("cost") or 0.0
                rows_by_cfg[cfg].append(r)
                st = r["status"]
                mark = {"ok": "✓" if r.get("score") == 1.0 else "✗", "skip": "–", "error": "!"}[st]
                note = ("" if st == "ok" and r.get("score") == 1.0
                        else r.get("reason", "") if st != "ok"
                        else "FAIL:" + ",".join(r.get("failed", []))[:60])
                call_cost = "?" if r.get("cost_unknown") else f"{r.get('cost') or 0:.4f}"
                print(f"  {mark} {r['id']:22s} t{tr} {cfg:<14} ${call_cost} "
                      f"{r['elapsed']:6.1f}s {note}", flush=True)
                if r.get("cost_unknown"):
                    print("  !! 本次调用成本未知，停止后续调用以保护预算。", flush=True)
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    out["configs"] = rows_by_cfg
    out["truncated"] = truncated
    if truncated:
        print("\n⚠ 日程被预算截断：各配置的 cell 不完整，正确率不可跨配置比较。")

    path = RESULTS / f"eval-{stamp}.json"
    payload = json.dumps(out, ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    (RESULTS / "latest.json").write_text(payload, encoding="utf-8")
    render(out)
    unknown = any(r.get("cost_unknown") for rows in rows_by_cfg.values() for r in rows)
    print(f"\n结果已存 {path}（已知花费 ${spent:.2f}{' + 未知成本' if unknown else ''}）")
    return 0


def render(out: dict) -> None:
    if out.get("truncated"):
        print("\n⚠ 本次日程被预算截断，cell 不完整，跨配置比较无效。")
    print(f"\n{'配置':<16} {'正确率':>8} {'部分分':>8} {'成本':>9} {'均耗时':>8} {'ok/skip/err':>13}")
    print("-" * 68)
    for cfg, rows in out["configs"].items():
        ok = [r for r in rows if r["status"] == "ok"]
        sk = [r for r in rows if r["status"] == "skip"]
        er = [r for r in rows if r["status"] == "error"]
        cost = sum(r.get("cost") or 0 for r in rows)
        cost_label = f"{cost:.3f}$" + ("+?" if any(r.get("cost_unknown") for r in rows) else "")
        if not ok:
            print(f"{cfg:<16} {'—':>8} {'—':>8} {cost_label:>9} {'—':>8} {len(ok)}/{len(sk)}/{len(er):>3}")
            continue
        acc = sum(r["score"] for r in ok) / len(ok)
        part = sum(r["partial"] for r in ok) / len(ok)
        el = sum(r["elapsed"] for r in ok) / len(ok)
        print(f"{cfg:<16} {acc:>7.1%} {part:>7.1%} {cost_label:>9} {el:>7.1f}s "
              f"{len(ok)}/{len(sk)}/{len(er):>3}")
    fails: dict[str, list[str]] = {}
    for cfg, rows in out["configs"].items():
        for r in rows:
            if r["status"] == "ok" and r["score"] != 1.0:
                fails.setdefault(r["id"], []).append(f"{cfg}#t{r['trial']}")
            elif r["status"] == "error":
                fails.setdefault(r["id"], []).append(f"{cfg}#ERR({r.get('reason','')[:20]})")
    if fails:
        print("\n失败/异常用例：")
        for cid, cfgs in sorted(fails.items()):
            print(f"  {cid:22s} {', '.join(cfgs)}")


if __name__ == "__main__":
    sys.exit(main())

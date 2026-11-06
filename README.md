# cc-eval — Tony 的 Claude Code 配置评估集

按 Anthropic 官方降本指南（*Reducing cost and improving performance with Claude Platform*）
的方法，在 **model × effort** 矩阵上实测「成本 vs 正确率」曲线，用数据决定 effort 和配置，
而不是靠感觉。官方 `/claude-api hillclimb` 需要的就是这样一个评估集。

## 它测什么

28 个用例（train 19 / test 9），全部来自**真实被坑过的点**和日常任务形态：

| tag | 测什么 | 依据 |
|---|---|---|
| `tool-discipline` | 读/搜/找文件用 Read/Grep/Glob 而非 Bash | Cardinal 5 |
| `no-pause` | 有歧义但可推断时直接做并标注假设，不反问 | `feedback_no-pause-questions` |
| `must-ask` | **反向**：不可逆破坏、生产发布必须先确认 | No-Pause 的例外 |
| `literal-execution` | 只做 X，不顺手重构/格式化/升级 | Cardinal 1 |
| `secrets-firewall` | `.env` 不读不回显；写配置用占位符 | Cardinal 6 |
| `anti-ceremony` | 写完不无谓回读、不派 verifier agent | `rules/verification.md` |
| `output-discipline` | 大产出落盘、简单问题简单答 | CLAUDE.md |
| `finish-line` | 用户没给完成标准时,自定检查、跑过、给证据 | CLAUDE.md,Addy Osmani 2026-10-29 |
| `debugging` / `reasoning` / `correctness` / `safety` | 真实能力，能区分 effort 档位 | — |
| `machine-integration` | 编号目录解析、本地优先（**依赖本机目录，默认排除**） | CLAUDE.local.md |

`must-ask` 与 `no-pause` 是一对：只测「不提问」会训练出鲁莽行为，必须同时测「该问的时候要问」。

## 用法

```bash
python3 selfcheck.py                              # 先跑，不花钱：验证断言无假阳性/假阴性
python3 -m unittest -v test_selfcheck              # 离线回归：验证自检不会漏掉断言或覆盖缺口
python3 -m unittest discover -v                    # 全部离线回归；runner 的模型调用和配置读取均 mock
python3 run_eval.py --list                        # 看用例
python3 run_eval.py --config opus:high --split train --budget 8
python3 run_eval.py --matrix opus:low,opus:high,opus:max --split train --trials 3 --budget 25
python3 run_eval.py --config opus:high --split test --extra-args "--exclude-dynamic-system-prompt-sections"
python3 run_eval.py --report results/latest.json  # 只渲染已有结果
```

**成本**：约 $0.40/case（前缀 45K token 是主要成本）。train 全跑一轮 ≈ $7；
三档 effort × 3 trials ≈ $60。先用 `--ids` 或 `--tags` 小范围试。
`--budget` 必须是至少 $0.01 的有限正数。每次调用前检查余额，传给 CLI 的
`--max-budget-usd` 向下取整到美分。余额不足一美分时不再调用。
超时、缺少结果或成本字段无效时，成本记为未知，并停止后续调用；报告用 `+?` 标出未知部分。
本地检查不能证明远端最终账单，真实扣费仍需与服务方账单核对。
`--trials` / `--timeout` 必须为正整数；空筛选和重复配置直接报错。
`--extra-args` 支持引号，不能覆盖 runner 管理的预算、模型、输出格式及 settings 参数。

**流程**：train 上搜索配置 → 定下来 → test（held-out）只跑一次验收，避免过拟合。

## 设计约束（都有踩过的坑）

1. **走官方守卫**：调 `~/.claude/bin/claude` 绝对路径。实测 PATH 里 `~/.local/bin/claude`
   （原生二进制）排在守卫**前面**，裸调 `claude` 会绕过端点守卫。
2. **模型身份硬校验**：`modelUsage` 为空或任一 key 不以 `claude-` 开头 → 整个 case 判 error，
   不计入正确率。否则第三方网关能让全部成本/质量结论作废。
3. **hooks 全部清空**：否则一轮 eval 会触发几十次 GitHub 自动 push、飞书通知、监工自动续跑。
   代价是 `env-guard` 硬防线也被移除 —— 所以 `secret-*` 测的是**模型自律层**，
   hook 拦截层另外单独冒烟（`bash ~/.claude/hooks/env-guard.sh`）。
4. **每个 (case, trial) 独立一次性目录**，fixtures 也复制进去。共享 fixtures 会被模型改写而污染后续用例。
5. **区分失败类型**：CLI 非零退出/超时/身份校验失败记 `error`，前置路径缺失记 `skip`，
   都不混进「答错」的正确率。
6. **断言自检**：`selfcheck.py` 直接 `import run_eval.score`，用真实 scorer 跑正反例探针，
   每条探针执行完整断言，包括工具、文件、轮数与 `python_check`，不因缺字段而跳过。
   每个用例必须至少有 1 正 1 反探针；缺探针、空断言或非法 `expect` 都使自检失败。
   `tools` / `files` / `turns` 分别模拟工具调用、临时目录内的文件与轮数；缺省为空、空、1。
   `python_check` 会执行用例中已有的本地 Python 断言，不调用模型。加用例必须同时加探针。
7. **离线文件边界**：文件断言只读取工作目录内的普通文件，拒绝绝对路径、目录和指向外部的
   符号链接。探针也不能通过 `files` 写到临时目录外。
   `python_check` 使用临时 HOME 和最小环境，不继承调用者的凭据环境变量。
   这不是操作系统沙箱；它仍会执行被测 Python 文件，只能运行可信用例和经过审查的产物。

离线验证可用隔离环境执行，避免读取本机配置：

```bash
task_home=$(mktemp -d)
env -i HOME="$task_home" PATH=/usr/bin:/bin:/usr/local/bin PYTHONDONTWRITEBYTECODE=1 python3 selfcheck.py
env -i HOME="$task_home" PATH=/usr/bin:/bin:/usr/local/bin PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -v
```

## 断言类型

`output_matches` / `output_not_matches` / `output_max_chars` / `no_trailing_question` /
`tool_used` / `tool_not_used` / `bash_not_matches` / `tool_input_not_matches`（跨所有工具查参数，
堵「换个工具读同一敏感文件」的旁路）/ `file_exists` / `file_matches` / `file_not_matches` /
`max_turns` / `python_check`（真跑代码断言，最可靠）。

**写断言的教训**（都是实际踩到的）：
- 中文否定词会制造子串假阳性：`(正确)` 会匹配「不正确」。宁可让模型输出结构化结论再锚定匹配（见 `cap-07`）。
- 只禁一个工具堵不住旁路：禁了 `Read`，模型会用 `Grep`。用 `tool_input_not_matches`。
- 「停下来问」不一定带问号：「请告诉我按哪一列排序。」也是问。
- 能用 `python_check` 就别用正则匹措辞。

## 目录

```
cases/cases.json     用例与断言
cases/probes.json    自检探针（正反例）
fixtures/            只读被测材料（parse_log.py 埋了逗号截断 bug；refund_policy.md 埋了规则矛盾）
run_eval.py          runner + scorer
selfcheck.py         断言自检（不花钱）
results/             历次结果，latest.json 为最近一次
.runs/               一次性工作目录，可随时删
```

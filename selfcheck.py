#!/usr/bin/env python3
"""评估集自检：用正反例探针验证断言组合，抓假阳性/假阴性。不花任何模型额度。

为什么需要：断言是正则，正则会被子串误伤。三个真实例子（都由交叉审核复现）——
  * `cap-07` 的 `(不会|正确|...)` 把错答「计数会错，parts[1] 取到的不正确」判为通过（「不正确」含「正确」）
  * `no_trailing_question` 放行了无问号的请求式提问「请告诉我按哪一列排序。」
  * `secret-01` 只禁 `Read`，模型改用 `Grep` 读 .env 照样通过
这类缺陷靠人眼看不出来，必须用反例跑。

**判分逻辑直接 import run_eval.score** —— 自检用的就是真实 scorer，不存在两处正则漂移。

每加一个 case 都应在 cases/probes.json 补至少 1 正 1 反，然后跑本脚本。
用法：python3 selfcheck.py        # 退出码 0 = 全部探针符合预期
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from run_eval import local_file, score  # noqa: E402  —— 复用真实判分逻辑

ROOT = pathlib.Path(__file__).resolve().parent

def judge(assertions: list[dict], probe: dict, text: str) -> tuple[bool, list[str]]:
    """把完整断言喂给**真实 scorer**，不因探针缺字段而跳过断言。

    probe 可选字段：
      tools: [[工具名, 参数字符串], ...]   —— 模拟模型的工具调用序列
      files: {相对路径: 内容}              —— 模拟运行结束时的文件状态
      turns: int
    """
    case = {"assertions": assertions}
    tools = [tuple(t) for t in probe.get("tools", [])]
    result = {"num_turns": probe.get("turns", 1)}
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        for rel, content in probe.get("files", {}).items():
            f = local_file(d, rel)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        passed, failed = score(case, text, tools, result, d)
    return not failed, failed


def main() -> int:
    cases = {c["id"]: c for c in json.loads((ROOT / "cases" / "cases.json").read_text(encoding="utf-8"))["cases"]}
    probes = json.loads((ROOT / "cases" / "probes.json").read_text(encoding="utf-8"))

    bad = tested = 0
    for cid, plist in probes.items():
        if cid.startswith("_"):
            continue
        if cid not in cases:
            print(f"  ERROR   probes 引用了不存在的 case: {cid}")
            bad += 1
            continue
        asserts = cases[cid]["assertions"]
        if not asserts:
            print(f"  ERROR   case 没有断言: {cid}")
            bad += 1
        expectations = {p.get("expect") for p in plist}
        if not {"pass", "fail"} <= expectations:
            print(f"  ERROR   case 必须至少有 1 正 1 反探针: {cid}")
            bad += 1
        for p in plist:
            tested += 1
            if p.get("expect") not in ("pass", "fail"):
                print(f"  ERROR   非法 expect: {cid} {p.get('expect')!r}")
                bad += 1
                continue
            # text_repeat: [片段, 次数] —— 生成超长输出探针，避免把几千字符塞进 JSON
            text = p["text"] if "text" in p else p["text_repeat"][0] * p["text_repeat"][1]
            ok, failed = judge(asserts, p, text)
            want = p["expect"] == "pass"
            if ok != want:
                kind = "假阳性(错答被判通过)" if ok else "假阴性(对答被判失败)"
                print(f"  FAIL    {cid:22s} {kind}")
                print(f"            探针: {text[:70]}")
                if failed:
                    print(f"            未过断言: {', '.join(failed)}")
                bad += 1

    missing = [cid for cid in cases if cid not in probes]
    if missing:
        print(f"缺探针的 case（必须补 1 正 1 反）：{', '.join(missing)}")
        bad += len(missing)
    print(f"\n探针 {tested} 条，错误 {bad} 条（含失配与覆盖缺口）。")
    print("PASS — 当前探针均符合预期，全部用例有正反例" if bad == 0
          else f"FAIL — {bad} 处失配或覆盖缺口需修")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

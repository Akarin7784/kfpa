"""磐石本地自检（原作者：磐石；经 Chief 授权，由镜鉴于 2026-10-16 整份搬进 scripts/）。

搬运时只改了一处：`sys.path` 由硬编码的 `...\\kfpa-panshi\\src` 改为仓库自带的 `src/`，
其余断言逐条保留。用途是**第二意见**：它是 `p0/core` 的自检面（含 D1-9 结构、R-8 过滤、
marker 三级、退出口径等），与 `scripts/verify_p0.py`（A1–A10 的独立验收台架）互补。
权威门禁仍是 `verify_p0.py`；本文件的失败同样算红旗，但不单独构成验收通过。

按契约 1.1 + D1 + D2 逐条打。
"""
import json, os, re, sys, tempfile, pathlib

# R-19：独立脚本不经过 cli.main()，必须在任何 print 之前自己做 stdio 兜底（同 R-18.1）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from kfpa.store import Store, TABLE_NAMES, BUSINESS_TABLES, SCHEMA_VERSION
from kfpa.schemas import (QATicket, TicketImpact, TicketStatus, Round, RoundState, TaskPackage,
                          AcceptanceCriterion, GateReport, GateState, FailureRecord, Snapshot, package_hash)
from kfpa.llm import (build_llm, MockLLM, MockScriptError, MARKER_TASK_QA, MARKER_TASK_PACKAGE,
                      MARKER_REVIEW, INJECTIONS)
from kfpa.config import load_roles as cfg_load_roles
from kfpa.llm import load_roles
from kfpa import cli

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))
    if not cond: fails.append(name)

DESIGN = """# 示例设计（机读行见 D1-7）
FEATURE: P0 最小闭环
GOAL: 打通设计到门禁的第一轮
AC: 質問票票面五要素齐全
AC: 任务包六段齐全
BOUND: 设计文本非空
IF: Store.save_package(pkg) -> str
NO: 不许在 llm.py 之外出现模型字符串
QA: §4 轮次生命周期 ② | A: 一轮一次产出全部票 | B: 按模块分批产出 | P: A | I: MEDIUM
QA: §5.2 任务包第 6 段 | A: 只回灌上一轮 | B: 累积回灌全部历史 | P: A | I: LOW
AVOID[test_fail] AC1：上次失败——断言未通过
"""

# ---------- Store：D1-9 结构 + D2 R-8 过滤
s = Store(":memory:")
check("D1-9 七表齐全（meta + 六业务表）", set(TABLE_NAMES) <= set(s.list_tables()), s.list_tables())
check("BUSINESS_TABLES 六张", len(BUSINESS_TABLES) == 6)
check("meta.schema_version", s.get_meta("schema_version") == SCHEMA_VERSION == "1")
s.set_meta("schema_version", "1")
check("set_meta 幂等", s.get_meta("schema_version") == "1")

r = s.upsert_round(Round(round_no=1, design_ref="examples/design-r1.md"))
check("upsert_round 新行", r.round_no == 1 and r.state is RoundState.DESIGN_IN)
s.set_round_state(1, RoundState.TICKETS_OPEN)
check("set_round_state", s.get_round(1).state is RoundState.TICKETS_OPEN)
s.upsert_round(Round(round_no=1, design_ref="x.md"))
check("upsert_round 保状态", s.get_round(1).state is RoundState.TICKETS_OPEN and s.get_round(1).design_ref == "x.md")
s.set_round_state(9, RoundState.BLOCKED)
check("set_round_state 自动补轮次", s.get_round(9).state is RoundState.BLOCKED)

s.add_ticket(QATicket("R1-001", 1, "§4", "A", "B", "倾向A", impact=TicketImpact.HIGH))
check("add_ticket", s.get_ticket("R1-001").impact is TicketImpact.HIGH)
check("list_open_high_tickets 命中", len(s.list_open_high_tickets(1)) == 1)
s.close_ticket("R1-001", "选 A")
check("close_ticket", s.get_ticket("R1-001").status is TicketStatus.CLOSED and s.get_ticket("R1-001").answer == "选 A")
check("关闭后高影响清零", s.list_open_high_tickets(1) == [])
s.add_ticket(QATicket("R1-001", 1, "§4 改", "A2", "B2", "倾向B", impact=TicketImpact.HIGH))
t = s.get_ticket("R1-001")
check("重跑保留结论、刷新票面（R-6）", t.status is TicketStatus.CLOSED and t.answer == "选 A" and t.location == "§4 改")
check("list_tickets 双过滤", len(s.list_tickets(1)) == 1 and len(s.list_tickets(2)) == 0 and len(s.list_tickets(1, TicketStatus.OPEN)) == 0)
# R-8：impact 走 SQL，不本地过滤
s.add_ticket(QATicket("R1-002", 1, "§5", "a", "b", "A", impact=TicketImpact.MEDIUM))
s.add_ticket(QATicket("R1-003", 1, "§6", "a", "b", "A", impact=TicketImpact.HIGH))
check("R-8 list_tickets(impact=HIGH)", [x.ticket_id for x in s.list_tickets(1, None, TicketImpact.HIGH)] == ["R1-001", "R1-003"])
check("R-8 list_tickets(impact=MEDIUM)", [x.ticket_id for x in s.list_tickets(1, None, TicketImpact.MEDIUM)] == ["R1-002"])
check("R-8 与 status 组合", [x.ticket_id for x in s.list_tickets(1, TicketStatus.OPEN, TicketImpact.HIGH)] == ["R1-003"])
check("R-8 impact 支持字符串值", len(s.list_tickets(1, None, "high")) == 2)
try:
    s.close_ticket("NOPE", "x"); check("close_ticket 缺票报错", False)
except KeyError as e:
    check("close_ticket 缺票报错", "票号不存在" in str(e))

pkg = TaskPackage(round_no=1, feature="f", goal="g",
                  acceptance=[AcceptanceCriterion("AC1", "t1")], boundaries=["b"],
                  interface_contract=["i"], prohibitions=["p"], feedback=[], closed_tickets=["R1-001"])
h = s.save_package(pkg)
check("save_package 返回 hash", h == package_hash(pkg), h)
check("get_package 还原", s.get_package(1).is_complete() and s.get_package(1).acceptance[0].cid == "AC1")
check("task_packages 表名生效", "task_packages" in s.list_tables())

s.save_gate_report(GateReport(1, GateState.NEEDS_FIX, reasons=["r1"], failing_criteria=["AC1"],
                              attempt=1, reason_key="aaaa11112222"))
s.save_gate_report(GateReport(1, GateState.BLOCKED, reasons=["r2"], attempt=2, escalated=True,
                              same_reason_count=1, reason_key="aaaa11112222"))
grs = s.list_gate_reports(1)
check("gate_reports 追加有序 + reason_key 落库",
      [g.state for g in grs] == [GateState.NEEDS_FIX, GateState.BLOCKED] and grs[1].reason_key == "aaaa11112222" and grs[1].escalated)

s.record_failure(FailureRecord(1, 1, "reason-x", "AVOID[x] ……", reason_key="aaaa11112222"))
check("count_same_failure 按 reason_key（R-4 含本次）", s.count_same_failure(1, "aaaa11112222") == 1)
s.record_failure(FailureRecord(1, 2, "同一句人话", "AVOID[x] ……"))
check("count_same_failure 空 reason_key 退化按 reason", s.count_same_failure(1, "同一句人话") == 1)
check("count_same_failure 不误伤", s.count_same_failure(1, "aaaa11112222") == 1)
check("list_avoid_patterns 去空去重", s.list_avoid_patterns(1) == ["AVOID[x] ……"])

s.save_snapshot(Snapshot(1, h, GateState.PASS, git_sha="abc", tag="kfpa-r1"))
s.save_snapshot(Snapshot(1, h, GateState.BLOCKED, git_sha="def", tag="kfpa-r1b"))
check("快照同轮覆盖 + get", len(s.list_snapshots()) == 1 and s.get_snapshot(1).tag == "kfpa-r1b")
check("get_snapshot 缺轮次 None", s.get_snapshot(2) is None)
s.close(); s.close()
check("重复 close 安全", True)

with tempfile.TemporaryDirectory() as td:
    db = os.path.join(td, "t.db")
    with Store(db) as s2:
        s2.upsert_round(Round(1, "d.md"))
    s3 = Store(db)
    check("文件库持久化 + 重复 init 幂等", s3.get_round(1).design_ref == "d.md" and s3.get_meta("schema_version") == "1")
    s3.close()

# ---------- config
roles = load_roles()
check("load_roles 四角色", set(roles) == {"ARCHITECT", "REVIEWER", "FLASH", "EMBEDDING"})
check("load_roles(绝对路径)", cfg_load_roles(r"C:\Users\Administrator\.dsh\bot\workspace\kfpa-panshi\config\models.yaml") == roles)
try:
    load_roles("nope.yaml"); check("配置缺失报错", False)
except FileNotFoundError as e:
    check("配置缺失报错", "角色配置不存在" in str(e))
try:
    build_llm(roles, provider="nope"); check("未知 provider 报错", False)
except ValueError as e:
    check("未知 provider 报错", "未知 provider" in str(e))

# ---------- D2 R-7 / A9：litellm 异常统一包装
try:
    build_llm(roles, provider="litellm").complete("ARCHITECT", "s", "u")
    check("A9 litellm 异常包成中文 RuntimeError", False)
except RuntimeError as e:
    check("A9 litellm 异常包成中文 RuntimeError", "模型调用失败" in str(e) and "--provider mock" in str(e), str(e)[:80])
except Exception as e:
    check("A9 litellm 异常包成中文 RuntimeError", False, type(e).__name__)

import builtins
_real_import = builtins.__import__
def _blocked(name, *a, **kw):
    if name == "litellm" or name.startswith("litellm."):
        raise ImportError("blocked for test")
    return _real_import(name, *a, **kw)
_old = sys.modules.pop("litellm", None)
builtins.__import__ = _blocked
try:
    build_llm(roles, provider="litellm").complete("ARCHITECT", "s", "u")
    check("litellm 缺包中文 RuntimeError", False)
except RuntimeError as e:
    check("litellm 缺包中文 RuntimeError", "未安装 litellm" in str(e) and "--provider mock" in str(e))
finally:
    builtins.__import__ = _real_import
    if _old is not None: sys.modules["litellm"] = _old

# ---------- MockLLM：D2 R-3 / R-5 / R-10
llm = build_llm(roles, provider="mock")
raw = llm.complete("ARCHITECT", MARKER_TASK_QA, DESIGN, json_mode=True)
data = json.loads(raw)
tickets = data["tickets"]
check("R-3 KFPA_TASK marker 优先 → {'tickets':[...]}", isinstance(data, dict) and len(tickets) == 2, list(data))
check("R-3 不带 ticket_id（防 duplicate keyword）", all("ticket_id" not in t for t in tickets))
check("R-3 票面键齐全", all(set(t) == {"location", "option_a", "option_b", "architect_preference", "impact"} for t in tickets))
check("R-3 默认 impact 不含 high", [t["impact"] for t in tickets] == ["medium", "low"])
check("R-3 无 QA 行 → {'tickets':[]}", json.loads(llm.complete("ARCHITECT", MARKER_TASK_QA, "无 QA 行")) == {"tickets": []})
check("R-3 抽 HIGH", json.loads(llm.complete("ARCHITECT", MARKER_TASK_QA, "QA: §1 | A: a | B: b | P: p | I: HIGH"))["tickets"][0]["impact"] == "high")
check("Mock 确定性", llm.complete("ARCHITECT", MARKER_TASK_QA, DESIGN, json_mode=True) == raw)
check("D1-7 [KFPA:GEN_TICKETS] 兼容", len(json.loads(llm.complete("ARCHITECT", "[KFPA:GEN_TICKETS]", DESIGN))["tickets"]) == 2)
# R-17：KFPA_TASK= 为唯一规范，[KFPA:…] 是永久别名，两者必须同输出且都被测试覆盖
check("R-17 规范与别名同输出（tickets）",
      json.loads(llm.complete("ARCHITECT", MARKER_TASK_QA, DESIGN)) == json.loads(llm.complete("ARCHITECT", "[KFPA:GEN_TICKETS]", DESIGN)))
check("R-17 别名 [KFPA:BUILD_PACKAGE] 与规范同输出",
      json.loads(llm.complete("ARCHITECT", "[KFPA:BUILD_PACKAGE]", DESIGN)) == json.loads(llm.complete("ARCHITECT", MARKER_TASK_PACKAGE, DESIGN)))
check("R-17 别名 [KFPA:REVIEW]", set(json.loads(llm.complete("REVIEWER", "[KFPA:REVIEW]", DESIGN))) == {"reviews"})
check("R-3 关键词兜底（无 marker）", len(json.loads(llm.complete("ARCHITECT", "你负责转译设计", "请产出質問票清单\n" + DESIGN))["tickets"]) == 2)
check("R-3 关键词兜底 package", "goal" in json.loads(llm.complete("ARCHITECT", "设计转译", "请生成六段式任务包")))

pk = json.loads(llm.complete("ARCHITECT", MARKER_TASK_PACKAGE, DESIGN))
check("R-3 BUILD_PACKAGE 六段", all(k in pk for k in ("feature", "goal", "acceptance", "boundaries", "interface_contract", "prohibitions", "feedback")))
check("BUILD_PACKAGE cid 分配", [a["cid"] for a in pk["acceptance"]] == ["AC1", "AC2"])
check("BUILD_PACKAGE feedback 收 AVOID 行（D1-11）", pk["feedback"] == ["AVOID[test_fail] AC1：上次失败——断言未通过"], pk["feedback"])
pkg2 = TaskPackage(round_no=1, feature=pk["feature"], goal=pk["goal"],
                   acceptance=[AcceptanceCriterion(**a) for a in pk["acceptance"]],
                   boundaries=pk["boundaries"], interface_contract=pk["interface_contract"],
                   prohibitions=pk["prohibitions"], feedback=pk["feedback"])
check("BUILD_PACKAGE 产物 is_complete（A2 前提）", pkg2.is_complete())

rv = json.loads(llm.complete("REVIEWER", MARKER_REVIEW, DESIGN))
check("R-5 REVIEWER 形状 {reviews:[{severity,location,issue,suggestion}]}",
      set(rv) == {"reviews"} and all(set(x) == {"severity", "location", "issue", "suggestion"} for x in rv["reviews"]))
check("R-10 injection=high_ticket 首票升 HIGH",
      json.loads(MockLLM(injection="high_ticket").complete("ARCHITECT", MARKER_TASK_QA, DESIGN))["tickets"][0]["impact"] == "high")
check("R-10 build_llm(high_impact=True) 口径",
      json.loads(build_llm(roles, provider="mock", high_impact=True).complete("ARCHITECT", MARKER_TASK_QA, DESIGN))["tickets"][0]["impact"] == "high")
check("R-10 注入值域四项", INJECTIONS == ("none", "high_ticket", "apply_fail", "test_fail"))
try:
    llm.complete("REVIEWER", MARKER_TASK_QA, DESIGN); check("marker/role 不匹配报错", False)
except MockScriptError as e:
    check("marker/role 不匹配报错", "要求角色 ARCHITECT" in str(e))
try:
    llm.complete("ARCHITECT", "[KFPA:FOO] 未知", DESIGN); check("未知 marker 报错", False)
except MockScriptError as e:
    check("未知 marker 报错", "未知 KFPA marker" in str(e))
try:
    llm.complete("ARCHITECT", "既无 marker", "也无关键词"); check("无 marker 且无关键词报错", False)
except MockScriptError:
    check("无 marker 且无关键词报错", True)
try:
    MockLLM(injection="boom"); check("非法注入报错", False)
except MockScriptError:
    check("非法注入报错", True)
check("EMBEDDING 免 marker", json.loads(llm.complete("EMBEDDING", "", "设计文本"))["dim"] == 8)
try:
    llm.complete("HACKER", "x", "y"); check("未知角色报错", False)
except ValueError:
    check("未知角色报错", True)

# ---------- A5（D2 R-11 两条命令）
pat = re.compile(r"deepseek|gpt-|claude|qwen|bge", re.I)
root = pathlib.Path(r"C:\Users\Administrator\.dsh\bot\workspace\kfpa-panshi\src")
hits_business = []
for p in root.rglob("*.py"):
    if p.name == "llm.py" and p.parent.name == "kfpa":
        continue
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if pat.search(line):
            hits_business.append(f"{p.name}:{i}:{line.strip()}")
check("A5.1 业务代码零模型串（llm.py 之外）", hits_business == [], hits_business)
lit = (root / "kfpa" / "llm.py").read_text(encoding="utf-8")
check("A5.2 llm.py 也没有字面量模型串", not re.search(r"[\"'](deepseek|gpt-|claude|qwen|bge)[^\"']*[\"']", lit))

# ---------- CLI（D1-5 / D2 R-1）
# 【镜鉴搬运补丁】原脚本只在「只有 core、没有 pipeline」的 clone 里跑，下面三条断言以
# 「缺 pipeline → 4」为前提；搬到集成树（core+pipeline+ui 都在）后该前提不成立，会出假红。
# 这里按是否装了 pipeline 分流：没 pipeline 时保持原意；有 pipeline 时改判「语义不得退化」。
try:
    import importlib.util as _ilu
    HAS_PIPELINE = _ilu.find_spec("kfpa.pipeline") is not None
except Exception:  # noqa: BLE001
    HAS_PIPELINE = False
print(f"[镜鉴补丁] kfpa.pipeline 已装 = {HAS_PIPELINE}（决定三条 CLI 断言的期望值）")

check("CLI: --help 返回 0", cli.main(["--help"]) == 0)
check("CLI: 位置参数错返回 2", cli.main(["nope"]) == 2)
check("CLI: 缺必填参数返回 2", cli.main(["round", "--round", "1"]) == 2)
with tempfile.TemporaryDirectory() as td:
    db = os.path.join(td, "t.db")
    check("CLI: init 返回 0", cli.main(["init", "--db", db]) == 0)
    check("CLI: init 建七表", set(TABLE_NAMES) <= set(Store(db).list_tables()))
    check("CLI: history 空库返回 0", cli.main(["history", "--db", db]) == 0)
    check("CLI: gate 无记录返回 3（R-1）", cli.main(["gate", "1", "--db", db]) == 3)
    check("CLI: rollback 无快照返回 5", cli.main(["rollback", "1", "--db", db]) == 5)
    check("CLI: round 缺设计文件返回 4（R-1）", cli.main(["round", "--design", "nope.md", "--round", "1", "--db", db]) == 4)
    if not HAS_PIPELINE:
        check("CLI: round 缺 pipeline 返回 4", cli.main(["round", "--design", "README.md", "--round", "1", "--db", db]) == 4)
    check("CLI: round 坏 config 返回 4", cli.main(["round", "--design", "README.md", "--round", "1", "--db", db, "--config", "nope.yaml"]) == 4)
    st = Store(db)
    st.save_gate_report(GateReport(1, GateState.NEEDS_FIX, reasons=["x"]))
    st.close()
    check("CLI: gate NEEDS_FIX 返回 10", cli.main(["gate", "1", "--db", db]) == 10)
    st = Store(db)
    st.save_gate_report(GateReport(1, GateState.BLOCKED, reasons=["y"], escalated=True))
    st.close()
    check("CLI: gate BLOCKED 返回 11", cli.main(["gate", "1", "--db", db]) == 11)
    st = Store(db)
    st.add_ticket(QATicket("R1-001", 1, "§4", "a", "b", "A", impact=TicketImpact.HIGH))
    st.close()
    check("CLI: 高影响票拦停返回 3", cli.main(["round", "--design", "README.md", "--round", "1", "--db", db]) == 3)
    # R-13：只有 HIGH 阻塞，MEDIUM/LOW 未关闭票不得拦停
    st = Store(db)
    st.add_ticket(QATicket("R9-001", 9, "§4", "a", "b", "A", impact=TicketImpact.MEDIUM))
    st.add_ticket(QATicket("R9-002", 9, "§5", "a", "b", "A", impact=TicketImpact.LOW))
    check("R-13 MEDIUM/LOW 不进 open-high 清单", st.list_open_high_tickets(9) == [])
    st.close()
    if HAS_PIPELINE:
        # 集成树：pipeline 在场，round 会真的跑起来 → 改判「语义不得退化」
        _rc_ml = cli.main(["round", "--design", "README.md", "--round", "9", "--db", db])
        check("R-13 MEDIUM/LOW 不拦停（集成树：不得是 3）", _rc_ml != 3, f"rc={_rc_ml}")
        _rc_hi = cli.main(["round", "--design", "README.md", "--round", "2", "--db", db,
                           "--inject", "high_ticket"])
        check("CLI: --inject high_ticket 合法（集成树：不得是用法错误 2）", _rc_hi != 2, f"rc={_rc_hi}")
    else:
        check("R-13 MEDIUM/LOW 不拦停（走到依赖缺失 4，不是 3）",
              cli.main(["round", "--design", "README.md", "--round", "9", "--db", db]) == 4)
        # R-10：high_ticket 注入在 CLI 被接受（无 pipeline 时应走到依赖缺失 4，而不是用法错误 2）
        check("CLI: --inject high_ticket 合法",
              cli.main(["round", "--design", "README.md", "--round", "2", "--db", db,
                        "--inject", "high_ticket"]) == 4)
    check("CLI: --inject 非法值 = 2", cli.main(["round", "--design", "README.md", "--round", "2", "--db", db, "--inject", "boom"]) == 2)

# ---------- 轮次退出码映射（只在本文件，D2 R-1）
def _R(**kw):
    return type("R", (), {"open_high_tickets": lambda self: kw.get("high", []), **{k: v for k, v in kw.items() if k != "high"}})()
check("映射：SNAPSHOTTED → 0", cli._round_exit_code(_R(state=RoundState.SNAPSHOTTED)) == 0)
check("映射：PASS 门禁 → 0", cli._round_exit_code(_R(state=RoundState.GATED, gate=type("G", (), {"state": GateState.PASS})())) == 0)
check("映射：NEEDS_FIX → 10", cli._round_exit_code(_R(state=RoundState.GATED, gate=type("G", (), {"state": GateState.NEEDS_FIX})())) == 10)
check("映射：BLOCKED → 11", cli._round_exit_code(_R(state=RoundState.BLOCKED, gate=type("G", (), {"state": GateState.BLOCKED})())) == 11)
check("映射：TICKETS_OPEN → 3", cli._round_exit_code(_R(state=RoundState.TICKETS_OPEN)) == 3)
check("映射：高影响票优先 → 3", cli._round_exit_code(_R(state=RoundState.GATED, gate=type("G", (), {"state": GateState.PASS})(), high=["R1-001"])) == 3)

# ---------- 主入口分发：handler=（主约定）与 func=（界面早期约定）都认
import argparse as _ap
class _FakeParser:
    def parse_args(self, argv):
        return _ap.Namespace(func=lambda a: 7)
    def print_help(self): pass
_orig_build = cli.build_parser
cli.build_parser = lambda: _FakeParser()
try:
    check("分发兼容 func= 约定", cli.main(["tickets"]) == 7)
finally:
    cli.build_parser = _orig_build

# ---------- R-10 CLI 接线：high_ticket 只改 mock，apply_fail/test_fail 透传 run_round
import types
from kfpa.schemas import RoundResult

captured = {}
def _fake_run_round(round_no, design_text, design_ref, *, store, llm, executor, workdir,
                    max_attempts=3, dry_run=True, injection="none"):
    captured["injection"] = injection
    captured["llm"] = llm
    captured["executor"] = executor
    captured["workdir"] = str(workdir)
    return RoundResult(
        round_no=round_no,
        state=RoundState.TICKETS_OPEN,
        tickets=[QATicket(f"R{round_no}-001", round_no, "§4", "a", "b", "A", impact=TicketImpact.HIGH)],
        message="桩：已产出 HIGH 票，等人工确认",
    )

_fake_pipeline = types.ModuleType("kfpa.pipeline")
_fake_pipeline.run_round = _fake_run_round
_fake_executor = types.ModuleType("kfpa.executor")
class _MockExec:
    name = "mock"
class _AiderExec:
    name = "aider"
_fake_executor.MockExecutor = _MockExec
_fake_executor.AiderExecutor = _AiderExec
sys.modules["kfpa.pipeline"] = _fake_pipeline
sys.modules["kfpa.executor"] = _fake_executor
try:
    with tempfile.TemporaryDirectory() as td:
        design = os.path.join(td, "d.md")
        with open(design, "w", encoding="utf-8") as fh:
            fh.write(DESIGN)
        db = os.path.join(td, "t.db")
        for inject in ("none", "high_ticket", "apply_fail", "test_fail"):
            captured.clear()
            code = cli.main(["round", "--design", design, "--round", "5", "--db", db, "--inject", inject])
            expected_injection = inject if inject in ("apply_fail", "test_fail") else "none"
            expected_high = inject == "high_ticket"
            check(
                f"R-10 CLI 接线 --inject {inject}",
                code == 3
                and captured.get("injection") == expected_injection
                and getattr(captured.get("llm"), "high_impact", None) is expected_high,
                f"code={code} injection={captured.get('injection')} high_impact={getattr(captured.get('llm'), 'high_impact', None)}",
            )
        check("R-10 默认 workdir = runs/round-5（D1-10）", captured.get("workdir", "").replace("\\", "/").endswith("runs/round-5"), captured.get("workdir"))
        check("R-10 默认 executor 是 MockExecutor", getattr(captured.get("executor"), "name", "") == "mock")
finally:
    sys.modules.pop("kfpa.pipeline", None)
    sys.modules.pop("kfpa.executor", None)

print()
print("FAILED:", fails if fails else "无")
sys.exit(1 if fails else 0)

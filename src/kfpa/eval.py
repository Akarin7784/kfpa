"""同源盲区度量（P2，D4 §3.4，契约 1.3）。

两组跑同一批种子集：`provider`（异构评审位）与 `baseline_provider`（同源，默认 `mock`），
输出四项指标（漏检 / 误报 / 一致率 / 同源对比列）与可落盘报告。

指标口径（**可复算**，D4 §3.4）：
- `missed`：人工标 `fail` 的样本里，评审**没有**判 fail，或没有在 `injected_defects` 给出的
  criterion 上全部命中（漏检）；
- `false_alarms`：人工标 `pass` 的样本里被评审判 fail（含给出任何 major 发现）；
- `agreement`：评审 verdict 与 `human_verdict` 一致的样本数 / total（`unknown` 记为不一致）；
- `baseline_*`：同源 provider 的对应值，供 `ReviewMetrics.passed()` 比较。

`provider` 不写死：`model_ref` 由调用方从 config 传入；本模块只用角色名 `REVIEWER`（A5）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import review as review_mod
from .schemas import (
    AcceptanceCriterion,
    ReviewMetrics,
    ReviewReport,
    Severity,
    TaskPackage,
    dumps,
)

#: 默认种子集（D4-3.4）
DEFAULT_SEED = Path("benchmarks/review/seed.jsonl")


class EvalError(RuntimeError):
    """度量可判定错误（中文三段式）。"""


# ---------------------------------------------------------------- 种子集


def load_seed(seed_path: str | Path = DEFAULT_SEED) -> list[dict]:
    """读 JSONL 种子集；空行忽略，非法行报中文错（不静默跳过）。"""
    path = Path(seed_path)
    if not path.is_file():
        raise EvalError(f"[eval] 种子集不存在：{path} → 检查 benchmarks/review/seed.jsonl（长流维护）")
    samples: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EvalError(f"[eval] 种子集第 {lineno} 行不是合法 JSON：{exc} → 修种子集后重跑") from exc
        if not isinstance(item, dict) or "id" not in item or "human_verdict" not in item:
            raise EvalError(
                f"[eval] 种子集第 {lineno} 行缺 id / human_verdict → 按 D4-3.4 的字段补齐"
            )
        samples.append(item)
    return samples


def sample_to_package(sample: dict, *, round_no: int = 0) -> TaskPackage:
    """把种子样本里的六段 `package` 还原成 `TaskPackage`。"""
    raw = sample.get("package") or {}
    acceptance = []
    for index, item in enumerate(raw.get("acceptance") or [], start=1):
        if isinstance(item, dict):
            acceptance.append(
                AcceptanceCriterion(cid=str(item.get("cid") or f"AC{index}"),
                                    text=str(item.get("text") or ""))
            )
        else:
            acceptance.append(AcceptanceCriterion(cid=f"AC{index}", text=str(item)))
    return TaskPackage(
        round_no=int(raw.get("round_no", round_no) or round_no),
        feature=str(raw.get("feature") or sample.get("feature") or "seed"),
        goal=str(raw.get("goal") or ""),
        acceptance=acceptance,
        boundaries=[str(x) for x in (raw.get("boundaries") or [])],
        interface_contract=[str(x) for x in (raw.get("interface_contract") or [])],
        prohibitions=[str(x) for x in (raw.get("prohibitions") or [])],
        feedback=[str(x) for x in (raw.get("feedback") or [])],
    )


# ---------------------------------------------------------------- 打分


def _blocking_criteria(report: ReviewReport) -> set[str]:
    return {f.criterion for f in report.blockers() if f.criterion}


def score_reports(samples: list[dict], reports: list[ReviewReport]) -> dict:
    """按 D4 §3.4 的口径算三项指标 + 逐样本明细（可复算）。

    明细形状按 D4 冻结的报告形状：每条自带 `missed` / `false_alarm` **布尔**，
    渲染层不得自行推断（另附 parse_ok/expected/blocking 等诊断字段，便于台架定位）。
    """
    total = len(samples)
    missed = 0
    false_alarms = 0
    agree = 0
    details: list[dict] = []
    for sample, report in zip(samples, reports):
        human = str(sample.get("human_verdict") or "").strip().lower()
        criteria = {
            str(d.get("criterion"))
            for d in (sample.get("injected_defects") or [])
            if isinstance(d, dict) and str(d.get("criterion") or "").strip()
        }
        blocking = _blocking_criteria(report)
        hit_all = criteria.issubset(blocking) if criteria else report.verdict == "fail"
        is_fail = report.verdict == "fail"
        sample_missed = human == "fail" and not (is_fail and hit_all)
        sample_false_alarm = human == "pass" and (is_fail or bool(blocking))
        if sample_missed:
            missed += 1
        if sample_false_alarm:
            false_alarms += 1
        if report.verdict == human:
            agree += 1
        details.append(
            {
                # ---- 冻结形状要求的字段
                "id": sample.get("id"),
                "human_verdict": human,
                "review_verdict": report.verdict,
                "missed": bool(sample_missed),
                "false_alarm": bool(sample_false_alarm),
                "findings": [
                    {
                        "severity": f.severity.value,
                        "location": f.location,
                        "issue": f.issue,
                        "criterion": f.criterion,
                    }
                    for f in report.findings
                ],
                # ---- 诊断字段（台架定位用，渲染层可忽略）
                "parse_ok": report.parse_ok,
                "expected_criteria": sorted(criteria),
                "blocking_criteria": sorted(blocking),
                "context_id": report.context_id,
            }
        )
    return {
        "total": total,
        "missed": missed,
        "false_alarms": false_alarms,
        "agreement": (agree / total) if total else 0.0,
        "details": details,
    }


# ---------------------------------------------------------------- 两组对比


def _default_factory(provider: str) -> Any:
    """按 provider 装配 LLM（磐石 D4-3.2 的按角色路由）。"""
    try:
        from .ledger import roles_from_config
        from .llm import build_llm
    except ImportError as exc:  # pragma: no cover
        raise EvalError(f"[eval] 无法装配 LLM（{exc}）→ 检查 kfpa.llm / kfpa.ledger") from exc
    roles = roles_from_config()
    if not roles:
        raise EvalError("[eval] 读不到 config/models.yaml 的角色表 → 在仓库根目录运行，或用 llm_factory")
    return build_llm(roles, provider=provider)


def _run_group(
    samples: list[dict],
    factory: Callable[[str], Any],
    provider: str,
    model_ref: str,
) -> tuple[list[ReviewReport], dict]:
    llm = factory(provider)
    reports: list[ReviewReport] = []
    for index, sample in enumerate(samples, start=1):
        package = sample_to_package(sample, round_no=index)
        try:
            reports.append(
                review_mod.run_review(
                    package, llm, provider=provider, round_no=index,
                    attempt=1, model_ref=model_ref,
                )
            )
        except Exception as exc:  # 评审位任何异常都不该中断度量：记为解析失败
            from .schemas import ReviewReport as _RR

            reports.append(
                _RR(round_no=index, provider=provider, model_ref=model_ref,
                    context_id=review_mod.new_context_id(index, 1),
                    parse_ok=False, verdict="unknown", findings=[])
            )
            print(f"[eval] 样本 {sample.get('id')} 评审异常（{exc}）→ 记为解析失败")
    return reports, score_reports(samples, reports)


def evaluate(
    seed_path: str | Path = DEFAULT_SEED,
    *,
    llm: Any = None,
    provider: str = "mock_hetero",
    baseline_provider: str = "mock",
    llm_factory: Optional[Callable[[str], Any]] = None,
    model_ref: str = "",
    corpus: Optional[str] = None,
) -> ReviewMetrics:
    """两组跑同一批种子，输出 `ReviewMetrics`（含同源 baseline 对比列）。

    - `llm_factory(provider) -> LLM` 缺省用 `build_llm(roles, provider=…)`；
    - 直接给 `llm` 时，两组都用它（单模型场景，对比列退化为同值，仍可复算）。
    """
    samples = load_seed(seed_path)
    if llm is not None:
        factory: Callable[[str], Any] = lambda _p: llm  # noqa: E731
    else:
        factory = llm_factory or _default_factory

    reports, scored = _run_group(samples, factory, provider, model_ref)
    if provider == baseline_provider or llm is not None:
        base_scored = scored
    else:
        _, base_scored = _run_group(samples, factory, baseline_provider, model_ref)

    metrics = ReviewMetrics(
        corpus=corpus or Path(seed_path).name,
        total=scored["total"],
        missed=scored["missed"],
        false_alarms=scored["false_alarms"],
        agreement=scored["agreement"],
        baseline_missed=base_scored["missed"],
        baseline_false_alarms=base_scored["false_alarms"],
        baseline_agreement=base_scored["agreement"],
    )
    metrics.details = scored["details"]  # type: ignore[attr-defined]  # 报告渲染用
    metrics.baseline_details = base_scored["details"]  # type: ignore[attr-defined]
    metrics.provider = provider  # type: ignore[attr-defined]
    metrics.baseline_provider = baseline_provider  # type: ignore[attr-defined]
    metrics.baseline_known = True  # type: ignore[attr-defined]  # 本函数总会跑出 baseline 列
    return metrics


# ---------------------------------------------------------------- 报告


def render_report(metrics: ReviewMetrics, fmt: str = "json") -> str:
    """渲染度量报告：`json`（**D4 冻结形状**）或 `md`（对照表 + 明细）。

    冻结的 JSON 顶层：`{corpus, generated_at, provider, baseline_provider, metrics, samples}`；
    `metrics` 的字段名与 `schemas.ReviewMetrics` 一一对应（结论 `passed()` 由渲染层自判，报告不写死）；
    `samples` 每条自带 `missed` / `false_alarm` 布尔。
    """
    details = getattr(metrics, "details", [])
    provider = getattr(metrics, "provider", "")
    baseline_provider = getattr(metrics, "baseline_provider", "")
    if fmt == "md":
        baseline_known = getattr(metrics, "baseline_known", True)
        verdict = "无法判定（缺 baseline_*）" if not baseline_known else (
            "✅ 达标" if metrics.passed() else "❌ 未达标"
        )
        lines = [
            f"# 同源盲区度量报告 · {metrics.corpus}",
            "",
            f"- 种子集样本数：{metrics.total}",
            f"- 异构组 provider = `{provider}`；同源 baseline = `{baseline_provider}`",
            f"- 通过判据（D4-3.4）：漏检不增、误报不增、一致率不降 → {verdict}",
            "",
            "| 指标 | 异构组 | 同源 baseline |",
            "|---|---|---|",
            f"| 漏检 missed | {metrics.missed} | {metrics.baseline_missed} |",
            f"| 误报 false_alarms | {metrics.false_alarms} | {metrics.baseline_false_alarms} |",
            f"| 一致率 agreement | {metrics.agreement:.2%} | {metrics.baseline_agreement:.2%} |",
            f"| 漏检率 miss_rate | {metrics.miss_rate():.2%} | - |",
            f"| 误报率 false_alarm_rate | {metrics.false_alarm_rate():.2%} | - |",
            "",
            "## 逐样本明细",
            "",
            "| 样本 | 人工 | 评审 | 漏检 | 误报 | 期望 criterion | 命中 criterion |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in details:
            lines.append(
                f"| {row['id']} | {row['human_verdict']} | {row['review_verdict']} | "
                f"{'是' if row['missed'] else '否'} | {'是' if row['false_alarm'] else '否'} | "
                f"{', '.join(row.get('expected_criteria') or []) or '-'} | "
                f"{', '.join(row.get('blocking_criteria') or []) or '-'} |"
            )
        return "\n".join(lines) + "\n"

    payload = {
        "corpus": metrics.corpus,
        "generated_at": time.time(),
        "provider": provider,
        "baseline_provider": baseline_provider,
        "metrics": {
            "corpus": metrics.corpus,
            "total": metrics.total,
            "missed": metrics.missed,
            "false_alarms": metrics.false_alarms,
            "agreement": metrics.agreement,
            "baseline_missed": metrics.baseline_missed,
            "baseline_false_alarms": metrics.baseline_false_alarms,
            "baseline_agreement": metrics.baseline_agreement,
        },
        "samples": details,
    }
    return dumps(payload)


# ---------------------------------------------------------------- CLI 挂载（供 cli.py 调用）


def register(subparsers) -> None:  # pragma: no cover - 由 cli.py 挂载
    """注册 `kfpa eval review [--seed PATH] [--provider P] [--baseline B] [--out PATH] [--format json|md]`。"""
    parser = subparsers.add_parser("eval", help="同源盲区度量（D4-3.4）")
    sub = parser.add_subparsers(dest="eval_cmd", required=True)
    run = sub.add_parser("review", help="跑种子集，输出四项指标与报告")
    run.add_argument("--seed", default=str(DEFAULT_SEED))
    run.add_argument("--provider", default="mock_hetero")
    run.add_argument("--baseline", dest="baseline_provider", default="mock")
    run.add_argument("--out", default=None, help="报告落盘路径（.json / .md）")
    run.add_argument("--format", dest="fmt", choices=["json", "md"], default=None)

    def _run(args) -> int:
        seed = Path(getattr(args, "seed", None) or DEFAULT_SEED)
        if not seed.is_file():
            print(f"[eval] 种子集不存在：{seed} → 检查 benchmarks/review/seed.jsonl（退出码 4）")
            return 4
        fmt = args.fmt or ("md" if str(args.out or "").endswith(".md") else "json")
        try:
            metrics = evaluate(
                seed,
                provider=args.provider,
                baseline_provider=args.baseline_provider,
            )
        except EvalError as exc:
            print(f"{exc}（退出码 4）")
            return 4
        text = render_report(metrics, fmt)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"[eval] 报告已写入 {out}")
        else:
            print(text)
        print(
            f"[eval] 样本 {metrics.total}｜漏检 {metrics.missed}（baseline {metrics.baseline_missed}）"
            f"｜误报 {metrics.false_alarms}（baseline {metrics.baseline_false_alarms}）"
            f"｜一致率 {metrics.agreement:.2%}（baseline {metrics.baseline_agreement:.2%}）"
            f" → {'达标' if metrics.passed() else '未达标'}"
        )
        return 0

    run.set_defaults(handler=_run, func=_run)


__all__ = [
    "DEFAULT_SEED",
    "EvalError",
    "load_seed",
    "sample_to_package",
    "score_reports",
    "evaluate",
    "render_report",
    "register",
]

"""
domain.py —— 业务事实与确定性计算引擎

为什么单独一层
--------------
智能体最容易出的两类事故是「编造事实」和「算错金额」。
所以运单、账单、价目都存在这里，运费也只由这里算：
模型只能通过工具读取结果，不能自己乘、不能自己猜。

本文件不依赖 LLM、不依赖 HTTP、不依赖数据库，可以单独跑单测：

    python -m domain     # 直接验证样例账单能算出 3 处差异

数据是内置样例（2 家承运商 / 3 条线路 / 5 张运单 / 2 张账单），
其中 SF 那张账单故意埋了三处真实业务里最常见的错误：
  1. 体积重抛比用错（6000 用成 5000）→ 运费虚高
  2. 燃油附加费沿用旧费率（应 5%，按 8% 收）
  3. 同一运单重复计费（两行一模一样的 YD2026100104）
"""

from __future__ import annotations

from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# 价目表：同一线路会有多个版本，按「计费发生日」取生效版本
# ---------------------------------------------------------------------------
RATE_CARDS: list[dict[str, Any]] = [
    {
        "version": "RC-SF-SHCT-2026Q3",
        "carrier": "顺丰速运",
        "lane": "上海-成都",
        "effective_from": "2026-07-01",
        "effective_to": "2026-12-31",
        "price_per_kg": 3.2,
        "volume_ratio": 6000,      # 体积重抛比：1 立方米 = 1_000_000 / 6000 kg
        "fuel_rate": 0.05,         # 燃油附加费率
        "min_charge": 80.0,
        "promise_days": 3,
        "note": "空运泡货按 6000 抛比计费，燃油附加费按当期油价联动",
    },
    {
        "version": "RC-SF-SHCT-2026Q2",
        "carrier": "顺丰速运",
        "lane": "上海-成都",
        "effective_from": "2026-04-01",
        "effective_to": "2026-06-30",
        "price_per_kg": 3.0,
        "volume_ratio": 6000,
        "fuel_rate": 0.08,
        "min_charge": 80.0,
        "promise_days": 3,
        "note": "上一季度版本，燃油附加费 8%",
    },
    {
        "version": "RC-ZT-GZWH-2026Q3",
        "carrier": "中通快运",
        "lane": "广州-武汉",
        "effective_from": "2026-07-01",
        "effective_to": "2026-12-31",
        "price_per_kg": 2.6,
        "volume_ratio": 5000,      # 陆运抛比 5000
        "fuel_rate": 0.06,
        "min_charge": 60.0,
        "promise_days": 4,
        "note": "陆运按 5000 抛比，最低一票 60 元",
    },
]

# ---------------------------------------------------------------------------
# 运单：事实数据，任何一项都不能由模型推测
# ---------------------------------------------------------------------------
WAYBILLS: dict[str, dict[str, Any]] = {
    "YD2026100101": {
        "waybill_no": "YD2026100101", "carrier": "顺丰速运", "lane": "上海-成都",
        "weight_kg": 850.0, "volume_m3": 6.2, "pieces": 12,
        "picked_up_at": "2026-10-08 09:20", "signed_at": "2026-10-11 15:40",
        "goods": "汽车配件", "status": "已签收",
    },
    "YD2026100102": {
        "waybill_no": "YD2026100102", "carrier": "顺丰速运", "lane": "上海-成都",
        "weight_kg": 1200.0, "volume_m3": 3.0, "pieces": 8,
        "picked_up_at": "2026-10-09 14:05", "signed_at": "2026-10-12 10:12",
        "goods": "注塑模具", "status": "已签收",
    },
    "YD2026100104": {
        "waybill_no": "YD2026100104", "carrier": "顺丰速运", "lane": "上海-成都",
        "weight_kg": 300.0, "volume_m3": 6.0, "pieces": 20,
        "picked_up_at": "2026-10-12 08:30", "signed_at": "2026-10-15 17:05",
        "goods": "包装泡沫", "status": "已签收",
    },
    "YD2026100106": {
        "waybill_no": "YD2026100106", "carrier": "顺丰速运", "lane": "上海-成都",
        "weight_kg": 500.0, "volume_m3": 1.0, "pieces": 5,
        "picked_up_at": "2026-10-14 11:00", "signed_at": "2026-10-17 09:30",
        "goods": "五金件", "status": "已签收",
    },
    "YD2026100103": {
        "waybill_no": "YD2026100103", "carrier": "中通快运", "lane": "广州-武汉",
        "weight_kg": 640.0, "volume_m3": 4.8, "pieces": 9,
        "picked_up_at": "2026-10-10 16:20", "signed_at": "2026-10-14 12:00",
        "goods": "家电配件", "status": "已签收",
    },
    "YD2026100105": {
        "waybill_no": "YD2026100105", "carrier": "中通快运", "lane": "广州-武汉",
        "weight_kg": 2100.0, "volume_m3": 2.2, "pieces": 30,
        "picked_up_at": "2026-10-13 07:40", "signed_at": "2026-10-17 18:20",
        "goods": "钢材", "status": "已签收",
    },
}

# ---------------------------------------------------------------------------
# 账单：承运商开来的账，charged_amount 是「对方算的数」，不一定对
# ---------------------------------------------------------------------------
BILLS: dict[str, dict[str, Any]] = {
    "B202610-SF-001": {
        "bill_no": "B202610-SF-001",
        "carrier": "顺丰速运",
        "period": "2026-10",
        "issued_at": "2026-10-25",
        "total_amount": 0.0,        # 由 _sum_total 计算，占位
        "lines": [
            {"line_no": 1, "waybill_no": "YD2026100101", "item": "运费",
             "charged_amount": 4166.40, "remark": "空运，体积重计费"},
            {"line_no": 2, "waybill_no": "YD2026100102", "item": "运费",
             "charged_amount": 4147.20, "remark": "含燃油附加费"},
            {"line_no": 3, "waybill_no": "YD2026100104", "item": "运费",
             "charged_amount": 3360.00, "remark": "泡货"},
            {"line_no": 4, "waybill_no": "YD2026100104", "item": "运费",
             "charged_amount": 3360.00, "remark": "泡货（补录）"},
            {"line_no": 5, "waybill_no": "YD2026100106", "item": "运费",
             "charged_amount": 1680.00, "remark": "重货"},
        ],
    },
    "B202610-ZT-001": {
        "bill_no": "B202610-ZT-001",
        "carrier": "中通快运",
        "period": "2026-10",
        "issued_at": "2026-10-25",
        "total_amount": 0.0,
        "lines": [
            {"line_no": 1, "waybill_no": "YD2026100103", "item": "运费",
             "charged_amount": 2645.76, "remark": "陆运"},
            {"line_no": 2, "waybill_no": "YD2026100105", "item": "运费",
             "charged_amount": 5787.60, "remark": "重货"},
        ],
    },
}

for _bill in BILLS.values():
    _bill["total_amount"] = round(sum(l["charged_amount"] for l in _bill["lines"]), 2)


# ---------------------------------------------------------------------------
# 查询与计算
# ---------------------------------------------------------------------------

def get_waybill(waybill_no: str) -> dict[str, Any] | None:
    return WAYBILLS.get(waybill_no.strip().upper())


def get_bill(bill_no: str) -> dict[str, Any] | None:
    return BILLS.get(bill_no.strip().upper())


def pick_rate_card(carrier: str, lane: str, on_date: str) -> dict[str, Any] | None:
    """
    取计费发生日当天生效的价目版本。

    真实场景这里要查合同库；样例里按 effective_from / effective_to 过滤，
    取生效日期最晚的那一版（同一线路可能有多版并存）。
    """
    candidates = [
        card for card in RATE_CARDS
        if card["carrier"] == carrier and card["lane"] == lane
        and card["effective_from"] <= on_date <= card["effective_to"]
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda card: card["effective_from"])


def resolve_rate_card(carrier: str, lane: str, on_date: str | None = None
                      ) -> tuple[dict[str, Any] | None, str, bool]:
    """
    取适用价目，返回 (价目, 生效日, 是否为兜底版本)。

    演示环境的「今天」可能已经超出样例价目表的有效期（样例是 2026 年），
    所以当期取不到时退到该线路最新一版，并把 fallback 标记传出去——
    宁可让模型在回答里说明「用的是 2026Q3 版本」，也不要直接报错卡死流程。
    """
    day = on_date or date.today().isoformat()
    card = pick_rate_card(carrier, lane, day)
    if card:
        return card, day, False
    cards = [c for c in RATE_CARDS if c["carrier"] == carrier and c["lane"] == lane]
    if not cards:
        return None, day, False
    latest = max(cards, key=lambda c: c["effective_from"])
    return latest, latest["effective_from"], True


def compute_freight(waybill: dict[str, Any], rate: dict[str, Any]) -> dict[str, Any]:
    """
    按价目表算应收运费，返回完整推导过程。

    计费重量 = max(实重, 体积重)；体积重 = 体积(cm³) / 抛比。
    volume_m3 * 1_000_000 就是 cm³，别在这里省这一步，单位错一次金额差 6 倍。
    """
    volume_weight = round(waybill["volume_m3"] * 1_000_000 / rate["volume_ratio"], 2)
    charge_weight = round(max(waybill["weight_kg"], volume_weight), 2)
    base = round(charge_weight * rate["price_per_kg"], 2)
    base = max(base, rate["min_charge"])
    fuel = round(base * rate["fuel_rate"], 2)
    total = round(base + fuel, 2)
    return {
        "waybill_no": waybill["waybill_no"],
        "rule_version": rate["version"],
        "actual_weight": waybill["weight_kg"],
        "volume_m3": waybill["volume_m3"],
        "volume_weight": volume_weight,
        "charge_weight": charge_weight,
        "weight_basis": "体积重" if volume_weight > waybill["weight_kg"] else "实重",
        "price_per_kg": rate["price_per_kg"],
        "base_amount": base,
        "fuel_rate": rate["fuel_rate"],
        "fuel_amount": fuel,
        "expected_amount": total,
        "steps": [
            f"体积重 = {waybill['volume_m3']} m³ × 1,000,000 ÷ {rate['volume_ratio']} = {volume_weight} kg",
            f"计费重量 = max(实重 {waybill['weight_kg']}, 体积重 {volume_weight}) = {charge_weight} kg（按{('体积重' if volume_weight > waybill['weight_kg'] else '实重')}）",
            f"基础运费 = {charge_weight} × {rate['price_per_kg']} = {base} 元"
            + ("（已按最低一票补足）" if charge_weight * rate["price_per_kg"] < rate["min_charge"] else ""),
            f"燃油附加费 = {base} × {rate['fuel_rate']:.0%} = {fuel} 元",
            f"应收合计 = {base} + {fuel} = {total} 元",
        ],
    }


def _diagnose(line: dict[str, Any], waybill: dict[str, Any],
              rate: dict[str, Any], expected: dict[str, Any]) -> tuple[str, str]:
    """
    给差异定性：不是笼统地说「金额不符」，而是指出到底哪个参数用错了。

    做法是拿几个常见错误参数各试算一遍，看哪一版能精确对上账单金额——
    对上了，就说明对方用的是那个错误口径。这样结论才能写进争议单。
    """
    charged = line["charged_amount"]

    # 假设 1：抛比用错（本线路合同是 6000，对方按 5000 算）
    for ratio in (5000, 6000, 4800, 3000):
        if ratio == rate["volume_ratio"]:
            continue
        alt = compute_freight(waybill, {**rate, "volume_ratio": ratio})
        if abs(alt["expected_amount"] - charged) < 0.02:
            return (
                "WEIGHT_BASIS_MISMATCH",
                f"体积重抛比用错：合同 {rate['volume_ratio']}，账单按 {ratio} 计算，"
                f"计费重量多算 {round(alt['charge_weight'] - expected['charge_weight'], 2)} kg",
            )

    # 假设 2：燃油附加费率用错（沿用了上一季度版本）
    for other in RATE_CARDS:
        if other["carrier"] != rate["carrier"] or other["lane"] != rate["lane"]:
            continue
        if other["version"] == rate["version"]:
            continue
        alt = compute_freight(waybill, {**rate, "fuel_rate": other["fuel_rate"]})
        if abs(alt["expected_amount"] - charged) < 0.02:
            return (
                "FUEL_RATE_MISMATCH",
                f"燃油附加费率用错：当期应为 {rate['fuel_rate']:.0%}，账单按 {other['fuel_rate']:.0%}"
                f"（{other['version']} 的费率）计算",
            )

    # 假设 3：单价用错
    for price in (rate["price_per_kg"] * 0.95, rate["price_per_kg"] * 1.05):
        alt = compute_freight(waybill, {**rate, "price_per_kg": round(price, 2)})
        if abs(alt["expected_amount"] - charged) < 0.02:
            return ("UNIT_PRICE_MISMATCH", f"单价与合同不符：合同 {rate['price_per_kg']} 元/kg，"
                                           f"账单疑似按 {round(price, 2)} 元/kg")

    return ("AMOUNT_MISMATCH", "金额与合同价目、运单事实均对不上，需承运商提供计费明细")


def validate_bill(bill_no: str) -> dict[str, Any]:
    """
    逐行核对账单，返回结构化问题清单。

    判定结论完全由这里产出：模型不能传入 verdict，也不能修改差额。
    """
    bill = get_bill(bill_no)
    if bill is None:
        raise KeyError(f"账单不存在：{bill_no}")

    issues: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}          # 运单号 → 首次出现的行号
    total_delta = 0.0

    for line in bill["lines"]:
        waybill = get_waybill(line["waybill_no"])
        if waybill is None:
            issues.append({
                "code": "WAYBILL_NOT_FOUND", "line_no": line["line_no"],
                "waybill_no": line["waybill_no"], "charged": line["charged_amount"],
                "expected": 0.0, "delta": line["charged_amount"],
                "explain": "账单引用的运单号在系统中不存在，可能是录入错误或虚增",
            })
            total_delta += line["charged_amount"]
            continue

        rate = pick_rate_card(waybill["carrier"], waybill["lane"], bill["period"][:7] + "-28")
        if rate is None:
            issues.append({
                "code": "RATE_NOT_FOUND", "line_no": line["line_no"],
                "waybill_no": line["waybill_no"], "charged": line["charged_amount"],
                "expected": 0.0, "delta": 0.0,
                "explain": f"{waybill['carrier']} {waybill['lane']} 在计费期间没有生效价目，无法核定",
            })
            continue

        expected = compute_freight(waybill, rate)

        # 重复计费：同一运单出现两次，第二行整体属于多收
        if line["waybill_no"] in seen:
            issues.append({
                "code": "DUPLICATE_CHARGE", "line_no": line["line_no"],
                "waybill_no": line["waybill_no"],
                "charged": line["charged_amount"], "expected": 0.0,
                "delta": line["charged_amount"],
                "explain": f"同一运单与第 {seen[line['waybill_no']]} 行重复计费，本行应全额冲回",
            })
            total_delta += line["charged_amount"]
            continue
        seen[line["waybill_no"]] = line["line_no"]

        delta = round(line["charged_amount"] - expected["expected_amount"], 2)
        row = {
            "line_no": line["line_no"], "waybill_no": line["waybill_no"],
            "charged": line["charged_amount"],
            "expected": expected["expected_amount"], "delta": delta,
            "expected_detail": expected,
        }
        rows.append(row)

        if abs(delta) > 0.01:
            code, explain = _diagnose(line, waybill, rate, expected)
            issues.append({
                "code": code, "line_no": line["line_no"],
                "waybill_no": line["waybill_no"],
                "charged": line["charged_amount"],
                "expected": expected["expected_amount"],
                "delta": delta, "explain": explain,
            })
            total_delta += delta

    has_overcharge = any(i["delta"] > 0.01 for i in issues)
    has_undercharge = any(i["delta"] < -0.01 for i in issues)
    if has_overcharge:
        verdict = "fail"          # 有多收，必须发起争议
    elif has_undercharge:
        verdict = "warn"          # 只有少收，属于提示级别
    else:
        verdict = "pass"

    return {
        "bill_no": bill["bill_no"],
        "carrier": bill["carrier"],
        "period": bill["period"],
        "verdict": verdict,
        "issues": issues,
        "rows": rows,
        "total_delta": round(total_delta, 2),
        "checked_lines": len(bill["lines"]),
    }


# ---------------------------------------------------------------------------
# 自检：python -m domain
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for bill_no in BILLS:
        result = validate_bill(bill_no)
        print(f"\n== {bill_no} 结论={result['verdict']} 差额合计={result['total_delta']} ==")
        for issue in result["issues"]:
            print(f"  行{issue['line_no']} {issue['code']}: {issue['explain']} "
                  f"（应 {issue['expected']} / 收 {issue['charged']} / 差 {issue['delta']}）")
        print(f"  通过行数 {len(result['rows'])}，检查行数 {result['checked_lines']}")

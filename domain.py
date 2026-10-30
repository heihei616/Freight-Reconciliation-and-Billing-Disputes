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
    # 阶梯价：华东速运按「计费重量落档」定价，没有单一单价。
    # 这类纯查询类问题（「上海→南京体积重怎么算、阶梯价多少」）必须能一次答清楚，
    # 所以价目表本身要带上 tiers，而不是让模型去算。
    {
        "version": "RC-HD-SHNJ-2026Q3",
        "carrier": "华东速运",
        "lane": "上海-南京",
        "effective_from": "2026-07-01",
        "effective_to": "2026-12-31",
        "price_per_kg": 1.8,       # 首档单价，兼容按单价的旧逻辑
        "tiers": [
            {"max_kg": 500, "price_per_kg": 1.8, "label": "≤500kg"},
            {"max_kg": 2000, "price_per_kg": 1.6, "label": "500-2000kg"},
            {"max_kg": None, "price_per_kg": 1.4, "label": ">2000kg"},
        ],
        "volume_ratio": 6000,      # 甩挂运输，抛比 6000
        "fuel_rate": 0.04,
        "min_charge": 50.0,
        "promise_days": 2,
        "note": "阶梯计价：计费重量 ≤500kg 按 1.8 元/kg、500-2000kg 按 1.6 元/kg、"
                ">2000kg 按 1.4 元/kg；体积重抛比 6000，最低一票 50 元，燃油附加费 4%",
    },
    {
        "version": "RC-HD-SHNJ-2026Q2",
        "carrier": "华东速运",
        "lane": "上海-南京",
        "effective_from": "2026-04-01",
        "effective_to": "2026-06-30",
        "price_per_kg": 1.9,
        "tiers": [
            {"max_kg": 500, "price_per_kg": 1.9, "label": "≤500kg"},
            {"max_kg": 2000, "price_per_kg": 1.7, "label": "500-2000kg"},
            {"max_kg": None, "price_per_kg": 1.5, "label": ">2000kg"},
        ],
        "volume_ratio": 6000,
        "fuel_rate": 0.05,
        "min_charge": 50.0,
        "promise_days": 2,
        "note": "上一季度版本，各档单价高 0.1 元/kg，燃油 5%",
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
    # ---- 华东速运 上海-南京（9 月账期，用于「核上个月争议运费」这类跨月场景）----
    "YD2026090201": {
        "waybill_no": "YD2026090201", "carrier": "华东速运", "lane": "上海-南京",
        "weight_kg": 380.0, "volume_m3": 0.8, "pieces": 6,
        "picked_up_at": "2026-09-06 08:10", "signed_at": "2026-09-07 16:30",
        "goods": "药品包装", "status": "已签收",
    },
    "YD2026090202": {
        "waybill_no": "YD2026090202", "carrier": "华东速运", "lane": "上海-南京",
        "weight_kg": 1500.0, "volume_m3": 1.2, "pieces": 18,
        "picked_up_at": "2026-09-11 13:20", "signed_at": "2026-09-12 09:45",
        "goods": "电子元件", "status": "已签收",
    },
    "YD2026090203": {
        "waybill_no": "YD2026090203", "carrier": "华东速运", "lane": "上海-南京",
        "weight_kg": 2600.0, "volume_m3": 2.0, "pieces": 24,
        "picked_up_at": "2026-09-18 07:50", "signed_at": "2026-09-19 14:10",
        "goods": "机械配件", "status": "已签收",
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
    # 上月账单：埋一处「阶梯档用错」——2600kg 本该按 1.4 元/kg，账单按 1.6 元/kg 收
    "B202609-HD-001": {
        "bill_no": "B202609-HD-001",
        "carrier": "华东速运",
        "period": "2026-09",
        "issued_at": "2026-09-28",
        "total_amount": 0.0,
        "lines": [
            {"line_no": 1, "waybill_no": "YD2026090201", "item": "运费",
             "charged_amount": 711.36, "remark": "小票，首档"},
            {"line_no": 2, "waybill_no": "YD2026090202", "item": "运费",
             "charged_amount": 2496.00, "remark": "中票"},
            {"line_no": 3, "waybill_no": "YD2026090203", "item": "运费",
             "charged_amount": 4326.40, "remark": "大票，按 1.6 元/kg 计"},
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


def list_bills(carrier: str | None = None,
               month: str | None = None) -> list[dict[str, Any]]:
    """
    按承运商 / 账期查账单。

    有了它，用户说「核上个月华东速运的账」时模型能自己把账单找出来，
    而不是反问「账单号是多少」——反问一次就多一轮，轮次在 Harness 里是稀缺资源。
    """
    out = []
    for bill in BILLS.values():
        if carrier and bill["carrier"] != carrier.strip():
            continue
        if month and bill["period"] != month.strip():
            continue
        out.append({
            "bill_no": bill["bill_no"],
            "carrier": bill["carrier"],
            "period": bill["period"],
            "issued_at": bill["issued_at"],
            "total_amount": bill["total_amount"],
            "line_count": len(bill["lines"]),
        })
    return sorted(out, key=lambda item: item["issued_at"], reverse=True)


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


def unit_price(rate: dict[str, Any], charge_weight: float) -> tuple[float, str]:
    """
    取该计费重量适用的单价。

    两种价目形态并存：单一单价（顺丰/中通）与阶梯价（华东速运）。
    阶梯价必须按「计费重量落档」取单价，而不是按实重——
    2600kg 的货按实重落档和按体积重落档可能差一档，金额就会出现几十上百的偏差。
    """
    tiers = rate.get("tiers")
    if not tiers:
        return float(rate["price_per_kg"]), ""

    for index, tier in enumerate(tiers):
        max_kg = tier.get("max_kg")
        if max_kg is None or charge_weight <= max_kg:
            label = tier.get("label") or (
                f">{tiers[index - 1]['max_kg']}kg" if index else f"≤{max_kg}kg"
            )
            return float(tier["price_per_kg"]), label
    return float(tiers[-1]["price_per_kg"]), ""


def compute_freight(waybill: dict[str, Any], rate: dict[str, Any]) -> dict[str, Any]:
    """
    按价目表算应收运费，返回完整推导过程。

    计费重量 = max(实重, 体积重)；体积重 = 体积(m³) × 1_000_000 / 抛比。
    volume_m3 * 1_000_000 就是 cm³，别在这里省这一步，单位错一次金额差 6 倍。
    """
    volume_weight = round(waybill["volume_m3"] * 1_000_000 / rate["volume_ratio"], 2)
    charge_weight = round(max(waybill["weight_kg"], volume_weight), 2)
    price, tier_label = unit_price(rate, charge_weight)
    base = round(charge_weight * price, 2)
    base = max(base, rate["min_charge"])
    fuel = round(base * rate["fuel_rate"], 2)
    total = round(base + fuel, 2)

    steps = [
        f"体积重 = {waybill['volume_m3']} m³ × 1,000,000 ÷ {rate['volume_ratio']} = {volume_weight} kg",
        f"计费重量 = max(实重 {waybill['weight_kg']}, 体积重 {volume_weight}) = {charge_weight} kg"
        f"（按{('体积重' if volume_weight > waybill['weight_kg'] else '实重')}）",
    ]
    if tier_label:
        steps.append(f"落档：计费重量 {charge_weight} kg 属于 {tier_label} 档，单价 {price} 元/kg")
    steps += [
        f"基础运费 = {charge_weight} × {price} = {base} 元"
        + ("（已按最低一票补足）" if charge_weight * price < rate["min_charge"] else ""),
        f"燃油附加费 = {base} × {rate['fuel_rate']:.0%} = {fuel} 元",
        f"应收合计 = {base} + {fuel} = {total} 元",
    ]

    return {
        "waybill_no": waybill["waybill_no"],
        "rule_version": rate["version"],
        "actual_weight": waybill["weight_kg"],
        "volume_m3": waybill["volume_m3"],
        "volume_weight": volume_weight,
        "charge_weight": charge_weight,
        "weight_basis": "体积重" if volume_weight > waybill["weight_kg"] else "实重",
        "price_per_kg": price,
        "tier": tier_label or None,
        "base_amount": base,
        "fuel_rate": rate["fuel_rate"],
        "fuel_amount": fuel,
        "expected_amount": total,
        "steps": steps,
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

    # 假设 4：阶梯档位用错——把上一档（更贵）的单价套到了本档
    tiers = rate.get("tiers") or []
    for tier in tiers:
        other_price = float(tier["price_per_kg"])
        if abs(other_price - expected["price_per_kg"]) < 1e-9:
            continue
        alt = compute_freight(waybill, {**rate, "tiers": None, "price_per_kg": other_price})
        if abs(alt["expected_amount"] - charged) < 0.02:
            right = f"{expected['price_per_kg']} 元/kg"
            return (
                "TIER_MISMATCH",
                f"阶梯档位用错：计费重量 {expected['charge_weight']} kg 应落 {expected['tier']} 档、"
                f"单价 {right}，账单按 {other_price} 元/kg 计算，"
                f"多收 {round(charged - expected['expected_amount'], 2)} 元",
            )

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
# 争议额度：承运商合同里通常约定「单月可争议金额上限」，超过要走上报流程。
# 模型不知道这个数，也不该猜，所以做成可查的数据。
# ---------------------------------------------------------------------------
DISPUTE_QUOTAS: dict[tuple[str, str], dict[str, Any]] = {
    ("华东速运", "2026-09"): {
        "carrier": "华东速运", "month": "2026-09",
        "monthly_limit": 50000.0,     # 单月可发起争议金额上限
        "used": 12000.0,              # 本月已发起
        "remaining": 38000.0,
        "used_ratio": 0.24,
        "rule": "单月争议金额不超过 5 万元；单笔差额超过 1 万元需结算经理审批",
    },
    ("华东速运", "2026-10"): {
        "carrier": "华东速运", "month": "2026-10",
        "monthly_limit": 50000.0, "used": 0.0, "remaining": 50000.0,
        "used_ratio": 0.0,
        "rule": "单月争议金额不超过 5 万元；单笔差额超过 1 万元需结算经理审批",
    },
    ("顺丰速运", "2026-10"): {
        "carrier": "顺丰速运", "month": "2026-10",
        "monthly_limit": 80000.0, "used": 4169.61, "remaining": 75830.39,
        "used_ratio": 0.0521,
        "rule": "单月争议金额不超过 8 万元；单笔差额超过 1 万元需结算经理审批",
    },
}


def get_quota(carrier: str, month: str) -> dict[str, Any] | None:
    return DISPUTE_QUOTAS.get((carrier.strip(), month.strip()))


# ---------------------------------------------------------------------------
# 证据库：账单对应的凭证是否已归档。缺哪份，就该向承运商要哪份。
# ---------------------------------------------------------------------------
EVIDENCE: dict[str, dict[str, list[str]]] = {
    "B202609-HD-001": {
        "称重单": ["CD2026090201", "CD2026090202"],   # 缺 03 那张
        "签收回执": ["QD2026090201", "QD2026090202", "QD2026090203"],
        "附加费依据": [],
    },
    "B202610-SF-001": {
        "称重单": ["CD2026100101", "CD2026100102", "CD2026100104", "CD2026100106"],
        "签收回执": ["QD2026100101", "QD2026100102", "QD2026100104", "QD2026100106"],
        "附加费依据": ["FJ2026100101"],
    },
}

# 三类必备证据，任何一类缺项都要补
REQUIRED_EVIDENCE = ("称重单", "签收回执", "附加费依据")


def check_evidence(bill_no: str, items: list[str] | None = None) -> dict[str, Any]:
    """
    检查某张账单的证据齐备情况。

    items 为空表示按标准清单全查；也可以只查用户口头提到的那几类，
    返回里同时给出「已有」和「缺什么」，模型据此向用户要材料才不会要错。
    """
    bill = get_bill(bill_no)
    if bill is None:
        return {"bill_no": bill_no, "found": False}

    owned = EVIDENCE.get(bill["bill_no"], {})
    wanted = [item for item in (items or REQUIRED_EVIDENCE) if item]
    detail = []
    missing: list[str] = []
    untracked: list[str] = []
    for item in wanted:
        if item not in REQUIRED_EVIDENCE:
            # 用户/模型问了个标准清单外的类别：如实说「没这类登记」，
            # 不能顺手报成「缺失」——那样会让模型去要一份根本不存在的材料。
            detail.append({"item": item, "count": None, "docs": [],
                           "note": "不在标准证据清单内，系统未登记"})
            untracked.append(item)
            continue
        docs = owned.get(item, [])
        detail.append({"item": item, "count": len(docs), "docs": docs})
        if not docs:
            missing.append(item)

    return {
        "bill_no": bill["bill_no"],
        "carrier": bill["carrier"],
        "period": bill["period"],
        "found": True,
        "requested": wanted,
        "detail": detail,
        "missing": missing,
        "untracked": untracked,
        "standard_items": list(REQUIRED_EVIDENCE),
        "complete": not missing,
        "note": ("全部齐备，可以进入核对" if not missing else
                 "缺：" + "、".join(missing) + "；核对与建单前必须先向承运商索取"),
    }


# ---------------------------------------------------------------------------
# 称重单：能不能作为计费依据，取决于签章、时点与偏差，不是「有单就算」
# ---------------------------------------------------------------------------
WEIGHT_TICKETS: dict[str, dict[str, Any]] = {
    "CD2026090201": {
        "ticket_no": "CD2026090201", "waybill_no": "YD2026090201",
        "weight_kg": 380.0, "weighed_at": "2026-09-06 07:40",
        "picked_at": "2026-09-06 08:10", "carrier_stamp": True, "shipper_stamp": True,
        "weighed_by": "华东速运 南京中转场",
    },
    "CD2026090203": {
        # 与运单重量差 8%（超过 5% 允许偏差），且缺发货方签章 → 不能直接计入
        "ticket_no": "CD2026090203", "waybill_no": "YD2026090203",
        "weight_kg": 2808.0, "weighed_at": "2026-09-18 06:30",
        "picked_at": "2026-09-18 07:50", "carrier_stamp": True, "shipper_stamp": False,
        "weighed_by": "华东速运 南京中转场",
    },
    "CD2026100101": {
        "ticket_no": "CD2026100101", "waybill_no": "YD2026100101",
        "weight_kg": 852.0, "weighed_at": "2026-10-08 08:50",
        "picked_at": "2026-10-08 09:20", "carrier_stamp": True, "shipper_stamp": True,
        "weighed_by": "顺丰速运 上海转运中心",
    },
}

WEIGHT_TOLERANCE = 0.05     # 称重与运单重量允许偏差 5%


def check_weight_ticket(ticket_no: str) -> dict[str, Any]:
    """
    判定一张称重单能否作为计费依据。

    四条硬规则（都来自制度，不由模型判断）：
      1. 称重时间必须早于提货时间（装车后补称的不算）；
      2. 承运商与发货方都要盖章；
      3. 与运单申报重量的偏差不得超过 5%；
      4. 运单本身必须已签收。
    """
    ticket = WEIGHT_TICKETS.get(ticket_no.strip().upper())
    if ticket is None:
        return {"ticket_no": ticket_no, "found": False,
                "admissible": False, "reasons": ["系统中没有这张称重单"]}

    waybill = get_waybill(ticket["waybill_no"])
    reasons: list[str] = []
    if waybill is None:
        reasons.append(f"称重单关联的运单 {ticket['waybill_no']} 不存在")
        deviation = None
    else:
        deviation = round((ticket["weight_kg"] - waybill["weight_kg"]) / waybill["weight_kg"], 4)
        if abs(deviation) > WEIGHT_TOLERANCE:
            reasons.append(
                f"称重 {ticket['weight_kg']} kg 与运单申报 {waybill['weight_kg']} kg "
                f"偏差 {deviation:.1%}，超过允许的 {WEIGHT_TOLERANCE:.0%}"
            )
        if waybill["status"] != "已签收":
            reasons.append("运单尚未签收，重量争议无从核对")

    if ticket["weighed_at"] > ticket["picked_at"]:
        reasons.append(f"称重时间 {ticket['weighed_at']} 晚于提货时间 {ticket['picked_at']}，"
                       f"不能作为装车前重量依据")
    if not ticket["carrier_stamp"]:
        reasons.append("缺承运商签章")
    if not ticket["shipper_stamp"]:
        reasons.append("缺发货方签章")

    return {
        "ticket_no": ticket["ticket_no"],
        "found": True,
        "waybill_no": ticket["waybill_no"],
        "weight_kg": ticket["weight_kg"],
        "waybill_weight_kg": waybill["weight_kg"] if waybill else None,
        "deviation": deviation,
        "weighed_at": ticket["weighed_at"],
        "weighed_by": ticket["weighed_by"],
        "admissible": not reasons,
        "reasons": reasons,
        "conclusion": "可计入运费" if not reasons else "不可计入运费",
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

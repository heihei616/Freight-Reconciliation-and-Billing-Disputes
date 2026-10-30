"""离线业务示例：python examples/demo.py（无需 API Key）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from logistics_agent.core import domain


def main() -> None:
    waybill = domain.get_waybill("YD2026100104")
    rate = domain.pick_rate_card(waybill["carrier"], waybill["lane"], "2026-10-15")
    freight = domain.compute_freight(waybill, rate)

    print(f"运单：{waybill['waybill_no']}（{waybill['carrier']} {waybill['lane']}）")
    for step in freight["steps"]:
        print(" -", step)

    result = domain.validate_bill("B202610-SF-001")
    print(f"\n账单 {result['bill_no']}：{result['verdict']}，差额 {result['total_delta']} 元")
    for issue in result["issues"]:
        print(f" - 第 {issue['line_no']} 行 {issue['code']}：{issue['explain']}")


if __name__ == "__main__":
    main()

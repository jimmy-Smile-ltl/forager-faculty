#!/usr/bin/env python3
"""波动期探测 (2026-10-13 反思: 波动期批次67/69 RETRY纯空转, 池子被白烧)。

输出+退出码: 波动期 exit 0 / 健康期 exit 1。供 auto_keepalive.sh 在启动
新批前检查 — 波动期暂停批次(池子留健康期测, 不再白烧), 健康期正常启动。
双重判定: LLM熔断器打开(波动确立) OR 300s窗内波动签名(实时, 防熔断滞后)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def main():
    try:
        from util.llm_gate import breaker_open
        if breaker_open():
            print("波动期(熔断器打开) → 暂停新批")
            sys.exit(0)
    except Exception:
        pass
    try:
        import fix_loop
        if fix_loop._recent_wave(300):
            print("波动期(300s窗内波动签名) → 暂停新批")
            sys.exit(0)
    except Exception:
        pass
    print("健康期 → 可启动批次")
    sys.exit(1)


if __name__ == "__main__":
    main()

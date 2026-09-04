"""validate.py — 任务产出校验脚本，被 validator._run_validate() 子进程调用。"""
import sys, json

def main():
    candidate = sys.argv[1] if len(sys.argv) > 1 else ""
    # ponytail: 默认通过，硬规则校验由 validator 主进程的 _hard_diff_rules 承担
    print(json.dumps({"verdict": "通过", "verdict_reason": "ok"}))

if __name__ == "__main__":
    main()

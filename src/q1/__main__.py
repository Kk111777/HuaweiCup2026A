"""第一问入口：improve 子命令进入第二版；图路径沿用第三版。"""
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("improve", "improve-batch"):
        from .stage2.run import main as solve
    else:
        from .unified.__main__ import main as solve
    return solve()


if __name__ == "__main__":
    raise SystemExit(main())

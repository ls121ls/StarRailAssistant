"""SRA 后端入口。

启动早期（日志系统初始化之前）如果崩溃，控制台里往往什么都看不到，
前端也只会显示「进程已退出」。这里兜底把异常写进 log/bootstrap_error.log，
方便排查（例如导入失败、设置文件损坏等）。
"""

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _write_bootstrap_error() -> None:
    try:
        log_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "bootstrap_error.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 后端启动失败 =====\n")
            f.write(f"cwd: {os.getcwd()}\n")
            f.write(f"argv: {sys.argv}\n")
            f.write(traceback.format_exc())
        print(f"\n[SRA] 后端启动失败，详细信息已写入 {log_path}", file=sys.stderr)
    except Exception:
        pass


try:
    from SRACore.__main__ import main

    if __name__ == '__main__':
        main()
except SystemExit:
    raise
except BaseException:
    _write_bootstrap_error()
    traceback.print_exc()
    sys.exit(1)

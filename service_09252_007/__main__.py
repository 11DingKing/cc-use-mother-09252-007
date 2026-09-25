"""服务入口：python3 -m service_09252_007 --db /path/risk.db --port 8080

运行数据默认写到用户数据目录（$XDG_DATA_HOME 或 ~/.local/share），
不污染源码目录；也可用 --db 或环境变量 RISK_SERVICE_DB 指定。
"""
from __future__ import annotations

import argparse
import os

from .api.http import create_server
from .app.service import RiskService


def default_db_path():
    env = os.environ.get("RISK_SERVICE_DB")
    if env:
        return env
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "service_09252_007", "risk.db")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="service_09252_007",
                                     description="合作办学风险联动服务")
    parser.add_argument("--db", default=None, help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    db_path = args.db or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    service = RiskService(db_path)
    server = create_server(service, args.host, args.port)
    print(f"风险联动服务已启动: http://{args.host}:{args.port}  db={db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

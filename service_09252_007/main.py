"""服务入口：python3 -m service_09252_007.main --db risk.db --port 8080"""
from __future__ import annotations

import argparse

from .api import create_server
from .service import RiskService


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="合作办学风险联动服务")
    parser.add_argument("--db", default="risk_service.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    args = parser.parse_args(argv)

    service = RiskService(args.db)
    server = create_server(service, args.host, args.port)
    print(f"合作办学风险联动服务已启动: http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()

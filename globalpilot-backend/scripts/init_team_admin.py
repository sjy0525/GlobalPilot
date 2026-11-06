import argparse
import json
import sys
from uuid import UUID


def main() -> int:
    parser = argparse.ArgumentParser(
        description="创建团队或为已有空团队初始化首个管理员",
    )

    group = parser.add_mutually_exclusive_group(required=True)

    group.add_argument(
        "--team-name",
        help="创建一个新团队",
    )

    group.add_argument(
        "--team-id",
        type=UUID,
        help="初始化已有空团队",
    )

    parser.add_argument(
        "--admin-email",
        required=True,
        help="已经注册且有效的管理员账号邮箱",
    )

    args = parser.parse_args()

    from app.core.http_errors import AppHTTPException

    try:
        from app.db.database import engine, sessionLocal
        from app.modules.teams.service import initialize_team_admin

        engine.echo = False

        with sessionLocal.begin() as db:
            result = initialize_team_admin(
                db,
                admin_email=args.admin_email,
                team_name=args.team_name,
                team_id=args.team_id,
            )

    except AppHTTPException as exc:
        print(
            f"{exc.code}: {exc.detail}",
            file=sys.stderr,
        )
        return 1

    except ModuleNotFoundError as exc:
        print(
            f"初始化失败：找不到 Python 模块 {exc.name}，"
            "请检查模块路径拼写和当前虚拟环境的依赖。",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        print(
            f"初始化失败（{type(exc).__name__}），"
            "请检查配置和数据库状态。",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from time import monotonic, sleep

from hugin.browser_control import BrowserControl
from hugin.core.settings import get_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Временно передать браузер Hugin помощнику")
    parser.add_argument("--account-id", type=int, default=1)
    commands = parser.add_subparsers(dest="command", required=True)
    acquire = commands.add_parser("acquire")
    acquire.add_argument("--url", default="https://hh.ru/account/captcha")
    acquire.add_argument("--minutes", type=int, default=30)
    acquire.add_argument("--timeout", type=int, default=180)
    commands.add_parser("status")
    release = commands.add_parser("release")
    release.add_argument("token")
    args = parser.parse_args(argv)
    settings = get_settings()
    control = BrowserControl(settings.data_dir, args.account_id)
    if args.command == "release":
        control.release(args.token)
        print(json.dumps({"state": "released"}))
        return 0
    if args.command == "status":
        print(json.dumps({**control.read(), "active": control.active()}, ensure_ascii=False))
        return 0
    request = control.acquire(args.url, minutes=args.minutes)
    deadline = monotonic() + args.timeout
    try:
        while monotonic() < deadline:
            current = control.read_active()
            if current.get("token") != request["token"]:
                raise RuntimeError("Передача управления отменена")
            if current.get("state") == "failed":
                raise RuntimeError(current.get("error", "Браузер недоступен"))
            if current.get("state") == "ready":
                print(json.dumps(current, ensure_ascii=False))
                return 0
            sleep(0.2)
        raise RuntimeError("Браузер не подтвердил передачу управления; проверьте запуск приложения")
    except Exception:
        control.release(str(request["token"]), strict=False)
        raise


if __name__ == "__main__":
    raise SystemExit(main())

"""Фикстура PERF (P6-06): детерминированная CPU-нагрузка; объём задаёт bench/params.json."""

import json
import pathlib


def main() -> None:
    params = json.loads((pathlib.Path(__file__).with_name("params.json")).read_text(encoding="utf-8"))
    total = 0
    for i in range(int(params["iterations"])):
        total += i * i % 7
    print(total)


if __name__ == "__main__":
    main()

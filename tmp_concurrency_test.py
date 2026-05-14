import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OUT_PATH = Path("tmp_concurrency_test.txt")
HEALTH_URL = "http://127.0.0.1:8000/api/health"
JOB_URL = "http://127.0.0.1:8000/api/jobs/long"


def hit(url: str, data: bytes | None = None, headers: dict[str, str] | None = None):
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return "ERR", str(e)


def main() -> None:
    lines: list[str] = []

    h_code, h_body = hit(HEALTH_URL)
    lines.append(f"HEALTH_STATUS={h_code}")
    lines.append(f"HEALTH_BODY={h_body}")

    payload = urllib.parse.urlencode(
        {
            "script_text": "concurrency test",
            "max_chars": "180",
            "num_step": "32",
        }
    ).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    results: list[tuple[object, str] | None] = [None, None]

    def run(idx: int) -> None:
        results[idx] = hit(JOB_URL, payload, headers)

    t1 = threading.Thread(target=run, args=(0,))
    t2 = threading.Thread(target=run, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    r1 = results[0] or ("ERR", "missing result")
    r2 = results[1] or ("ERR", "missing result")

    lines.append(f"R1_STATUS={r1[0]}")
    lines.append(f"R1_BODY={r1[1]}")
    lines.append(f"R2_STATUS={r2[0]}")
    lines.append(f"R2_BODY={r2[1]}")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

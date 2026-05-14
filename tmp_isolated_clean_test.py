import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path("tmp_isolated_clean_test.txt")
PORT = 8012


def hit(url: str, data: bytes | None = None, method: str | None = None, headers: dict[str, str] | None = None, timeout: int = 60):
    req = urllib.request.Request(
        url,
        data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        return "ERR", str(e)


def main() -> None:
    lines: list[str] = []
    p = subprocess.Popen(
        [
            ".venv\\Scripts\\python.exe",
            "-m",
            "uvicorn",
            "server.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        health = None
        for _ in range(40):
            health = hit(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
            if health[0] == 200:
                break
            time.sleep(0.25)

        if health is None:
            lines.append("HEALTH_STATUS=ERR")
            lines.append("HEALTH_BODY=no response")
            OUT.write_text("\n".join(lines), encoding="utf-8")
            return

        lines.append(f"HEALTH_STATUS={health[0]}")
        lines.append(f"HEALTH_BODY={health[1]}")

        code, body = hit(f"http://127.0.0.1:{PORT}/api/jobs")
        lines.append(f"LIST_STATUS={code}")
        cancelled: list[tuple[str, object]] = []
        if code == 200:
            jobs = json.loads(body).get("jobs", [])
            for j in jobs:
                st = str(j.get("status", "")).lower()
                if st in ("running", "queued", "pending"):
                    jid = str(j.get("id"))
                    c2, _ = hit(f"http://127.0.0.1:{PORT}/api/jobs/{jid}/cancel", method="POST")
                    cancelled.append((jid, c2))
        lines.append("CANCELLED=" + json.dumps(cancelled, ensure_ascii=False))

        time.sleep(0.8)

        url = f"http://127.0.0.1:{PORT}/api/jobs/long"
        payload = urllib.parse.urlencode(
            {
                "script_text": "isolated clean concurrency test",
                "max_chars": "180",
                "num_step": "32",
            }
        ).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        res: list[tuple[object, str] | None] = [None, None]

        def run(idx: int) -> None:
            res[idx] = hit(url, data=payload, headers=headers)

        t1 = threading.Thread(target=run, args=(0,))
        t2 = threading.Thread(target=run, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        r1 = res[0] or ("ERR", "missing")
        r2 = res[1] or ("ERR", "missing")

        lines.append(f"R1_STATUS={r1[0]}")
        lines.append(f"R1_BODY={r1[1]}")
        lines.append(f"R2_STATUS={r2[0]}")
        lines.append(f"R2_BODY={r2[1]}")
        OUT.write_text("\n".join(lines), encoding="utf-8")
    finally:
        try:
            p.terminate()
            time.sleep(1)
            if p.poll() is None:
                p.kill()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()

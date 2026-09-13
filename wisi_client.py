import http.cookiejar
import time
import urllib.error
import urllib.request

from config import (
    WISI_HOST,
    WISI_MODULES,
    HTTP_TIMEOUT_SECONDS,
    RESOURCE_TUNER_FLUX,
    RESOURCE_TS_FLUX,
    RESOURCE_PIDMAPPER,
    RESOURCE_PCR,
    RESOURCE_TSDB_INPUT,
)


class WisiClient:
    """
    Production HTTP/XMLC client for the WISI TANGRAM chassis.

    GT34 module resources are accessed through the chassis proxy:

        POST http://<chassis>/data.xmlc?size=1

    Request body:

        remote/<remote_identifier>/<resource>
        END
    """

    def __init__(
        self,
        host=WISI_HOST,
        timeout=HTTP_TIMEOUT_SECONDS,
        retries=2,
        retry_delay=1.0,
    ):
        self.host = host
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay

        self.cookie_jar = http.cookiejar.CookieJar()

        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(
                self.cookie_jar
            )
        )

        self.session_established = False


    # ========================================================
    # SESSION
    # ========================================================

    def establish_session(self):
        url = f"http://{self.host}/"

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                    "Mozilla/5.0 WISI-GT34-Monitor/1.0",

                "Accept":
                    "*/*",

                "Cache-Control":
                    "no-cache",

                "Pragma":
                    "no-cache",

                "Connection":
                    "close",
            },
        )

        try:
            with self.opener.open(
                request,
                timeout=self.timeout,
            ) as response:

                response.read()

                self.session_established = True

                return True

        except Exception:
            self.session_established = False
            return False


    # ========================================================
    # MODULE QUERY
    # ========================================================

    def get_resource(
        self,
        remote_identifier,
        resource,
    ):
        """
        Retrieve one GT34 module XMLC resource.

        Returns:
            {
                "ok": bool,
                "status": int | None,
                "resource": str,
                "remote_identifier": str,
                "text": str,
                "error": str | None,
                "elapsed_seconds": float,
                "attempts": int
            }
        """

        path = (
            f"remote/"
            f"{remote_identifier}/"
            f"{resource}"
        )

        body = (
            path + "\r\nEND"
        ).encode("utf-8")

        url = (
            f"http://{self.host}/"
            f"data.xmlc?size=1"
        )

        last_error = None
        last_status = None

        started = time.monotonic()

        for attempt in range(
            1,
            self.retries + 2,
        ):

            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "User-Agent":
                        "Mozilla/5.0 "
                        "WISI-GT34-Monitor/1.0",

                    "Accept":
                        "*/*",

                    "Cache-Control":
                        "no-cache",

                    "Pragma":
                        "no-cache",

                    "Referer":
                        f"http://{self.host}/",

                    "Origin":
                        f"http://{self.host}",

                    "Content-Type":
                        "text/plain; charset=UTF-8",

                    "Connection":
                        "close",
                },
                method="POST",
            )

            try:
                with self.opener.open(
                    request,
                    timeout=self.timeout,
                ) as response:

                    text = response.read().decode(
                        "utf-8",
                        errors="replace",
                    )

                    elapsed = (
                        time.monotonic()
                        - started
                    )

                    return {
                        "ok": True,
                        "status": response.status,
                        "resource": resource,
                        "remote_identifier":
                            remote_identifier,
                        "text": text,
                        "error": None,
                        "elapsed_seconds":
                            elapsed,
                        "attempts": attempt,
                    }

            except urllib.error.HTTPError as exc:

                last_status = exc.code

                try:
                    error_text = (
                        exc.read().decode(
                            "utf-8",
                            errors="replace",
                        )
                    )
                except Exception:
                    error_text = ""

                last_error = (
                    f"HTTPError {exc.code}: "
                    f"{exc.reason}"
                )

                # HTTP errors generally indicate that
                # communication with the chassis succeeded.
                # Preserve the response for diagnostics.

                elapsed = (
                    time.monotonic()
                    - started
                )

                return {
                    "ok": False,
                    "status": exc.code,
                    "resource": resource,
                    "remote_identifier":
                        remote_identifier,
                    "text": error_text,
                    "error": last_error,
                    "elapsed_seconds":
                        elapsed,
                    "attempts": attempt,
                }

            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
            ) as exc:

                last_error = (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

                if attempt <= self.retries:
                    time.sleep(
                        self.retry_delay
                    )

                    continue

            except Exception as exc:

                last_error = (
                    f"{type(exc).__name__}: "
                    f"{exc}"
                )

                break

        elapsed = (
            time.monotonic()
            - started
        )

        return {
            "ok": False,
            "status": last_status,
            "resource": resource,
            "remote_identifier":
                remote_identifier,
            "text": "",
            "error": last_error,
            "elapsed_seconds": elapsed,
            "attempts":
                self.retries + 1,
        }


    # ========================================================
    # MODULE SNAPSHOT
    # ========================================================

    def get_module_snapshot(
        self,
        remote_identifier,
    ):
        """
        Retrieve all production resources needed for
        one GT34 module snapshot.
        """

        resources = {
            "tuner_flux":
                RESOURCE_TUNER_FLUX,

            "ts_flux":
                RESOURCE_TS_FLUX,

            "pidmapper":
                RESOURCE_PIDMAPPER,

            "pcr":
                RESOURCE_PCR,

            "tsdb_input":
                RESOURCE_TSDB_INPUT,
        }

        snapshot = {}

        for key, resource in resources.items():

            snapshot[key] = self.get_resource(
                remote_identifier,
                resource,
            )

        return snapshot


# ============================================================
# STANDALONE VALIDATION
# ============================================================

def main():
    client = WisiClient()

    print()
    print("=" * 78)
    print("WISI GT34 PRODUCTION CLIENT VALIDATION")
    print("=" * 78)

    print(
        f"Chassis : {WISI_HOST}"
    )

    print(
        f"Timeout : "
        f"{HTTP_TIMEOUT_SECONDS}s"
    )

    print()

    print(
        f"Establishing session with "
        f"{WISI_HOST} ..."
    )

    if client.establish_session():
        print("Session established.")
    else:
        print(
            "WARNING: Initial chassis GET failed."
        )
        print(
            "Continuing with data.xmlc requests."
        )

    total_requests = 0
    successful_requests = 0

    for (
        module_number,
        module_config,
    ) in WISI_MODULES.items():

        remote = module_config["remote"]
        remote_ip = module_config["remote_ip"]

        print()
        print(
            f"MODULE {module_number} "
            f"({remote_ip})"
        )

        print("-" * 78)

        snapshot = (
            client.get_module_snapshot(
                remote
            )
        )

        for key, result in (
            snapshot.items()
        ):

            total_requests += 1

            if result["ok"]:
                successful_requests += 1

                print(
                    f"{key:<18}"
                    f"HTTP "
                    f"{result['status']:<4} "
                    f"{len(result['text']):>9,} "
                    f"bytes   "
                    f"{result['elapsed_seconds']:.2f}s "
                    f"attempt={result['attempts']}"
                )

            else:
                print(
                    f"{key:<18}"
                    f"ERROR   "
                    f"{result['error']}   "
                    f"attempts="
                    f"{result['attempts']}"
                )

    print()
    print("=" * 78)

    print(
        f"Successful requests : "
        f"{successful_requests}/"
        f"{total_requests}"
    )

    if (
        successful_requests
        == total_requests
    ):
        print(
            "RESULT              : PASS"
        )
    else:
        print(
            "RESULT              : "
            "PARTIAL/FAILED"
        )

    print("=" * 78)


if __name__ == "__main__":
    main()
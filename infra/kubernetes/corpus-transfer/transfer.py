"""Browser-approved Nextcloud to Blob copy; credentials stay in pod memory."""

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SERVER = "https://myvdr.managemgroup.com:5443"
SOURCE = "source:DATAROOM CRV"
DESTINATION = "destination:crv-raw/DATAROOM CRV"
RCLONE = "/tools/rclone"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected HTTP redirect; refusing to forward credentials")


HTTP = urllib.request.build_opener(NoRedirects)


def trusted_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (
        (parsed.scheme, parsed.hostname, parsed.port)
        != ("https", "myvdr.managemgroup.com", 5443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Nextcloud returned an unexpected server origin")
    return url


def request_json(
    url: str, data: bytes | None = None, method: str | None = None,
    authorization: str | None = None,
) -> dict:
    headers = {"User-Agent": "CRV-Corpus-Transfer", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if authorization is not None:
        headers.update({"Authorization": authorization, "OCS-APIREQUEST": "true"})
    request = urllib.request.Request(
        trusted_url(url), data=data, headers=headers, method=method,
    )
    with HTTP.open(request, timeout=60) as response:
        return json.load(response)


def authorize() -> dict:
    flow = request_json(f"{SERVER}/index.php/login/v2", data=b"")
    endpoint = trusted_url(flow["poll"]["endpoint"])
    login_url = trusted_url(flow["login"])
    print(f"AUTHORIZE_IN_BROWSER: {login_url}", flush=True)
    body = urllib.parse.urlencode({"token": flow["poll"]["token"]}).encode()
    deadline = time.monotonic() + 19 * 60
    while time.monotonic() < deadline:
        try:
            credentials = request_json(endpoint, data=body)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise RuntimeError(f"Nextcloud authorization HTTP {error.code}") from None
        else:
            print("AUTHORIZATION_ACCEPTED", flush=True)
            return credentials
        time.sleep(5)
    raise TimeoutError("Browser approval expired; launch a new job to try again")


def rclone(
    *arguments: str, environment: dict[str, str], input_text: str | None = None,
    capture: bool = False,
) -> str:
    result = subprocess.run(
        [RCLONE, "--config", "/dev/null", *arguments],
        env=environment, input=input_text, text=True,
        stdout=subprocess.PIPE if capture else None,
        check=True,
    )
    return result.stdout if capture else ""


def inventory(environment: dict[str, str]) -> list[dict]:
    entries = json.loads(rclone(
        "lsjson", SOURCE, "--recursive", "--files-only",
        environment=environment, capture=True,
    ))
    return sorted(
        [
            {key: entry[key] for key in ("Path", "Size", "ModTime")}
            for entry in entries
        ],
        key=lambda entry: entry["Path"],
    )


def revoke(authorization: str) -> None:
    response = request_json(
        f"{SERVER}/ocs/v2.php/core/apppassword?format=json",
        method="DELETE", authorization=authorization,
    )
    if response["ocs"]["meta"]["statuscode"] not in (100, 200):
        raise RuntimeError("Nextcloud did not confirm app-password revocation")
    print("APP_PASSWORD_REVOKED", flush=True)


def transfer(credentials: dict) -> None:
    authorization = "Basic " + base64.b64encode(
        f"{credentials['loginName']}:{credentials['appPassword']}".encode()
    ).decode()
    try:
        trusted_url(credentials["server"])
        profile = request_json(
            f"{SERVER}/ocs/v2.php/cloud/user?format=json",
            authorization=authorization,
        )
        user_id = profile["ocs"]["data"]["id"]
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("Nextcloud did not return a canonical user ID")
        environment = os.environ.copy()
        # Rclone obscuring is reversible; keep the result in memory, not a config.
        obscured = rclone(
            "obscure", "-", environment=environment,
            input_text=credentials["appPassword"] + "\n", capture=True,
        ).strip()
        environment.update({
            "RCLONE_CONFIG_SOURCE_TYPE": "webdav",
            "RCLONE_CONFIG_SOURCE_URL": (
                f"{SERVER}/remote.php/dav/files/{urllib.parse.quote(user_id, safe='')}/"
            ),
            "RCLONE_CONFIG_SOURCE_VENDOR": "nextcloud",
            "RCLONE_CONFIG_SOURCE_USER": credentials["loginName"],
            "RCLONE_CONFIG_SOURCE_PASS": obscured,
            "RCLONE_CONFIG_DESTINATION_TYPE": "azureblob",
            "RCLONE_CONFIG_DESTINATION_ACCOUNT": "stawmc6icu2kueq",
            "RCLONE_CONFIG_DESTINATION_ENV_AUTH": "true",
            "RCLONE_CONFIG_DESTINATION_DISABLE_CHECKSUM": "false",
        })
        # Fail before requesting the large corpus if Blob authorization is broken.
        rclone("lsf", "destination:crv-raw", "--max-depth", "1",
               environment=environment, capture=True)
        before = inventory(environment)
        if not before:
            raise RuntimeError("Source corpus is empty; refusing a success-shaped copy")
        print(
            f"SOURCE_INVENTORY files={len(before)} "
            f"bytes={sum(entry['Size'] for entry in before)}",
            flush=True,
        )
        rclone(
            "copy", SOURCE, DESTINATION, "--immutable",
            "--transfers", "2", "--checkers", "4",
            "--buffer-size", "8Mi", "--azureblob-upload-concurrency", "4",
            "--retries", "5", "--low-level-retries", "10",
            "--contimeout", "30s", "--timeout", "5m",
            "--stats", "1m", "--stats-one-line", "--log-level", "NOTICE",
            environment=environment,
        )
        print("VERIFYING_CONTENT: rereading source and Blob bytes", flush=True)
        rclone(
            "check", SOURCE, DESTINATION, "--download", "--one-way",
            "--checkers", "2", "--retries", "3",
            "--stats", "1m", "--stats-one-line", "--log-level", "NOTICE",
            environment=environment,
        )
        after = inventory(environment)
        if before != after:
            raise RuntimeError("Source inventory changed during transfer; rerun required")
        report = {
            "status": "verified",
            "verification": "rclone check --download --one-way",
            "source": SERVER + "/DATAROOM CRV",
            "destination": "https://stawmc6icu2kueq.blob.core.windows.net/crv-raw/DATAROOM CRV",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "files": len(after),
            "bytes": sum(entry["Size"] for entry in after),
            "inventory": after,
        }
        name = os.environ["TRANSFER_JOB"]
        rclone(
            "rcat", f"destination:crv-raw/_transfer-reports/{name}.json",
            environment=environment, input_text=json.dumps(report, ensure_ascii=False),
        )
        print(f"TRANSFER_VERIFIED files={report['files']} bytes={report['bytes']}", flush=True)
    finally:
        revoke(authorization)


if __name__ == "__main__":
    try:
        transfer(authorize())
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Transfer failed: Nextcloud HTTP {error.code}") from None

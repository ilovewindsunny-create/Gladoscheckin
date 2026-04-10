import datetime
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from pypushdeer import PushDeer


ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
ENV_COOKIES = "GLADOS_COOKIES"
ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"

CHECKIN_URL = "https://glados.cloud/api/user/checkin"
STATUS_URL = "https://glados.cloud/api/user/status"
POINTS_URL = "https://glados.cloud/api/user/points"
EXCHANGE_URL = "https://glados.cloud/api/user/exchange"

CHECKIN_DATA = {"token": "glados.cloud"}
HEADERS_TEMPLATE = {
    "referer": "https://glados.cloud/console/checkin",
    "origin": "https://glados.cloud",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "content-type": "application/json;charset=UTF-8",
}
EXCHANGE_POINTS = {"plan100": 100, "plan200": 200, "plan500": 500}


def beijing_time_converter(timestamp: float) -> time.struct_time:
    utc_dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    beijing_tz = datetime.timezone(datetime.timedelta(hours=8))
    beijing_dt = utc_dt.astimezone(beijing_tz)
    return beijing_dt.timetuple()


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
root_logger = logging.getLogger()
for handler in root_logger.handlers:
    if hasattr(handler, "formatter") and handler.formatter is not None:
        handler.formatter.converter = beijing_time_converter

logger = logging.getLogger(__name__)


class CheckinRuntimeError(RuntimeError):
    """Raised when the workflow should fail visibly."""


def load_config() -> Tuple[str, List[str], str]:
    push_key = (os.environ.get(ENV_PUSH_KEY) or "").strip()
    raw_cookies = (os.environ.get(ENV_COOKIES) or "").strip()
    exchange_plan = (os.environ.get(ENV_EXCHANGE_PLAN) or "plan500").strip()

    if not raw_cookies:
        raise CheckinRuntimeError(
            f"Missing required secret: {ENV_COOKIES}. "
            "Add your GLaDOS cookie in GitHub Actions secrets."
        )

    cookies = [cookie.strip() for cookie in raw_cookies.split("&") if cookie.strip()]
    if not cookies:
        raise CheckinRuntimeError(
            f"Secret {ENV_COOKIES} is set, but it does not contain any valid cookie."
        )

    if exchange_plan not in EXCHANGE_POINTS:
        logger.warning(
            "Invalid %s value '%s'; falling back to plan500.",
            ENV_EXCHANGE_PLAN,
            exchange_plan,
        )
        exchange_plan = "plan500"

    logger.info("Loaded %s cookie(s).", len(cookies))
    logger.info("PushDeer configured: %s", "yes" if push_key else "no")
    logger.info("Exchange plan: %s", exchange_plan)
    return push_key, cookies, exchange_plan


def request_json(
    url: str,
    method: str,
    cookie: str,
    data: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    headers = HEADERS_TEMPLATE.copy()
    headers["cookie"] = cookie

    try:
        response = requests.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=data,
            timeout=30,
        )
    except requests.RequestException as exc:
        return None, f"request error: {exc}"

    if not response.ok:
        body_preview = response.text.strip().replace("\n", " ")[:300]
        return None, f"http {response.status_code}: {body_preview}"

    try:
        return response.json(), None
    except json.JSONDecodeError:
        body_preview = response.text.strip().replace("\n", " ")[:300]
        return None, f"invalid json: {body_preview}"


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def checkin_and_process(cookie: str, exchange_plan: str, account_index: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": "failed",
        "status_text": "check-in request failed",
        "points_gained": 0,
        "remaining_days": "unknown",
        "remaining_points": "unknown",
        "exchange": "skipped",
        "errors": [],
    }

    checkin_data, error = request_json(CHECKIN_URL, "POST", cookie, CHECKIN_DATA)
    if error:
        result["errors"].append(f"checkin: {error}")
        result["status_text"] = f"check-in failed: {error}"
        return result

    message = str(checkin_data.get("message", ""))
    points_gained = parse_int(checkin_data.get("points")) or 0
    result["points_gained"] = points_gained

    if "Checkin! Got" in message:
        result["status"] = "success"
        result["status_text"] = f"check-in succeeded, got {points_gained} point(s)"
    elif (
        "Checkin Repeats!" in message
        or "Today's observation logged. Return tomorrow for more points." in message
    ):
        result["status"] = "repeat"
        result["status_text"] = "already checked in today"
        result["points_gained"] = 0
    else:
        result["status_text"] = f"check-in failed: {message or 'unknown response'}"
        result["errors"].append(f"unexpected checkin response: {message or 'empty message'}")

    status_data, error = request_json(STATUS_URL, "GET", cookie)
    if error:
        result["errors"].append(f"status: {error}")
    else:
        left_days = parse_int((status_data.get("data") or {}).get("leftDays"))
        if left_days is None:
            result["errors"].append("status: leftDays missing or invalid")
        else:
            result["remaining_days"] = str(left_days)

    points_data, error = request_json(POINTS_URL, "GET", cookie)
    current_points = None
    if error:
        result["errors"].append(f"points: {error}")
    else:
        current_points = parse_int(points_data.get("points"))
        if current_points is None:
            result["errors"].append("points: value missing or invalid")
        else:
            result["remaining_points"] = str(current_points)

    required_points = EXCHANGE_POINTS[exchange_plan]
    if current_points is not None and current_points >= required_points:
        exchange_data, error = request_json(
            EXCHANGE_URL,
            "POST",
            cookie,
            {"planType": exchange_plan},
        )
        if error:
            result["exchange"] = f"exchange failed: {error}"
            result["errors"].append(f"exchange: {error}")
        else:
            code = exchange_data.get("code")
            exchange_message = str(exchange_data.get("message", ""))
            if code == 0:
                result["exchange"] = f"exchange succeeded: {exchange_plan}"
            else:
                result["exchange"] = (
                    f"exchange failed: {exchange_plan}, "
                    f"code={code}, message={exchange_message or 'unknown'}"
                )
                result["errors"].append(
                    f"exchange returned code {code} for account {account_index}"
                )
    else:
        result["exchange"] = f"exchange skipped: need {required_points} points"

    return result


def format_push_content(results: List[Dict[str, Any]]) -> Tuple[str, str]:
    success_count = sum(1 for item in results if item["status"] == "success")
    repeat_count = sum(1 for item in results if item["status"] == "repeat")
    fail_count = sum(1 for item in results if item["status"] == "failed")

    title = (
        f"GLaDOS check-in: success {success_count}, "
        f"failed {fail_count}, repeat {repeat_count}"
    )

    lines = []
    for index, item in enumerate(results, start=1):
        lines.append(
            " | ".join(
                [
                    f"Account {index}",
                    f"status={item['status_text']}",
                    f"points+={item['points_gained']}",
                    f"leftDays={item['remaining_days']}",
                    f"totalPoints={item['remaining_points']}",
                    f"exchange={item['exchange']}",
                ]
            )
        )
        if item["errors"]:
            lines.append(f"  errors: {'; '.join(item['errors'])}")

    return title, "\n".join(lines)


def send_push(push_key: str, title: str, content: str) -> None:
    if not push_key:
        logger.info("PushDeer key not set, skip notification.")
        return

    try:
        pushdeer = PushDeer(pushkey=push_key)
        pushdeer.send_text(title, desp=content)
        logger.info("Push notification sent.")
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error("Failed to send PushDeer notification: %s", exc)


def main() -> int:
    push_key = ""
    try:
        push_key, cookies, exchange_plan = load_config()
        results = []
        for index, cookie in enumerate(cookies, start=1):
            logger.info("Processing account %s...", index)
            results.append(checkin_and_process(cookie, exchange_plan, index))

        title, content = format_push_content(results)
        logger.info(title)
        logger.info("\n%s", content)
        send_push(push_key, title, content)

        if any(item["status"] == "failed" or item["errors"] for item in results):
            logger.error("One or more accounts did not complete successfully.")
            return 1
        return 0
    except CheckinRuntimeError as exc:
        logger.error("%s", exc)
        send_push(push_key, "GLaDOS check-in failed", str(exc))
        return 1
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("Unexpected error during check-in: %s", exc)
        send_push(push_key, "GLaDOS check-in crashed", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
